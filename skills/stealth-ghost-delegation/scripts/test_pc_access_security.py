"""Synthetic transport and process tests: no real credentials, tunnels, commands or GUI input."""
import ctypes
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

import stealth_commander_mcp as m


class RemotePairingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, filename in (("_account_config_file", "account.json"), ("_tunnel_config_file", "tunnel.json"), ("_hub_state_file", "hub-state.json")):
            patcher = mock.patch.object(m, name, return_value=self.root / filename)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.account = {"email": "owner@example.invalid", "device_id": "synthetic-pc", "device_token": "sc_dev_" + "d" * 48}
        m._save_account_config(self.account)
        self.route = "s" * 43
        self.origin = "https://synthetic-pc.trycloudflare.com"
        self.pairing = {"ok": True, "email": self.account["email"], "device_id": self.account["device_id"],
                        "device": {"device_id": self.account["device_id"], "mcp_url": self.origin + "/" + self.route + "/mcp"}}
        self.saved = {"public_origin": self.origin, "remote_access_enabled": True,
                      "mcp_url": self.pairing["device"]["mcp_url"], "local_portal_url": "http://127.0.0.1:61077/" + self.route}
        m._write_tunnel_config(self.saved)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), m._make_remote_hub_handler(self.route, self.root))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, *, remote=True, headers=None, method="POST", path=None):
        req_headers = {"Content-Type": "application/json"}
        if remote:
            req_headers.update({"Host": "synthetic-pc.trycloudflare.com", "Cf-Connecting-Ip": "192.0.2.25", "X-Stealth-PC-Authorization": "sc_pct_" + "a" * 48})
        req_headers.update(headers or {})
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "run_command", "arguments": {"argv": ["SYNTHETIC_NO_PROCESS"]}}}
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            self.last_body = json.dumps(body).encode() if method == "POST" else b""
            connection.request(method, path or "/" + self.route + "/mcp", body=self.last_body if method == "POST" else None, headers=req_headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_every_remote_request_checks_current_account_and_endpoint(self):
        with mock.patch.object(m, "_portal_api_request", return_value=self.pairing) as api, mock.patch.object(m, "handle_tool_call", return_value={"ok": True, "sentinel": "NO_PROCESS"}) as dispatch:
            self.assertEqual(self.request()[0], 200)
            self.assertEqual(self.request()[0], 200)
            self.assertEqual(api.call_count, 2)
            self.assertEqual(api.call_args.args, ("/api/devices/authorize",))
            self.assertEqual(api.call_args.kwargs, {"method": "POST", "payload": {"ticket": "sc_pct_" + "a" * 48, "method": "POST", "path": "/mcp", "body_sha256": hashlib.sha256(self.last_body).hexdigest()}, "token": self.account["device_token"]})
            self.assertEqual(dispatch.call_count, 2)

    def test_revoked_unavailable_other_account_pc_and_capability_fail_closed(self):
        invalid = [{"ok": False}, {}, {**self.pairing, "email": "other@example.invalid"},
                   {**self.pairing, "device_id": "other-pc"},
                   {**self.pairing, "device": {"device_id": "synthetic-pc", "mcp_url": self.origin + "/" + "x" * 43 + "/mcp"}},
                   {**self.pairing, "device": {"device_id": "other-pc", "mcp_url": self.pairing["device"]["mcp_url"]}}]
        for response in invalid:
            with self.subTest(response=response), mock.patch.object(m, "_portal_api_request", return_value=response), mock.patch.object(m, "handle_tool_call") as dispatch:
                self.assertEqual(self.request()[0], 404)
                dispatch.assert_not_called()

    def test_unpaired_or_retired_direct_capability_is_denied(self):
        m._account_config_file().unlink()
        with mock.patch.object(m, "_portal_api_request") as api, mock.patch.object(m, "handle_tool_call") as dispatch:
            self.assertEqual(self.request()[0], 404)
            api.assert_not_called()
            dispatch.assert_not_called()
        m._save_account_config(self.account)
        m._write_tunnel_config({**self.saved, "retired_remote_token": self.route})
        with mock.patch.object(m, "_portal_api_request") as api:
            self.assertEqual(self.request()[0], 404)
            api.assert_not_called()

    def test_remote_browser_origins_and_missing_forwarding_header_cannot_be_loopback(self):
        with mock.patch.object(m, "_portal_api_request", return_value=self.pairing), mock.patch.object(m, "handle_tool_call", return_value={"ok": True}):
            self.assertEqual(self.request(headers={"Origin": m.PAGES_ORIGIN})[0], 403)
            # A public Host still requires current pairing when the forwarding header is absent.
            with mock.patch.object(m, "_portal_api_request", return_value={"ok": False}):
                self.assertEqual(self.request(headers={"Cf-Connecting-Ip": ""})[0], 404)

    def test_logout_retires_before_network_and_stale_handler_cannot_dispatch(self):
        calls = []
        def api(path, **kwargs):
            calls.append(path)
            if path == "/api/devices/unpair":
                self.assertFalse(m._account_config_file().exists())
                state = json.loads(m._tunnel_config_file().read_text())
                self.assertFalse(state["remote_access_enabled"])
                self.assertEqual(state["retired_remote_token"], self.route)
                return {"ok": True}
            return self.pairing
        with mock.patch.object(m, "_portal_api_request", side_effect=api), mock.patch.object(m, "handle_tool_call", return_value={"ok": True}) as dispatch:
            self.assertEqual(self.request()[0], 200)
            result = m.account_logout()
            self.assertTrue(result["logged_out"])
            self.assertTrue(result["revoked"])
            self.assertTrue(result["tunnel_stopped"])
            self.assertEqual(self.request()[0], 403)  # Public host retired from the allowlist.
            self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(calls, ["/api/devices/authorize", "/api/devices/unpair"])

    def test_local_loopback_control_remains_separate_and_does_not_use_account_api(self):
        m._account_config_file().unlink()
        with mock.patch.object(m, "_portal_api_request") as api, mock.patch.object(m, "handle_tool_call", return_value={"ok": True}):
            self.assertEqual(self.request(remote=False)[0], 200)
            api.assert_not_called()

    def test_remote_get_and_options_are_also_authorized(self):
        with mock.patch.object(m, "_portal_api_request", return_value=self.pairing) as api:
            self.assertEqual(self.request(method="GET")[0], 200)
            self.assertEqual(self.request(method="OPTIONS")[0], 404)
            self.assertEqual(api.call_count, 1)
        with mock.patch.object(m, "_portal_api_request", return_value={"ok": False}):
            self.assertEqual(self.request(method="GET")[0], 404)
            self.assertEqual(self.request(method="OPTIONS")[0], 404)

    def test_static_capability_without_a_gateway_ticket_is_denied_before_introspection(self):
        for value in ("", "bad-ticket", "sc_pct_" + "z" * 48, "sc_pct_" + "a" * 49):
            with self.subTest(value=value), mock.patch.object(m, "_portal_api_request") as api, mock.patch.object(m, "handle_tool_call") as dispatch:
                self.assertEqual(self.request(headers={"X-Stealth-PC-Authorization": value})[0], 404)
                api.assert_not_called()
                dispatch.assert_not_called()

    def test_ticket_is_bound_to_raw_body_and_remote_subpath(self):
        with mock.patch.object(m, "_portal_api_request", return_value=self.pairing) as api, mock.patch.object(m, "handle_tool_call", return_value={"ok": True}):
            self.assertEqual(self.request()[0], 200)
            payload = api.call_args.kwargs["payload"]
            self.assertEqual(payload["body_sha256"], hashlib.sha256(self.last_body).hexdigest())
            self.assertEqual(payload["path"], "/mcp")
            self.assertEqual(payload["method"], "POST")
            self.assertEqual(self.request(method="GET")[0], 200)
            self.assertEqual(api.call_args.kwargs["payload"]["body_sha256"], hashlib.sha256(b"").hexdigest())

    def test_logout_during_authorization_fails_before_dispatch(self):
        def retire(*args, **kwargs):
            m.stop_remote_tunnel()
            return self.pairing
        with mock.patch.object(m, "_portal_api_request", side_effect=retire), mock.patch.object(m, "handle_tool_call") as dispatch:
            self.assertEqual(self.request()[0], 404)
            dispatch.assert_not_called()


class RedirectTests(unittest.TestCase):
    def test_real_portal_entrypoint_refuses_all_redirects_without_forwarding_credentials_or_body(self):
        sink_requests = []
        class Sink(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                sink_requests.append(dict(self.headers))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
            do_POST = do_GET
        sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
        redirect_code = [302]
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(redirect_code[0])
                self.send_header("Location", f"http://localhost:{sink.server_port}/capture")
                self.send_header("Content-Length", "0")
                self.end_headers()
            do_POST = do_GET
        redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threads = []
        for server in (sink, redirect):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
        class LocalTransport(urllib.request.HTTPSHandler):
            def https_open(self, req):
                return self.do_open(lambda host, **opts: http.client.HTTPConnection("127.0.0.1", redirect.server_port, **opts), req)
        build = urllib.request.build_opener
        def local_opener(*handlers):
            return build(urllib.request.ProxyHandler({}), LocalTransport(), *handlers)
        try:
            with mock.patch.object(urllib.request, "build_opener", side_effect=local_opener):
                for method in ("GET", "POST"):
                    for code in (301, 302, 303, 307, 308):
                        redirect_code[0] = code
                        with self.subTest(method=method, code=code):
                            result = m._portal_api_request("/api/devices/authorize", method=method, payload={"sentinel": "SYNTHETIC_BODY"} if method == "POST" else None, token="SYNTHETIC_BEARER")
                            self.assertFalse(result["ok"])
                            self.assertEqual(result["error"], "Portal redirects are refused.")
                self.assertEqual(sink_requests, [])
        finally:
            for server in (redirect, sink):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(2)

    def test_noncanonical_or_non_https_portal_fails_before_transport(self):
        for origin in ("http://stealthcommander.pages.dev", "https://attacker.invalid", "https://stealthcommander.pages.dev@attacker.invalid"):
            with self.subTest(origin=origin), mock.patch.object(m, "PAGES_ORIGIN", origin), mock.patch.object(urllib.request, "build_opener") as opener:
                self.assertFalse(m._portal_api_request("/api/devices/me", token="SYNTHETIC")["ok"])
                opener.assert_not_called()


class TunnelStopTests(unittest.TestCase):
    def test_process_state_uses_one_verified_handle_and_confirms_exit(self):
        handle = 54321
        class Kernel:
            pass
        kernel = Kernel()
        for name in ("OpenProcess", "CloseHandle", "QueryFullProcessImageNameW", "GetProcessTimes", "WaitForSingleObject", "TerminateProcess"):
            setattr(kernel, name, mock.Mock())
        kernel.OpenProcess.return_value = handle
        kernel.QueryFullProcessImageNameW.side_effect = lambda target, flags, buffer, size: setattr(buffer, "value", r"C:\Synthetic\cloudflared.exe") or 1
        def creation(target, created, exited, kernel_time, user_time):
            created._obj.dwLowDateTime = 12345
            created._obj.dwHighDateTime = 1
            return 1
        kernel.GetProcessTimes.side_effect = creation
        kernel.WaitForSingleObject.side_effect = [258, 0]
        kernel.TerminateProcess.return_value = 1
        expected = {"pid": 1234, "exe": str(Path(r"C:\Synthetic\cloudflared.exe").resolve()), "created": (1 << 32) | 12345}
        with mock.patch.object(ctypes, "WinDLL", return_value=kernel):
            self.assertEqual(m._tunnel_process_state(1234, expected, stop=True), {"status": "stopped"})
        self.assertEqual(kernel.OpenProcess.call_count, 1)
        kernel.TerminateProcess.assert_called_once_with(handle, 0)
        kernel.CloseHandle.assert_called_once_with(handle)
        kernel.WaitForSingleObject.assert_any_call(handle, 5000)
        kernel.GetProcessTimes.assert_called_once()
        # PID reuse must not terminate the replacement process.
        kernel.TerminateProcess.reset_mock()
        kernel.WaitForSingleObject.side_effect = [258]
        with mock.patch.object(ctypes, "WinDLL", return_value=kernel):
            self.assertEqual(m._tunnel_process_state(1234, {**expected, "created": 0}, stop=True)["status"], "mismatch")
        kernel.TerminateProcess.assert_not_called()

    def test_stop_reports_unknown_mismatched_and_failed_process_without_false_success(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(m, "_tunnel_config_file", return_value=Path(tmp) / "tunnel.json"):
            identity = {"pid": 1234, "exe": r"C:\Synthetic\cloudflared.exe", "created": 12345}
            for status in ("mismatch", "unverified", "stop_failed", "stopped"):
                with self.subTest(status=status):
                    m._write_tunnel_config({"cloudflared_pid": 1234, "cloudflared_identity": identity, "public_origin": "https://synthetic.trycloudflare.com", "remote_access_enabled": True, "local_portal_url": "http://127.0.0.1:61077/" + "s" * 43})
                    with mock.patch.object(m, "_tunnel_process_state", return_value={"status": status}) as process, mock.patch.object(m.subprocess, "run") as run:
                        result = m.stop_remote_tunnel()
                        self.assertEqual(result["tunnel_stopped"], status == "stopped")
                        self.assertEqual(result["ok"], status == "stopped")
                        process.assert_called_once_with(1234, identity, stop=True)
                        run.assert_not_called()
                    saved = json.loads(m._tunnel_config_file().read_text())
                    self.assertFalse(saved["remote_access_enabled"])
                    self.assertIsNone(saved["public_origin"])
                    self.assertEqual(saved["retired_remote_token"], "s" * 43)
            m._write_tunnel_config({"cloudflared_pid": 1234})
            with mock.patch.object(m, "_tunnel_process_state") as process:
                self.assertFalse(m.stop_remote_tunnel()["tunnel_stopped"])
                process.assert_not_called()


class ToolContractTests(unittest.TestCase):
    def test_browser_schema_requires_paths_and_missing_paths_do_not_start_services(self):
        schema = next(tool for tool in m.TOOLS if tool["name"] == "start_browser")["inputSchema"]
        self.assertEqual(schema["required"], ["work_dir", "browser", "rea_cli"])
        with mock.patch.object(m, "ensure_dashboard") as dashboard, mock.patch.object(m, "_run_script") as runner:
            result = m.handle_tool_call("start_browser", {"work_dir": "SYNTHETIC"})
            self.assertFalse(result["ok"])
            self.assertIn("explicit browser and rea_cli", result["error"])
            dashboard.assert_not_called()
            runner.assert_not_called()

    def test_unsupported_native_and_browser_actions_are_explicit_before_cli(self):
        runner = mock.Mock()
        for kind in ("browser", "native"):
            for action in ("windows", "capture", "controls", "click"):
                result = m.handle_tool_call("session_action", {"kind": kind, "action": action, "session": "SYNTHETIC"}, _runner=runner)
                self.assertFalse(result["ok"])
                self.assertIn("unsupported", result["error"])
        runner.assert_not_called()


class HubStartupTests(unittest.TestCase):
    def test_actual_hub_binds_and_passes_version_probe_without_waiting_for_dashboard(self):
        ready = threading.Event()
        release_dashboard = threading.Event()
        servers = []
        def create_server(address, handler):
            server = ThreadingHTTPServer(address, handler)
            servers.append(server)
            ready.set()
            return server
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(m, "_ports_config_file", return_value=Path(tmp) / "ports.json"), mock.patch.object(m, "_load_account_config", return_value={}), mock.patch.object(m, "_port_open", return_value=False), mock.patch.object(m, "ThreadingHTTPServer", side_effect=create_server), mock.patch.object(m, "ensure_dashboard", side_effect=lambda *args: release_dashboard.wait(3)) as dashboard:
            route = "s" * 43
            thread = threading.Thread(target=m._serve_remote_hub, args=(0, route, tmp), daemon=True)
            thread.start()
            try:
                self.assertTrue(ready.wait(1), "Hub bind must not wait for optional dashboard readiness")
                server = servers[0]
                with mock.patch.object(m, "DEFAULT_HUB_PORT", server.server_port):
                    self.assertTrue(m._hub_version_matches(f"http://127.0.0.1:{server.server_port}/{route}"))
                dashboard.assert_not_called()
            finally:
                release_dashboard.set()
                if ready.wait(2):
                    servers[0].shutdown()
                    servers[0].server_close()
                thread.join(2)
            self.assertFalse(thread.is_alive())


class PortSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_denied_default_bind_uses_real_os_assigned_loopback_port_and_persists(self):
        bind = socket.socket.bind
        attempts = []
        def denied_default(sock, address):
            attempts.append(address)
            if address == ("127.0.0.1", m.DEFAULT_HUB_PORT):
                raise PermissionError(10013, "Synthetic reserved port")
            return bind(sock, address)
        with mock.patch.object(socket.socket, "bind", new=denied_default):
            selected = m._choose_loopback_port("hub")
        self.assertEqual(attempts, [("127.0.0.1", m.DEFAULT_HUB_PORT), ("127.0.0.1", 0)])
        self.assertTrue(1024 <= selected <= 65535)
        self.assertEqual(m._configured_port("hub"), selected)
        self.assertEqual(json.loads(m._ports_config_file().read_text()), {"hub": selected})

    def test_invalid_or_oversized_port_configuration_is_ignored(self):
        path = m._ports_config_file()
        path.parent.mkdir(parents=True)
        for raw in (" " * 257, "[]", '{"hub":true}', '{"hub":1023}', '{"hub":65536}', '{"host":"0.0.0.0"}'):
            with self.subTest(raw=raw[:30]):
                path.write_text(raw)
                self.assertEqual(m._load_ports(), {})

    def test_stale_dashboard_url_is_rebuilt_on_chosen_port_with_fresh_token(self):
        stale = "http://127.0.0.1:61076/" + "o" * 43
        with mock.patch.object(m, "_saved_dashboard_url", return_value=stale), mock.patch.object(m, "_choose_loopback_port", return_value=60618), mock.patch.object(m, "_fetch_dashboard_meta", side_effect=[None, {"desktops": []}]) as meta, mock.patch.object(m, "_spawn_detached") as spawn, mock.patch.object(m.time, "sleep"):
            result = m.ensure_dashboard()
        self.assertTrue(result["ok"])
        self.assertEqual(spawn.call_args.args[0][4], "60618")
        self.assertTrue(result["url"].startswith("http://127.0.0.1:60618/"))
        self.assertFalse(result["url"].endswith("o" * 43))
        self.assertEqual(meta.call_args.args[0], result["url"])

    def test_dashboard_unavailable_has_one_overall_deadline_and_short_attempts(self):
        now = [0.0]
        attempts = []
        def fetch(url, timeout):
            attempts.append(timeout)
            now[0] += timeout
            return None
        with mock.patch.object(m, "_saved_dashboard_url", return_value=None), mock.patch.object(m, "_choose_loopback_port", return_value=60618), mock.patch.object(m, "_fetch_dashboard_meta", side_effect=fetch), mock.patch.object(m, "_spawn_detached"), mock.patch.object(m.time, "monotonic", side_effect=lambda: now[0]), mock.patch.object(m.time, "sleep", side_effect=lambda delay: now.__setitem__(0, now[0] + delay)):
            result = m.ensure_dashboard()
        self.assertFalse(result["ok"])
        self.assertLessEqual(now[0], 8.001)
        self.assertTrue(attempts and all(0 < timeout <= 0.3 for timeout in attempts))

    def test_hub_unavailable_has_one_overall_deadline_and_preserves_version_checks(self):
        now = [0.0]
        attempts = []
        def probe(url, timeout):
            attempts.append(timeout)
            now[0] += timeout
            return False
        with mock.patch.object(m, "_load_account_config", return_value={}), mock.patch.object(m, "_choose_loopback_port", return_value=60618), mock.patch.object(m, "ensure_dashboard", return_value={"ok": False}), mock.patch.object(m, "_spawn_detached"), mock.patch.object(m, "_hub_version_matches", side_effect=probe), mock.patch.object(m.time, "monotonic", side_effect=lambda: now[0]), mock.patch.object(m.time, "sleep", side_effect=lambda delay: now.__setitem__(0, now[0] + delay)):
            result = m.ensure_remote_hub(enable_tunnel=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["hub_port"], 60618)
        self.assertLessEqual(now[0], 6.001)
        self.assertTrue(attempts and all(0 < timeout <= 0.5 for timeout in attempts))
        self.assertFalse(m._tunnel_config_file().exists())

    def test_verified_active_hub_is_reused_and_port_is_read_by_another_process(self):
        route = "s" * 43
        server = ThreadingHTTPServer(("127.0.0.1", 0), m._make_remote_hub_handler(route, self.root))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            local = f"http://127.0.0.1:{server.server_port}/{route}"
            m._write_tunnel_config({"local_portal_url": local})
            with mock.patch.object(m, "ensure_dashboard", return_value={"ok": False}), mock.patch.object(m, "_spawn_detached") as spawn, mock.patch.object(m.shutil, "which", return_value=None), mock.patch.object(m, "sync_device_to_account"):
                result = m.ensure_remote_hub(enable_tunnel=False)
            self.assertTrue(result["ok"])
            self.assertFalse(result["started"])
            self.assertEqual(result["local_mcp_url"], local + "/mcp")
            spawn.assert_not_called()
            source = "import stealth_commander_mcp as m; print(m._configured_port('hub'))"
            observed = subprocess.check_output([sys.executable, "-B", "-c", source], cwd=str(m.SCRIPTS_DIR), env=os.environ.copy(), text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(int(observed.strip()), server.server_port)
            self.assertTrue(m._hub_version_matches(local))
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request("GET", "/bootstrap")
            response = connection.getresponse()
            self.assertEqual(json.loads(response.read())["api_base"], local)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
