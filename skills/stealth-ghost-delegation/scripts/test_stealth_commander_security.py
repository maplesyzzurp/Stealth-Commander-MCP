import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error

import stealth_commander_mcp as mcp


class HubSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "tunnel.json"
        for name, value in (
            ("_tunnel_config_file", self.config),
            ("_account_config_file", self.root / "account.json"),
            ("_hub_state_file", self.root / "hub-state.json"),
        ):
            patcher = mock.patch.object(mcp, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.route = "synthetic-security-test-route-00000000"
        self.server = mcp.ThreadingHTTPServer(("127.0.0.1", 0), mcp._make_remote_hub_handler(self.route, self.root))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            conn.close()

    def post(self, data, headers=None, path=None):
        return self.request("POST", path or f"/{self.route}/mcp", json.dumps(data), {"Content-Type": "application/json", **(headers or {})})

    def test_bootstrap_is_same_origin_loopback_and_never_returns_account_credentials(self):
        with mock.patch.object(mcp, "_load_account_config", return_value={"token": "SYNTHETIC_ACCOUNT_SECRET"}) as load_account:
            status, _, data = self.request("GET", "/bootstrap")
            self.assertEqual(status, 200)
            self.assertIn("api_base", data)
            self.assertNotIn("account_token", data)
            load_account.assert_not_called()
        for origin in (mcp.PAGES_ORIGIN, "https://attacker.trycloudflare.com", "https://attacker.openai.com"):
            with self.subTest(origin=origin):
                self.assertEqual(self.request("GET", "/bootstrap", headers={"Origin": origin})[0], 403)
        self.assertEqual(self.request("GET", "/bootstrap", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        own_origin = f"http://127.0.0.1:{self.server.server_port}"
        self.assertEqual(self.request("GET", "/bootstrap", headers={"Origin": own_origin, "Sec-Fetch-Site": "same-origin"})[0], 200)

    def test_POST_enforces_origins_and_hosts_before_dispatch(self):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        with mock.patch.object(mcp, "handle_rpc") as dispatch:
            self.assertEqual(self.post(payload, {"Origin": "https://disallowed.invalid"})[0], 403)
            self.assertEqual(self.post(payload, {"Host": "attacker.invalid", "Cf-Connecting-Ip": "127.0.0.1"})[0], 403)
            dispatch.assert_not_called()
        self.assertEqual(self.post(payload, {"Origin": mcp.PAGES_ORIGIN})[0], 200)

    def test_only_the_configured_tunnel_origin_is_allowed(self):
        origin = "https://synthetic-device.trycloudflare.com"
        self.config.write_text(json.dumps({"public_origin": origin}), encoding="utf-8")
        payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        self.assertEqual(self.post(payload, {"Origin": origin, "Host": "synthetic-device.trycloudflare.com", "Cf-Connecting-Ip": "203.0.113.1"})[0], 403)
        self.assertEqual(self.post(payload, {"Origin": "https://other-device.trycloudflare.com"})[0], 403)

    def test_POST_auth_failures_are_throttled(self):
        for _ in range(15):
            self.assertEqual(self.request("POST", "/wrong/mcp", None, {"Content-Type": "application/json"})[0], 404)
        self.assertEqual(self.post({"method": "initialize"})[0], 429)

    def test_invalid_bodies_return_bounded_HTTP_errors(self):
        path = f"/{self.route}/mcp"
        for length, expected in (("-1", 400), ("invalid", 400), ("9" * 5000, 400), (str(2 * 1024 * 1024 + 1), 413)):
            with self.subTest(length=length[:20]):
                self.assertEqual(self.request("POST", path, "", {"Content-Type": "application/json", "Content-Length": length})[0], expected)
        self.assertEqual(self.request("POST", path, "{}", {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", path, "{", {"Content-Type": "application/json"})[0], 400)
        for value in (None, "string", 1, [], [{}] * 101, [None]):
            with self.subTest(value=str(value)[:30]):
                self.assertEqual(self.post(value)[0], 400)
        status, _, data = self.post({"id": 1, "method": "tools/call", "params": {"name": "run_command", "arguments": []}})
        self.assertEqual(status, 200)
        self.assertEqual(data["error"]["code"], -32602)

    def test_legacy_account_save_cannot_persist_account_credentials(self):
        with mock.patch.object(mcp, "_portal_api_request") as api:
            status, _, data = self.post({"kind": "account_save", "user": {"email": "owner@example.invalid", "api_key": "SYNTHETIC_OWNER_KEY", "devices": [{"device_id": "other-pc"}]}, "token": "SYNTHETIC_OWNER_SESSION"}, path=f"/{self.route}/action")
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertFalse((self.root / "account.json").exists())
        api.assert_not_called()


class ClientSecurityTests(unittest.TestCase):
    def test_certificate_failure_is_not_retried_with_verification_disabled(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError("CERTIFICATE_VERIFY_FAILED")
        with mock.patch("urllib.request.build_opener", return_value=opener):
            self.assertFalse(mcp._portal_api_request("/api/auth/login", "POST", {"password": "SYNTHETIC"})["ok"])
            self.assertEqual(opener.open.call_count, 1)

    def test_tool_annotations_are_boolean_and_describe_command_risk(self):
        for tool in mcp.TOOLS:
            self.assertEqual(set(tool["annotations"]), {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"})
            self.assertTrue(all(type(value) is bool for value in tool["annotations"].values()))
        command = next(tool for tool in mcp.TOOLS if tool["name"] == "run_command")
        self.assertFalse(command["annotations"]["readOnlyHint"])
        self.assertTrue(command["annotations"]["destructiveHint"])

    def test_setup_only_enables_new_tunnel_with_explicit_flag(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"APPDATA": tmp, "LOCALAPPDATA": tmp}), mock.patch.object(Path, "home", return_value=Path(tmp)), mock.patch.object(mcp.shutil, "copytree"), mock.patch.object(mcp.subprocess, "run"), mock.patch.object(mcp, "sync_device_to_account"), mock.patch.object(mcp, "_load_account_config", return_value={"email": "owner@example.invalid", "device_id": "synthetic-pc", "device_token": "sc_dev_" + "d" * 48}), mock.patch.object(mcp, "ensure_remote_hub", return_value={"ok": True}) as hub, mock.patch("install_local.install", return_value={"skills_installed": [], "mcp_configs_updated": []}):
            mcp.setup_environment()
            hub.assert_called_with(enable_tunnel=False)
            mcp.setup_environment({"enable_remote": True})
            hub.assert_called_with(enable_tunnel=True)
            mcp.setup_environment({"enable_remote": "false"})
            hub.assert_called_with(enable_tunnel=False)

    def test_local_setup_does_not_download_or_launch_cloudflared(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(mcp, "_account_config_file", return_value=Path(tmp) / "account.json"), mock.patch.object(mcp, "_tunnel_config_file", return_value=Path(tmp) / "tunnel.json"), mock.patch.object(mcp, "_hub_version_matches", return_value=True), mock.patch.object(mcp, "ensure_dashboard", return_value={"url": "http://127.0.0.1:61076/" + "s" * 40}), mock.patch.object(mcp, "_port_open", return_value=True), mock.patch.object(mcp.shutil, "which", return_value=None), mock.patch.object(mcp, "_spawn_detached") as spawn, mock.patch("urllib.request.urlretrieve") as download, mock.patch.object(mcp, "sync_device_to_account"):
            (Path(tmp) / "tunnel.json").write_text(json.dumps({"local_portal_url": "http://127.0.0.1:61077/" + "s" * 40}), encoding="utf-8")
            result = mcp.ensure_remote_hub(enable_tunnel=False)
            self.assertIsNone(result["public_origin"])
            spawn.assert_not_called()
            download.assert_not_called()

    def test_daemons_use_the_current_distribution(self):
        expected = str(Path(mcp.__file__).resolve())
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(mcp, "_ports_config_file", return_value=Path(tmp) / "ports.json"), mock.patch.object(mcp, "_saved_dashboard_url", return_value=None), mock.patch.object(mcp, "_fetch_dashboard_meta", side_effect=[None, {"desktops": []}]), mock.patch.object(mcp, "_port_open", return_value=False), mock.patch.object(mcp, "_spawn_detached") as spawn, mock.patch.object(mcp.time, "sleep"):
            mcp.ensure_dashboard()
            self.assertEqual(spawn.call_args.args[0][2], expected)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(mcp, "_account_config_file", return_value=Path(tmp) / "account.json"), mock.patch.object(mcp, "_tunnel_config_file", return_value=Path(tmp) / "tunnel.json"), mock.patch.object(mcp, "_hub_version_matches", return_value=True), mock.patch.object(mcp, "ensure_dashboard", return_value={"url": "http://127.0.0.1:61076/" + "s" * 40}), mock.patch.object(mcp, "_port_open", return_value=False), mock.patch.object(mcp, "_spawn_detached") as spawn, mock.patch.object(mcp.time, "sleep"), mock.patch.object(mcp.shutil, "which", return_value=None), mock.patch.object(mcp, "sync_device_to_account"):
            mcp.ensure_remote_hub(enable_tunnel=False)
            self.assertEqual(spawn.call_args.args[0][2], expected)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for the portal boundary check")
    def test_portal_escapes_device_markup_and_does_not_forward_account_credentials(self):
        html = mcp.PORTAL_HTML
        helpers = html[html.index("function escapeHtml("):html.index("function openAuthModal(")]
        render = html[html.index("function renderAccountState("):html.index("async function loadAccountMe(")]
        submit = html[html.index("async function submitAuth("):html.index("async function logoutAccount(")]
        js = r"""
const vm = require('node:vm'), assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const elements=new Map(),calls=[];
const document={getElementById(id){if(!elements.has(id))elements.set(id,{value:'synthetic-value',style:{},textContent:'',innerHTML:''});return elements.get(id);}};
const context={document,apiBase:'https://untrusted.invalid/capability',authMode:'login',currentAccount:null,authBaseUrl:()=> 'https://official.example.invalid',fetch:async(url,options)=>{calls.push({url,options});return{ok:true,json:async()=>({ok:true,token:'SYNTHETIC_SESSION',user:{email:'test@example.invalid',api_key:'SYNTHETIC_KEY'}})};},setAuthToken:()=>{},closeAuthModal:()=>{},renderAccountState:()=>{},toast:()=>{},loadAccountMe:async()=>{},poll:()=>{}};
vm.createContext(context);
(async()=>{
 await vm.runInContext(input.submit+'\nsubmitAuth("inline");',context);
 assert.equal(calls.length,1);
 assert.equal(calls[0].url,'https://official.example.invalid/api/auth/login');
 vm.runInContext(input.helpers+input.render,context);
 context.renderAccountState({email:'test@example.invalid',devices:[{hostname:'<img src=x onerror="audit=1">',username:'<script>audit=1</script>',device_id:"');audit=1;//",tunnel_url:"https://device.invalid/x');audit=1;//"}]});
 const table=elements.get('acctDevsTable').innerHTML;
 assert.ok(table.includes('&lt;img'));
 assert.ok(!table.includes('<img'));
 assert.ok(!table.includes('<script>'));
 assert.ok(!table.includes("x');audit"));
 assert.equal(context.escapeHtml('<>&"\''),'&lt;&gt;&amp;&quot;&#39;');
 console.log('portal boundary checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        proc = subprocess.run(["node", "-e", js], input=json.dumps({"helpers": helpers, "render": render, "submit": submit}), capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)



if __name__ == "__main__":
    unittest.main()
