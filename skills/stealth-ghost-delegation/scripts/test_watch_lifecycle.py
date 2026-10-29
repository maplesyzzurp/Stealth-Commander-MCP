"""Observer CLI isolation and cleanup contracts without real desktops or applications."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import desktop_watch as w


class WatchLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve()

    def command(self, action, state):
        output = io.StringIO()
        with (mock.patch.object(w.h, "dependencies"), mock.patch.object(w.h, "native_modules"),
              mock.patch.object(w, "config_at", return_value={"schema": w.SCHEMA}),
              mock.patch.object(w, "read", return_value=state), contextlib.redirect_stdout(output),
              mock.patch.object(sys, "argv", ["desktop_watch.py", action, "--watch", str(self.folder)])):
            code = w.main()
        return code, json.loads(output.getvalue())

    def test_stop_cleanup_failure_returns_error_and_nonzero_exit(self):
        state = {"status": "cleanup_failed", "cleanup": {"active_processes": 1, "worker_alive": True}}
        code, result = self.command("stop", state)
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"]["health"], state)

    def test_stop_requires_confirmed_empty_job_and_dead_worker(self):
        for cleanup in ({}, {"active_processes": 1, "worker_alive": False},
                        {"active_processes": 0, "worker_alive": True}):
            with self.subTest(cleanup=cleanup):
                code, result = self.command("stop", {"status": "stopped", "cleanup": cleanup})
                self.assertEqual(code, 1)
                self.assertFalse(result["ok"])

    def test_confirmed_stop_returns_success(self):
        code, result = self.command("stop", {"status": "stopped", "cleanup": {
            "active_processes": 0, "worker_alive": False}})
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])

    def test_unknown_input_desktop_query_is_never_substituted(self):
        error = OSError("OpenInputDesktop denied; active desktop is unknown")
        error.winerror = 5
        with (mock.patch.object(w.h, "dependencies"), mock.patch.object(w.h, "native_modules"),
              mock.patch.object(w.h, "input_desktop", side_effect=error),
              mock.patch.object(w, "start", side_effect=lambda args: w.h.input_desktop()),
              contextlib.redirect_stdout(io.StringIO()), mock.patch.object(sys, "argv", [
                  "desktop_watch.py", "start", "--desktop", "PyCharmHarness_test", "--pid", "1",
                  "--scope", "desktop", "--work-dir", str(self.folder)])):
            with self.assertRaises(OSError):
                w.main()


if __name__ == "__main__":
    unittest.main()
