"""Native observer source ownership checks without real desktops or applications."""
import contextlib
import json
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest import mock
import desktop_watch as w
import native_desktop as n

NAME = 'PyCharmHarness_Native_' + 'a' * 32


class NativeObserverSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name).resolve()
        self.exe = str(self.folder / 'application.exe')
        self.guardian = {'pid': 201, 'created': 90.0, 'exe': str(self.folder / 'python.exe')}
        self.launch = {'pid': 101, 'created': 100.0, 'exe': self.exe}
        self.state = {'schema': n.SCHEMA, 'session': str(self.folder), 'desktop': NAME, 'status': 'ready',
                      'executable': self.exe, 'guardian': self.guardian, 'launch': self.launch}
        self.snapshot = {**self.state, 'processes': [self.launch], 'windows': [
            {'hwnd': 42, 'tid': 301, **self.launch}]}
        self.save()
        self.guard = types.SimpleNamespace(pid=201, create_time=lambda: 90.0, exe=lambda: self.guardian['exe'],
                                           is_running=lambda: True, cmdline=lambda: [self.guardian['exe'], str(n.SCRIPT), '_guardian', '--session', str(self.folder)])
        self.app = types.SimpleNamespace(pid=101, create_time=lambda: 100.0, exe=lambda: self.exe,
                                         is_running=lambda: True, parent=lambda: self.guard, children=lambda recursive: [])
        self.procs = {201: self.guard, 101: self.app}
        self.desktop = types.SimpleNamespace(name=NAME, CloseDesktop=mock.Mock())
        self.window_pid = 101
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for patcher in (
            mock.patch.object(n, 'command', side_effect=lambda args: {'ok': True, 'result': {
                **self.snapshot, 'heartbeat': time.time()}}),
            mock.patch.object(w.h, 'psutil', types.SimpleNamespace(Process=lambda pid: self.procs[pid], NoSuchProcess=KeyError, AccessDenied=PermissionError), create=True),
            mock.patch.object(w.h, 'input_desktop', return_value='Default'),
            mock.patch.object(w.h, 'desktop_name', side_effect=lambda handle: handle.name),
            mock.patch.object(w.h, 'win32service', types.SimpleNamespace(OpenDesktop=lambda *args: self.desktop), create=True),
            mock.patch.object(w.h, 'win32con', types.SimpleNamespace(DESKTOP_READOBJECTS=1, DESKTOP_ENUMERATE=2), create=True),
            mock.patch.object(w.h, 'win32process', types.SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (301, self.window_pid)), create=True),
            mock.patch.object(w.h, 'win32gui', types.SimpleNamespace(EnumDesktopWindows=lambda handle, callback, value: callback(42, value), IsWindowVisible=lambda hwnd: True), create=True)):
            stack.enter_context(patcher)

    def save(self):
        (self.folder / n.STATE).write_text(json.dumps(self.state), encoding='utf-8')

    def test_ready_native_source_maps_stable_verified_guardian_identity(self):
        source = w.source_state(self.folder)
        self.assertEqual(w.identity(source), (NAME, 201, 90.0, self.guardian['exe']))
        self.assertEqual(source['session'], str(self.folder))
        self.desktop.CloseDesktop.assert_called_once_with()

    def test_guardian_commandline_must_match_exact_session(self):
        self.guard.cmdline = lambda: [self.guardian['exe'], str(n.SCRIPT), '_guardian', '--session', 'another-session']
        with self.assertRaisesRegex(RuntimeError, 'guardian'):
            w.source_state(self.folder)

    def test_reused_launch_pid_or_wrong_executable_is_rejected(self):
        self.app.create_time = lambda: 101.0
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            w.source_state(self.folder)
        self.app.create_time = lambda: 100.0
        self.app.exe = lambda: str(self.folder / 'another.exe')
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            w.source_state(self.folder)

    def test_window_outside_guardian_job_snapshot_is_rejected(self):
        self.snapshot['processes'] = []
        self.snapshot['windows'] = []
        with self.assertRaisesRegex(RuntimeError, 'owned window'):
            w.source_state(self.folder)

    def handoff(self):
        child = {'pid': 102, 'created': 101.0, 'exe': self.exe}
        self.procs.pop(101, None)
        self.procs[102] = types.SimpleNamespace(pid=102, create_time=lambda: 101.0,
            exe=lambda: self.exe, is_running=lambda: True, parent=lambda: None, children=lambda recursive: [])
        self.snapshot.update(processes=[child], windows=[{'hwnd': 42, 'tid': 301, **child}])
        self.window_pid = 102
        return child

    def test_existing_observer_survives_launcher_exit_with_owned_child(self):
        source = w.source_state(self.folder)
        config = {'anchor': None, 'source': str(self.folder), 'source_identity': list(w.identity(source))}
        child = self.handoff()
        current = w.observer_source(config)
        self.assertEqual(w.identity(current), w.identity(source))
        self.assertEqual(current['processes'], [child])

    def test_reused_child_pid_and_guardian_replacement_are_rejected(self):
        self.handoff()
        self.procs[102].create_time = lambda: 102.0
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            w.source_state(self.folder)
        self.procs[102].create_time = lambda: 101.0
        self.guard.create_time = lambda: 91.0
        with self.assertRaisesRegex(RuntimeError, 'guardian'):
            w.source_state(self.folder)

    def test_forged_or_failed_guardian_snapshot_is_rejected(self):
        for key, value in (('guardian', {**self.guardian, 'pid': 999}),
                           ('desktop', 'PyCharmHarness_Native_' + 'b' * 32),
                           ('session', str(self.folder.parent))):
            with self.subTest(key=key):
                with mock.patch.object(n, 'command', return_value={'ok': True, 'result': {
                    **self.snapshot, key: value, 'heartbeat': time.time()}}):
                    with self.assertRaises((ValueError, RuntimeError)):
                        w.source_state(self.folder)
        with mock.patch.object(n, 'command', return_value={'ok': False, 'error': 'Unavailable'}):
            with self.assertRaises(RuntimeError):
                w.source_state(self.folder)

    def test_stale_snapshot_or_guardian_exit_during_verification_is_rejected(self):
        with mock.patch.object(n, 'command', return_value={'ok': True, 'result': {
                **self.snapshot, 'heartbeat': time.time() - 10}}):
            with self.assertRaisesRegex(RuntimeError, 'stale'):
                w.source_state(self.folder)
        with mock.patch.object(n, 'guardian_alive', side_effect=[True, False]):
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                w.source_state(self.folder)

    def test_file_process_list_never_overrides_live_guardian_job_membership(self):
        self.state.update(processes=[{'pid': 999, 'created': 101.0, 'exe': self.exe}],
                          windows=[{'hwnd': 43, 'tid': 302, 'pid': 999, 'created': 101.0, 'exe': self.exe}])
        self.save()
        source = w.source_state(self.folder)
        self.assertEqual(source['processes'], [self.launch])

    def test_wrong_desktop_window_or_input_desktop_fails_closed(self):
        self.window_pid = 999
        with self.assertRaisesRegex(RuntimeError, 'owned window'):
            w.source_state(self.folder)
        self.window_pid = 101
        with mock.patch.object(w.h, 'input_desktop', return_value=NAME):
            with self.assertRaisesRegex(RuntimeError, 'inactive'):
                w.source_state(self.folder)

    def test_native_schema_session_and_launch_metadata_are_validated(self):
        for key, value in (('schema', 'forged'), ('session', str(self.folder.parent)), ('desktop', 'Default'),
                           ('status', 'stopped'), ('launch', {**self.launch, 'exe': str(self.folder / 'unapproved.exe')})):
            with self.subTest(key=key):
                original = self.state[key]
                self.state[key] = value
                self.save()
                with self.assertRaises((ValueError, RuntimeError)):
                    w.source_state(self.folder)
                self.state[key] = original
                self.save()


if __name__ == '__main__':
    unittest.main(verbosity=2)
