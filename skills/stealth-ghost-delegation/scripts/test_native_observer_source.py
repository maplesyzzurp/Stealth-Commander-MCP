"""Native observer source ownership checks without real desktops or applications."""
import contextlib
import json
from pathlib import Path
import tempfile
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

    def test_ready_native_source_maps_verified_launch_identity(self):
        source = w.source_state(self.folder)
        self.assertEqual(w.identity(source), (NAME, 101, 100.0, self.exe))
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

    def test_live_launch_must_belong_to_exact_guardian(self):
        self.app.parent = lambda: None
        with self.assertRaisesRegex(RuntimeError, 'guardian'):
            w.source_state(self.folder)

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
