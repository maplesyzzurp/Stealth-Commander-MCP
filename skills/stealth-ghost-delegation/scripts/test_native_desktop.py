"""Exercise native launcher ownership and Win32 window-closing races."""

import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import native_desktop as native


NAME = 'PyCharmHarness_Native_' + 'a' * 32
APP = {'pid': 40, 'created': 100.5, 'exe': str(Path('example.exe').resolve())}


class NativeDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        native.native()

    def test_state_save_survives_a_concurrent_reader_released_during_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            native.save(folder, {'status': 'ready', 'document': 'original'})
            destination = folder / native.STATE
            reader = destination.open('r', encoding='utf-8')
            self.assertEqual(json.load(reader), {'status': 'ready', 'document': 'original'})
            released = threading.Event()

            def release_reader():
                time.sleep(.1)
                reader.close()
                released.set()

            release = threading.Thread(target=release_reader)
            release.start()
            try:
                started = time.monotonic()
                native.save(folder, {'status': 'ready', 'document': 'updated'})
                elapsed = time.monotonic() - started
            finally:
                release.join()
                reader.close()
            self.assertTrue(released.is_set())
            self.assertGreaterEqual(elapsed, .05)
            self.assertEqual(json.loads(destination.read_text(encoding='utf-8')),
                             {'status': 'ready', 'document': 'updated'})
            self.assertEqual(list(folder.glob('*.tmp')), [])

    def enumerate(self, *, closing=None, outer=None, owner=True, desktop=NAME):
        def visit_all(handle, callback, context):
            callback(1, context)
            callback(2, context)
            if outer is not None:
                raise native.pywintypes.error(outer, 'EnumDesktopWindows', 'Window closed')

        def get_thread(hwnd):
            if hwnd == 1 and closing is not None:
                raise native.pywintypes.error(closing, 'GetWindowThreadProcessId', 'Window closed')
            return hwnd, 40 if hwnd == 2 else 999

        gui = SimpleNamespace(EnumDesktopWindows=visit_all, IsWindowVisible=lambda _: True,
                              GetWindowText=lambda _: 'Owned document',
                              GetWindowRect=lambda _: (0, 0, 200, 100))
        with patch.object(native.h, 'win32gui', gui), \
                patch.object(native.h, 'win32process', SimpleNamespace(GetWindowThreadProcessId=get_thread)), \
                patch.object(native.h, 'win32service', SimpleNamespace(GetThreadDesktop=lambda _: desktop)), \
                patch.object(native.h, 'desktop_name', side_effect=lambda handle: desktop if handle == 'handle' else handle), \
                patch.object(native.h, 'input_desktop', return_value='Default'), \
                patch.object(native, 'job_owns', return_value=owner):
            return native.owned_windows('handle', NAME, 'job', {40: APP})

    def test_only_verified_job_window_is_reported(self):
        self.assertEqual(self.enumerate(), [{'hwnd': 2, 'tid': 2, **APP, 'title': 'Owned document',
                                            'rect': [0, 0, 200, 100]}])
        self.assertEqual(self.enumerate(owner=False), [])

    def test_closing_window_error_zero_or_six_preserves_surviving_window(self):
        for code in (0, 6):
            with self.subTest(error=code):
                self.assertEqual([item['hwnd'] for item in self.enumerate(closing=code)], [2])
                self.assertEqual([item['hwnd'] for item in self.enumerate(outer=code)], [2])

    def test_enumeration_denied_or_wrong_desktop_fails_closed(self):
        with self.assertRaises(native.pywintypes.error):
            self.enumerate(outer=5)
        with self.assertRaisesRegex(RuntimeError, 'inactive'):
            self.enumerate(desktop='Default')

    def test_reused_pid_or_other_executable_is_not_owned(self):
        handle = SimpleNamespace(Close=lambda: None)
        with patch.object(native.h.win32api, 'OpenProcess', return_value=handle), \
                patch.object(native.job_api, 'IsProcessInJob', return_value=True):
            with patch.object(native, 'process_identity', return_value={**APP, 'created': 101.0}):
                self.assertFalse(native.job_owns('job', APP))
            with patch.object(native, 'process_identity', return_value={**APP, 'exe': 'other.exe'}):
                self.assertFalse(native.job_owns('job', APP))
            with patch.object(native, 'process_identity', return_value=dict(APP)):
                self.assertTrue(native.job_owns('job', APP))

    def test_identity_match_outside_job_is_rejected(self):
        handle = SimpleNamespace(Close=lambda: None)
        with patch.object(native.h.win32api, 'OpenProcess', return_value=handle), \
                patch.object(native.job_api, 'IsProcessInJob', return_value=False), \
                patch.object(native, 'process_identity', return_value=dict(APP)):
            self.assertFalse(native.job_owns('job', APP))

    def test_new_desktop_rejects_existing_and_active_destinations(self):
        closed = []
        handle = SimpleNamespace(CloseDesktop=lambda: closed.append('closed'))
        service = SimpleNamespace(GetProcessWindowStation=lambda: 'station',
                                  OpenDesktop=lambda *args: handle)
        with patch.object(native.h, 'win32service', service), \
                patch.object(native.h, 'desktop_name', return_value='WinSta0'):
            with self.assertRaisesRegex(RuntimeError, 'already exists'):
                native.new_desktop(NAME)
        self.assertEqual(closed, ['closed'])

        def absent(*args):
            raise native.pywintypes.error(2, 'OpenDesktop', 'Not found')

        service.OpenDesktop = absent
        service.CreateDesktop = lambda *args: handle
        with patch.object(native.h, 'win32service', service), \
                patch.object(native.h, 'desktop_name', side_effect=lambda item: 'WinSta0' if item == 'station' else NAME), \
                patch.object(native.h, 'input_desktop', return_value='Default'):
            self.assertIs(native.new_desktop(NAME), handle)
        with patch.object(native.h, 'win32service', service), \
                patch.object(native.h, 'desktop_name', side_effect=lambda item: 'WinSta0' if item == 'station' else NAME), \
                patch.object(native.h, 'input_desktop', return_value=NAME):
            with self.assertRaisesRegex(RuntimeError, 'inactive'):
                native.new_desktop(NAME)
        self.assertEqual(closed, ['closed', 'closed'])

    def test_graceful_close_rechecks_targets_and_skips_a_closing_window(self):
        messages = []

        def thread(hwnd):
            if hwnd == 1:
                raise native.pywintypes.error(6, 'GetWindowThreadProcessId', 'Window closed')
            return 200, 40 if hwnd == 2 else 999

        windows = [{'hwnd': number, 'tid': 200, **APP} for number in (1, 2, 3)]
        with patch.object(native, 'snapshot', return_value={'windows': windows}), \
                patch.object(native, 'inactive'), \
                patch.object(native, 'job_owns', return_value=True), \
                patch.object(native.h.win32process, 'GetWindowThreadProcessId', side_effect=thread), \
                patch.object(native.h.win32service, 'GetThreadDesktop', return_value='desktop'), \
                patch.object(native.h, 'desktop_name', return_value=NAME), \
                patch.object(native.h.win32gui, 'PostMessage', side_effect=lambda hwnd, *args: messages.append(hwnd)), \
                patch.object(native, 'active_processes', return_value=0):
            result = native.close_owned({'desktop': NAME}, 'desktop', 'job', False)
        self.assertEqual(messages, [2])
        self.assertEqual(result, {'stopped': True, 'active_processes': 0,
                                  'forced_owned_job': False, 'desktop_released': False})

    def test_modal_application_retains_job_until_explicit_force(self):
        active = {'owned': 1, 'external': 1}
        with patch.object(native, 'snapshot', return_value={'windows': []}), \
                patch.object(native, 'active_processes', side_effect=lambda job: active[job]), \
                patch.object(native.time, 'monotonic', side_effect=[0, 6]), \
                patch.object(native.job_api, 'TerminateJobObject', side_effect=lambda job, code: active.__setitem__(job, 0)):
            result = native.close_owned({'desktop': NAME}, 'desktop', 'owned', False)
            self.assertEqual(result['active_processes'], 1)
            self.assertFalse(result['stopped'])
            self.assertEqual(active, {'owned': 1, 'external': 1})
        with patch.object(native, 'active_processes', side_effect=lambda job: active[job]), \
                patch.object(native.job_api, 'TerminateJobObject', side_effect=lambda job, code: active.__setitem__(job, 0)):
            result = native.close_owned({'desktop': NAME}, 'desktop', 'owned', True)
            self.assertTrue(result['stopped'])
            self.assertTrue(result['forced_owned_job'])
            self.assertEqual(active, {'owned': 0, 'external': 1})

    def test_status_resolves_natural_exit_during_guardian_check(self):
        folder = Path('session').resolve()
        running = {'status': 'ready'}
        stopped = {'status': 'stopped', 'cleanup': {'active_processes': 0}}
        with patch.object(native, 'read_state', side_effect=[(folder, running), (folder, stopped)]), \
                patch.object(native, 'guardian_alive', return_value=False):
            result = native.command(SimpleNamespace(action='status', session=str(folder)))
        self.assertEqual(result, {'ok': True, 'result': {**stopped, 'guardian_alive': False}})

    def test_unavailable_guardian_never_accepts_a_stale_ready_state(self):
        folder = Path('session').resolve()
        with patch.object(native, 'read_state', return_value=(folder, {'status': 'ready'})), \
                patch.object(native, 'guardian_alive', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'Cleanup is unconfirmed'):
                native.command(SimpleNamespace(action='stop', session=str(folder), force_owned_job=True))


if __name__ == '__main__':
    unittest.main()
