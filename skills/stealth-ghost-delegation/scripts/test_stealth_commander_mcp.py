import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import stealth_commander_mcp as mcp


class StealthCommanderMcpTests(unittest.TestCase):
    def test_initialize_returns_stealth_commander_info(self):
        resp = mcp.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(resp["result"]["serverInfo"]["name"], "Stealth Commander")

    def test_tools_list_exposes_core_tools(self):
        resp = mcp.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertTrue({"doctor", "dashboard_status", "list_hidden_desktops", "start_native_app", "start_browser", "start_pycharm", "session_action", "start_observer", "run_command"}.issubset(names))

    def test_tools_call_dispatches_to_runner(self):
        calls = []

        def fake_runner(script, args, timeout=120):
            calls.append((script, args))
            return {"ok": True, "script": script, "args": args}

        resp = mcp.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "session_action",
                    "arguments": {"kind": "native", "action": "status", "session": "D:\\Work\\s1"},
                },
            },
            _runner=fake_runner,
        )
        self.assertFalse(resp["result"]["isError"])
        self.assertEqual(calls, [("native_desktop.py", ["status", "--session", "D:\\Work\\s1"])])

    def test_run_command_executes_without_window(self):
        resp = mcp.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "run_command",
                    "arguments": {"argv": [sys.executable, "-c", "print('ok')"]},
                },
            }
        )
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["stdout"].strip(), "ok")

    def test_dashboard_status_strips_private_watch_urls(self):
        from unittest import mock
        fake_dash = {
            "ok": True,
            "url": "http://127.0.0.1:61076/token123",
            "started": True,
            "meta": {"build": "abc", "desktops": [{"id": "d1", "name": "Chrome", "state": "live", "url": "http://127.0.0.1:50000/secret"}]},
        }
        with mock.patch.object(mcp, "ensure_dashboard", return_value=fake_dash):
            res = mcp.handle_tool_call("dashboard_status", {})
        self.assertTrue(res["ok"])
        self.assertTrue(res["started"])
        self.assertEqual(res["dashboard_url"], "http://127.0.0.1:61076/token123")
        self.assertNotIn("url", res["result"]["desktops"][0])

    def test_remote_hub_serves_token_gated_portal_and_mcp_endpoint(self):
        import http.client
        from pathlib import Path
        import tempfile
        import threading
        from unittest import mock

        route_id = "unit_test_route_identifier_00000000"
        with tempfile.TemporaryDirectory() as tmp:
            handler = mcp._make_remote_hub_handler(route_id, Path(tmp))
            server = mcp.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                conn.request("GET", "/wrong-token")
                self.assertEqual(conn.getresponse().status, 404)
                conn.close()

                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                conn.request("GET", f"/{route_id}")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                self.assertIn(b"Stealth Commander", resp.read())
                conn.close()

                fake_dash = {"ok": True, "meta": {"desktops": [{"id": "d1", "name": "Chrome", "state": "live", "url": "http://127.0.0.1:59999/obs"}]}}
                with mock.patch.object(mcp, "ensure_dashboard", return_value=fake_dash):
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    conn.request("GET", f"/{route_id}/meta")
                    meta = json.loads(conn.getresponse().read().decode("utf-8"))
                    conn.close()
                self.assertTrue(meta["device"]["online"])
                self.assertEqual(meta["desktops"][0]["stream_path"], f"/{route_id}/stream/d1.mjpg")

                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                conn.request("POST", f"/{route_id}/mcp", body=json.dumps({"jsonrpc": "2.0", "id": 9, "method": "initialize"}), headers={"Content-Type": "application/json"})
                rpc = json.loads(conn.getresponse().read().decode("utf-8"))
                conn.close()
                self.assertEqual(rpc["result"]["serverInfo"]["name"], "Stealth Commander")
            finally:
                server.shutdown()
                server.server_close()

    def test_account_cli_and_portal_contract(self):
        self.assertIn("view-account", mcp.PORTAL_HTML)
        self.assertIn("authModal", mcp.PORTAL_HTML)
        self.assertIn("submitAuth", mcp.PORTAL_HTML)

        opts = mcp._parse_cli_flags(["--email", "user@example.com", "--password=secret", "--register"])
        self.assertEqual(opts["email"], "user@example.com")
        self.assertEqual(opts["password"], "secret")
        self.assertTrue(opts["register"])

        with tempfile.TemporaryDirectory() as td:
            acct_file = Path(td) / "account.json"
            with mock.patch.object(mcp, "_account_config_file", return_value=acct_file), mock.patch.object(mcp, "_tunnel_config_file", return_value=Path(td) / "tunnel.json"), mock.patch.object(mcp.socket, "gethostname", return_value="TEST-PC"):
                fake_user = {
                    "email": "user@example.com",
                    "name": "user",
                    "api_key": "SYNTHETIC_TEST_API_KEY",
                    "devices": [{"device_id": "test-pc", "hostname": "TEST-PC"}],
                }
                with mock.patch.object(
                    mcp,
                    "_portal_api_request",
                    side_effect=[
                        {"ok": True, "token": "SYNTHETIC_TEST_SESSION_TOKEN", "user": fake_user},
                        {"ok": True, "device_token": "sc_dev_" + "d" * 48, "device": {"device_id": "test-pc", "hostname": "TEST-PC"}},
                        {"ok": True, "device": {"device_id": "test-pc", "hostname": "TEST-PC"}},
                        {"ok": True},
                    ],
                ):
                    login_res = mcp.account_login(email="user@example.com", password="secret", register=True)
                    self.assertTrue(login_res["ok"])
                    self.assertEqual(login_res["account"]["email"], "user@example.com")
                    self.assertTrue(acct_file.is_file())

                    who_res = mcp.account_whoami()
                    self.assertTrue(who_res["ok"])
                    self.assertTrue(who_res["logged_in"])
                    self.assertEqual(who_res["account"]["device_id"], "test-pc")
                    self.assertNotIn("api_key", json.dumps(who_res))

                    out_res = mcp.account_logout()
                    self.assertTrue(out_res["ok"])
                    self.assertFalse(acct_file.is_file())


if __name__ == "__main__":
    unittest.main()
