from contextlib import contextmanager, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import stealth_commander_mcp as mcp


@contextmanager
def synthetic_hub(version):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, "method": body["method"]})
            info = {"name": "Stealth Commander"}
            if version is not None:
                info["version"] = version
            raw = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {"serverInfo": info}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


class DevicePairingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.account_file = self.root / "account.json"
        for target, value in (("_account_config_file", self.account_file), ("_tunnel_config_file", self.root / "tunnel.json")):
            patcher = mock.patch.object(mcp, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(mcp.socket, "gethostname", return_value="SYNTHETIC-PC")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.device_token = "sc_dev_" + "d" * 48
        self.owner_key = "SYNTHETIC_OWNER_KEY"
        self.owner_session = "SYNTHETIC_OWNER_SESSION"
        self.local = {"email": "owner@example.invalid", "device_id": "synthetic-pc", "device_token": self.device_token}

    def auth_response(self):
        return {"ok": True, "token": self.owner_session, "user": {"email": "owner@example.invalid", "api_key": self.owner_key, "devices": [{"device_id": "other-pc"}]}}

    def pair_response(self):
        return {"ok": True, "device_token": self.device_token, "device": {"device_id": "synthetic-pc", "hostname": "SYNTHETIC-PC"}}

    def assert_public(self, response):
        public = json.dumps(response)
        for secret in (self.device_token, self.owner_key, self.owner_session):
            self.assertNotIn(secret, public)
        self.assertNotIn("other-pc", public)

    def test_pair_persists_only_scoped_credentials_and_redacts_response(self):
        with mock.patch.object(mcp, "_portal_api_request", side_effect=[self.auth_response(), self.pair_response()]) as api:
            response = mcp.account_login(api_key=self.owner_key)
        self.assertTrue(response["ok"])
        self.assertEqual(json.loads(self.account_file.read_text(encoding="utf-8")), self.local)
        self.assertEqual([call.args[0] for call in api.call_args_list], ["/api/auth/login", "/api/devices/pair"])
        self.assertEqual(api.call_args_list[1].kwargs["token"], self.owner_session)
        self.assertEqual(api.call_args_list[1].kwargs["payload"]["device_id"], "synthetic-pc")
        self.assert_public(response)

    def test_failed_pair_never_saves_owner_credentials(self):
        with mock.patch.object(mcp, "_portal_api_request", side_effect=[self.auth_response(), {"ok": False, "error": self.owner_key}]):
            response = mcp.account_login(email="owner@example.invalid", password="SYNTHETIC_PASSWORD")
        self.assertFalse(response["ok"])
        self.assertFalse(self.account_file.exists())
        self.assert_public(response)

    def test_pair_rejects_credential_for_different_device(self):
        paired = self.pair_response()
        paired["device"]["device_id"] = "other-pc"
        with mock.patch.object(mcp, "_portal_api_request", side_effect=[self.auth_response(), paired]):
            response = mcp.account_login(api_key=self.owner_key)
        self.assertFalse(response["ok"])
        self.assertFalse(self.account_file.exists())
        self.assert_public(response)

    def test_legacy_file_is_removed_before_authentication_even_when_pairing_fails(self):
        self.account_file.write_text(json.dumps({"email": "owner@example.invalid", "api_key": self.owner_key, "token": self.owner_session, "user": {"devices": [{"device_id": "other-pc", "mcp_url": "https://other-device.trycloudflare.com/OTHER_PC_CAPABILITY/mcp"}]}}), encoding="utf-8")
        untouched = self.root / "other-config.json"
        untouched.write_text("preserve", encoding="utf-8")

        def reject_auth(*args, **kwargs):
            self.assertFalse(self.account_file.exists())
            return {"ok": False}

        with mock.patch.object(mcp, "_portal_api_request", side_effect=reject_auth):
            response = mcp.account_login(api_key=self.owner_key)
        self.assertFalse(response["ok"])
        self.assertFalse(self.account_file.exists())
        self.assertEqual(untouched.read_text(encoding="utf-8"), "preserve")

    def test_locked_legacy_file_blocks_network_calls(self):
        self.account_file.write_text(json.dumps({"api_key": self.owner_key}), encoding="utf-8")
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("SYNTHETIC_LOCKED_FILE")), mock.patch.object(mcp, "_portal_api_request") as api:
            response = mcp.account_login(api_key=self.owner_key)
        self.assertFalse(response["ok"])
        api.assert_not_called()
        self.assertEqual(json.loads(self.account_file.read_text(encoding="utf-8"))["api_key"], self.owner_key)
        self.assert_public(response)

    def test_locked_legacy_state_blocks_remote_hub_startup(self):
        self.account_file.write_text(json.dumps({"api_key": self.owner_key}), encoding="utf-8")
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("SYNTHETIC_LOCKED_FILE")), mock.patch.object(mcp, "ensure_dashboard") as dashboard, mock.patch.object(mcp, "_spawn_detached") as spawn, mock.patch.object(mcp, "_portal_api_request") as api:
            response = mcp.ensure_remote_hub(enable_tunnel=True)
        self.assertFalse(response["ok"])
        dashboard.assert_not_called()
        spawn.assert_not_called()
        api.assert_not_called()
        self.assert_public(response)

    def test_sync_rejects_a_response_for_another_device(self):
        self.account_file.write_text(json.dumps(self.local), encoding="utf-8")
        with mock.patch.object(mcp, "_portal_api_request", return_value={"ok": True, "device": {"device_id": "other-pc", "mcp_url": "https://other-device.trycloudflare.com/OTHER_PC_CAPABILITY/mcp"}}):
            response = mcp.sync_device_to_account()
        self.assertFalse(response["ok"])
        self.assertNotIn("OTHER_PC_CAPABILITY", json.dumps(response))
        self.assert_public(response)

    def test_local_only_pairing_does_not_register_a_loopback_mcp_endpoint(self):
        (self.root / "tunnel.json").write_text(json.dumps({"mcp_url": "http://127.0.0.1:61077/LOCAL_CAPABILITY/mcp"}), encoding="utf-8")
        with mock.patch.object(mcp, "_portal_api_request", side_effect=[self.auth_response(), self.pair_response()]) as api:
            response = mcp.account_login(api_key=self.owner_key)
        self.assertTrue(response["ok"])
        self.assertIsNone(api.call_args.kwargs["payload"]["mcp_url"])
        self.assertEqual(json.loads(self.account_file.read_text(encoding="utf-8")), self.local)

    def test_sync_registers_only_an_exact_quick_tunnel_mcp_endpoint(self):
        self.account_file.write_text(json.dumps(self.local), encoding="utf-8")
        valid = "https://synthetic-device.trycloudflare.com/" + "c" * 32 + "/mcp"
        for endpoint in (valid, "http://127.0.0.1:61077/" + "c" * 32 + "/mcp", valid + "?key=SYNTHETIC", valid.replace("/mcp", "/other"), valid.replace("trycloudflare.com", "attacker.invalid")):
            with self.subTest(endpoint=endpoint), mock.patch.object(mcp, "_portal_api_request", return_value={"ok": True, "device": {"device_id": "synthetic-pc"}}) as api:
                response = mcp.sync_device_to_account({"mcp_url": endpoint})
                self.assertTrue(response["ok"])
                self.assertEqual(api.call_args.kwargs["payload"]["mcp_url"], valid if endpoint == valid else None)

    def test_sync_uses_scoped_heartbeat_and_cannot_override_own_device_id(self):
        self.account_file.write_text(json.dumps(self.local), encoding="utf-8")
        server = {"ok": True, "device": {"device_id": "synthetic-pc", "hostname": "SYNTHETIC-PC", "mcp_url": "https://synthetic.invalid/SECRET"}, "token": self.owner_session, "user": {"api_key": self.owner_key}}
        with mock.patch.object(mcp, "_portal_api_request", return_value=server) as api:
            response = mcp.sync_device_to_account({"device_id": "other-pc", "mcp_url": "https://synthetic-device.trycloudflare.com/capability/mcp"})
        self.assertTrue(response["ok"])
        api.assert_called_once()
        self.assertEqual(api.call_args.args[0], "/api/devices/heartbeat")
        self.assertEqual(api.call_args.kwargs["token"], self.device_token)
        self.assertEqual(api.call_args.kwargs["payload"]["device_id"], "synthetic-pc")
        self.assertEqual(json.loads(self.account_file.read_text(encoding="utf-8")), self.local)
        self.assertNotIn("SECRET", json.dumps(response))
        self.assert_public(response)

    def test_whoami_uses_only_device_me_and_redacts_server_extras(self):
        self.account_file.write_text(json.dumps(self.local), encoding="utf-8")
        server = {"ok": True, "device": {"device_id": "synthetic-pc", "hostname": "SYNTHETIC-PC"}, "user": {"api_key": self.owner_key, "devices": [{"device_id": "other-pc"}]}}
        with mock.patch.object(mcp, "_portal_api_request", return_value=server) as api:
            response = mcp.account_whoami()
        self.assertTrue(response["ok"])
        self.assertTrue(response["logged_in"])
        self.assertEqual(api.call_args.args[0], "/api/devices/me")
        self.assertEqual(api.call_args.kwargs["token"], self.device_token)
        self.assert_public(response)

    def test_logout_revokes_only_own_device_and_removes_local_file(self):
        self.account_file.write_text(json.dumps(self.local), encoding="utf-8")
        with mock.patch.object(mcp, "_portal_api_request", return_value={"ok": True}) as api:
            response = mcp.account_logout()
        self.assertTrue(response["ok"])
        self.assertTrue(response["logged_out"])
        self.assertFalse(self.account_file.exists())
        self.assertEqual(api.call_args.args[0], "/api/devices/unpair")
        self.assertEqual(api.call_args.kwargs["token"], self.device_token)
        self.assertEqual(api.call_args.kwargs["payload"], {"device_id": "synthetic-pc"})
        self.assert_public(response)

    def test_interactive_login_reads_hidden_key_and_prints_no_credentials(self):
        output = io.StringIO()
        with mock.patch.object(mcp.sys, "argv", ["stealth-commander", "login"]), mock.patch.object(mcp.sys, "stdin") as stdin, mock.patch.object(mcp.getpass, "getpass", return_value=self.owner_key) as prompt, mock.patch.object(mcp, "_portal_api_request", side_effect=[self.auth_response(), self.pair_response()]), redirect_stdout(output):
            stdin.isatty.return_value = True
            mcp.main()
        prompt.assert_called_once_with("Account key (hidden): ")
        response = json.loads(output.getvalue())
        self.assertTrue(response["ok"])
        self.assertEqual(json.loads(self.account_file.read_text(encoding="utf-8")), self.local)
        self.assert_public(response)

    def test_interactive_unpaired_setup_prompts_and_preserves_remote_opt_in(self):
        output = io.StringIO()
        with mock.patch.object(mcp.sys, "argv", ["stealth-commander", "setup", "--enable-remote"]), mock.patch.object(mcp.sys, "stdin") as stdin, mock.patch.object(mcp.getpass, "getpass", return_value=self.owner_key) as prompt, mock.patch.object(mcp, "setup_environment", return_value={"ok": True}) as setup, redirect_stdout(output):
            stdin.isatty.return_value = True
            mcp.main()
        prompt.assert_called_once_with("Account key (hidden): ")
        self.assertEqual(setup.call_args.args[0], {"enable_remote": True, "api_key": self.owner_key})
        self.assert_public(json.loads(output.getvalue()))

    def test_first_interactive_remote_setup_pairs_before_enabling_tunnel(self):
        output = io.StringIO()
        stages = []
        local_hub = {"ok": True, "local_portal_url": "http://127.0.0.1:61077/SYNTHETIC_LOCAL", "portal_url": "http://127.0.0.1:61077/SYNTHETIC_LOCAL"}
        remote_hub = {**local_hub, "portal_url": "https://synthetic.invalid/portal", "mcp_url": "https://synthetic.invalid/mcp", "remote_access_enabled": True}

        def hub(*, enable_tunnel):
            stages.append("remote" if enable_tunnel else "local")
            if enable_tunnel:
                self.assertEqual(json.loads(self.account_file.read_text(encoding="utf-8")), self.local)
                return remote_hub
            self.assertFalse(self.account_file.exists())
            return local_hub

        def portal(path, **kwargs):
            stages.append(path)
            return self.auth_response() if path == "/api/auth/login" else self.pair_response()

        with mock.patch.object(mcp.sys, "argv", ["stealth-commander", "setup", "--enable-remote"]), mock.patch.object(mcp.sys, "stdin") as stdin, mock.patch.object(mcp.getpass, "getpass", return_value=self.owner_key) as prompt, mock.patch("install_local.install", return_value={"skills_installed": [], "mcp_configs_updated": []}), mock.patch.object(mcp, "ensure_remote_hub", side_effect=hub), mock.patch.object(mcp, "_portal_api_request", side_effect=portal), redirect_stdout(output):
            stdin.isatty.return_value = True
            status = mcp.main()
        response = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertTrue(response["ok"])
        self.assertEqual(stages, ["local", "/api/auth/login", "/api/devices/pair", "remote"])
        self.assertEqual(response["portal_url"], remote_hub["portal_url"])
        self.assertEqual(response["mcp_url"], remote_hub["mcp_url"])
        prompt.assert_called_once_with("Account key (hidden): ")
        self.assert_public(response)

    def test_failed_setup_pairing_never_enables_remote_tunnel(self):
        local_hub = {"ok": True, "local_portal_url": "http://127.0.0.1:61077/SYNTHETIC_LOCAL"}
        with mock.patch("install_local.install", return_value={"skills_installed": [], "mcp_configs_updated": []}), mock.patch.object(mcp, "ensure_remote_hub", return_value=local_hub) as hub, mock.patch.object(mcp, "_portal_api_request", return_value={"ok": False}):
            response = mcp.setup_environment({"api_key": self.owner_key, "enable_remote": True})
        self.assertFalse(response["ok"])
        self.assertFalse(self.account_file.exists())
        hub.assert_called_once_with(enable_tunnel=False)
        self.assert_public(response)

    def test_failed_remote_start_after_pairing_returns_error_without_claiming_success(self):
        local_hub = {"ok": True, "local_portal_url": "http://127.0.0.1:61077/SYNTHETIC_LOCAL"}
        tunnel_failure = {"ok": False, "remote_access_enabled": False, "error": "Synthetic tunnel unavailable."}
        with mock.patch("install_local.install", return_value={"skills_installed": [], "mcp_configs_updated": []}), mock.patch.object(mcp, "ensure_remote_hub", side_effect=[local_hub, tunnel_failure]) as hub, mock.patch.object(mcp, "_portal_api_request", side_effect=[self.auth_response(), self.pair_response()]):
            response = mcp.setup_environment({"api_key": self.owner_key, "enable_remote": True})
        self.assertEqual(response, tunnel_failure)
        self.assertEqual(json.loads(self.account_file.read_text(encoding="utf-8")), self.local)
        self.assertEqual(hub.call_args_list, [mock.call(enable_tunnel=False), mock.call(enable_tunnel=True)])
        self.assert_public(response)

    def test_missing_remote_endpoint_after_pairing_is_not_setup_success(self):
        local_hub = {"ok": True, "local_portal_url": "http://127.0.0.1:61077/SYNTHETIC_LOCAL"}
        pending_tunnel = {**local_hub, "remote_access_enabled": False}
        with mock.patch("install_local.install", return_value={"skills_installed": [], "mcp_configs_updated": []}), mock.patch.object(mcp, "ensure_remote_hub", side_effect=[local_hub, pending_tunnel]), mock.patch.object(mcp, "_portal_api_request", side_effect=[self.auth_response(), self.pair_response()]):
            response = mcp.setup_environment({"api_key": self.owner_key, "enable_remote": True})
        self.assertFalse(response["ok"])
        self.assertFalse(response["remote_access_enabled"])
        self.assertIn("remote tunnel is not ready", response["error"])
        self.assertEqual(json.loads(self.account_file.read_text(encoding="utf-8")), self.local)
        self.assert_public(response)

    def test_noninteractive_login_requires_pairing_without_prompt_or_network(self):
        output = io.StringIO()
        with mock.patch.object(mcp.sys, "argv", ["stealth-commander", "login"]), mock.patch.object(mcp.sys, "stdin") as stdin, mock.patch.object(mcp.getpass, "getpass") as prompt, mock.patch.object(mcp, "_portal_api_request") as api, redirect_stdout(output):
            stdin.isatty.return_value = False
            mcp.main()
        response = json.loads(output.getvalue())
        self.assertFalse(response["ok"])
        self.assertTrue(response["pairing_required"])
        prompt.assert_not_called()
        api.assert_not_called()

    def test_background_unpaired_setup_never_prompts_or_starts_services(self):
        with mock.patch.object(mcp.getpass, "getpass") as prompt, mock.patch.object(mcp, "ensure_remote_hub") as hub, mock.patch.object(mcp.subprocess, "run") as execute:
            response = mcp.setup_environment({"enable_remote": True})
        self.assertFalse(response["ok"])
        self.assertTrue(response["pairing_required"])
        prompt.assert_not_called()
        hub.assert_not_called()
        execute.assert_not_called()

    def test_background_login_without_credentials_never_prompts_or_uses_network(self):
        with mock.patch.object(mcp.getpass, "getpass") as prompt, mock.patch.object(mcp, "_portal_api_request") as api:
            response = mcp.account_login()
        self.assertFalse(response["ok"])
        self.assertTrue(response["pairing_required"])
        prompt.assert_not_called()
        api.assert_not_called()

    def test_same_version_local_hub_is_reused_after_token_gated_probe(self):
        route = "c" * 40
        with synthetic_hub(mcp.SERVER_INFO["version"]) as (port, requests), mock.patch.object(mcp, "DEFAULT_HUB_PORT", port), mock.patch.object(mcp, "ensure_dashboard", return_value={"url": "http://127.0.0.1:61076/" + route}), mock.patch.object(mcp, "_spawn_detached") as spawn, mock.patch.object(mcp, "sync_device_to_account"), mock.patch.object(mcp.shutil, "which", return_value=None):
            local = f"http://127.0.0.1:{port}/{route}"
            (self.root / "tunnel.json").write_text(json.dumps({"local_portal_url": local, "public_origin": None}), encoding="utf-8")
            response = mcp.ensure_remote_hub(enable_tunnel=False)
        self.assertTrue(response["ok"])
        self.assertFalse(response["started"])
        self.assertEqual(response["mcp_url"], local + "/mcp")
        self.assertEqual(requests, [{"path": "/" + route + "/mcp", "method": "initialize"}])
        spawn.assert_not_called()

    def test_old_or_unknown_hub_blocks_setup_before_pairing_and_remote_access(self):
        route = "c" * 40
        for version in ("1.1.0", None):
            with self.subTest(version=version), synthetic_hub(version) as (port, requests), mock.patch.object(mcp, "DEFAULT_HUB_PORT", port), mock.patch.object(Path, "home", return_value=self.root), mock.patch.dict(mcp.os.environ, {"APPDATA": str(self.root), "LOCALAPPDATA": str(self.root)}), mock.patch.object(mcp.shutil, "copytree"), mock.patch.object(mcp.subprocess, "run"), mock.patch.object(mcp, "ensure_dashboard") as dashboard, mock.patch.object(mcp, "_spawn_detached") as spawn, mock.patch.object(mcp, "account_login") as pair, mock.patch("install_local.install", return_value={"skills_installed": [], "mcp_configs_updated": []}):
                local = f"http://127.0.0.1:{port}/{route}"
                (self.root / "tunnel.json").write_text(json.dumps({"local_portal_url": local, "public_origin": None}), encoding="utf-8")
                response = mcp.setup_environment({"api_key": self.owner_key, "enable_remote": True})
            self.assertFalse(response["ok"])
            self.assertTrue(response["restart_required"])
            self.assertNotIn("mcp_url", response)
            self.assertNotIn("portal_url", response)
            self.assertEqual(requests, [{"path": "/" + route + "/mcp", "method": "initialize"}])
            dashboard.assert_not_called()
            spawn.assert_not_called()
            pair.assert_not_called()


if __name__ == "__main__":
    unittest.main()
