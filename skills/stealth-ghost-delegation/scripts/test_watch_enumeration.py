"""Closing windows must not terminate observation of surviving applications."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import desktop_watch as watch


class EnumerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        watch.h.dependencies(False)
        watch.h.native_modules()

    def run_windows(self, thread_desktop):
        gui = SimpleNamespace(
            IsWindowVisible=lambda hwnd: True,
            GetWindowText=lambda hwnd: "Window",
            GetWindowRect=lambda hwnd: (0, 0, 100, 100),
            EnumWindows=lambda callback, argument: [callback(hwnd, argument) for hwnd in (1, 2)],
        )
        with patch.object(watch.h, "assert_isolation"), \
                patch.object(watch.h, "win32gui", gui), \
                patch.object(watch.h, "win32process", SimpleNamespace(
                    GetWindowThreadProcessId=lambda hwnd: (hwnd, hwnd + 100))), \
                patch.object(watch.h, "win32service", SimpleNamespace(GetThreadDesktop=thread_desktop)), \
                patch.object(watch.h, "desktop_name", lambda handle: handle):
            return watch.desktop_windows({"desktop": "PyCharmHarness_test"})

    def test_closing_window_does_not_stop_other_captures(self):
        import pywintypes
        def desktop(thread):
            if thread == 1:
                raise pywintypes.error(170, "GetThreadDesktop", "Resource is in use")
            return "PyCharmHarness_test"
        self.assertEqual([row["hwnd"] for row in self.run_windows(desktop)], [2])

    def test_wrong_desktop_still_fails_closed(self):
        with self.assertRaises(RuntimeError):
            self.run_windows(lambda thread: "Default")


if __name__ == "__main__":
    unittest.main()
