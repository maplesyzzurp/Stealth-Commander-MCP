"""Bounded loopback verifier tests: no screenshots, real apps, tunnels, or external requests."""
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest import mock

import cached_capture as c
import desktop_watch as w


class ExportVerifierTests(unittest.TestCase):
    def setUp(self):
        self.source = {"desktop": "PyCharmHarness_SyntheticVerifier",
                       "pid": 101, "process_created": 100.0, "ide_executable": sys.executable}
        self.token = "x" * 43
        self.proc = mock.Mock(pid=101)
        self.proc.is_running.return_value = True
        self.proc.create_time.return_value = 100.0
        self.proc.exe.return_value = sys.executable
        self.proc.parents.return_value = []
        patchers = [
            mock.patch.object(w.h, "assert_isolation"),
            mock.patch.object(w.h, "owned_process", return_value=self.proc),
            mock.patch.object(w.h, "input_desktop", return_value="Default"),
            mock.patch.object(w.h, "psutil", types.SimpleNamespace(Process=lambda pid: self.proc), create=True),
            mock.patch.object(w, "desktop_target", return_value={"hwnd": 42, "pid": 101}),
            mock.patch.object(c, "display_affinity_allowed", return_value=True)
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), w._make_export_verifier(self.source, self.token, "session"))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, overrides=None, headers=None, url="/verify"):
        data = {"hwnd": 42, "pid": 101, "process_created": 100.0,
                "process_executable": sys.executable,
                "desktop": self.source["desktop"], "scope": "session"}
        data.update(overrides or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request("POST", url, json.dumps(data), headers={
                "Content-Type": "application/json", "X-Stealth-Observer-Token": self.token, **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_owned_unprotected_window_is_approved(self):
        self.assertEqual(self.request(), (200, {"ok": True}))

    def test_invalid_token_origin_forwarding_and_path_are_denied(self):
        for kw in ({"headers": {"X-Stealth-Observer-Token": "wrong"}},
                   {"headers": {"Origin": "https://example.invalid"}},
                   {"headers": {"X-Forwarded-For": "192.0.2.1"}},
                   {"url": "/not-verify"}):
            with self.subTest(kw=kw):
                self.assertEqual(self.request(**kw)[0], 404)

    def test_wrong_desktop_pid_type_scope_and_protected_window_fail_closed(self):
        for bad in ({"desktop": "Default"}, {"pid": 102}, {"scope": "other"},
                    {"hwnd": True}, {"process_created": 101.0},
                    {"process_executable": "C:/some-other-app.exe"}):
            with self.subTest(bad=bad):
                self.assertEqual(self.request(overrides=bad)[0], 409)
        with mock.patch.object(c, "display_affinity_allowed", return_value=False):
            self.assertEqual(self.request()[0], 409)
        with mock.patch.object(w.h, "assert_isolation", side_effect=RuntimeError("not private")):
            self.assertEqual(self.request()[0], 409)


if __name__ == "__main__":
    unittest.main(verbosity=2)
