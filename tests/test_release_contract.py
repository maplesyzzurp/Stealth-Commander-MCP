"""Validate stdio discovery and guarded dispatch without GUI/network launches.

Run: python -B -m unittest discover -s tests -p test_release_contract.py -v
Only real command children print a marker or exit with a known failure.
Live native/browser/PyCharm isolation and cleanup are separate acceptance gates.
"""
import ast
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "stealth-ghost-delegation"
SERVER = SKILL / "scripts" / "stealth_commander_mcp.py"
EXPECTED_TOOLS = {
    "capture_observation", "doctor", "dashboard_status", "list_hidden_desktops",
    "start_native_app", "start_browser", "start_pycharm", "session_action",
    "start_observer", "observer_action", "run_command", "start_tunnel",
}

def call_message(request_id, name, arguments):
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}

def stdio_roundtrip(messages):
    """Close stdin after a bounded batch; never leave a server process running."""
    with tempfile.TemporaryDirectory(prefix="stealth-contract-") as directory:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
        env["LOCALAPPDATA"] = directory
        result = subprocess.run(
            [sys.executable, "-B", str(SERVER), "serve"],
            input="".join(json.dumps(message) + "\n" for message in messages),
            text=True, encoding="utf-8", capture_output=True, timeout=20,
            cwd=directory, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    if result.returncode:
        raise AssertionError(f"stdio server exited {result.returncode}: {result.stderr}")
    replies = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    if len(replies) != sum("id" in message for message in messages):
        raise AssertionError("Missing responses or non-protocol stdout: " + result.stdout)
    return {reply["id"]: reply for reply in replies}

def text_payload(response):
    return json.loads(response["result"]["content"][0]["text"])

class StdioReleaseContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.replies = stdio_roundtrip([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "release-test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            call_message(3, "run_command", {"argv": [sys.executable, "-c", "print('STEALTH_RELEASE_OK')"]}),
            call_message(4, "run_command", {"argv": [sys.executable, "-c", "import sys; sys.stderr.write('EXPECTED_FAILURE'); sys.exit(7)"]}),
            call_message(5, "session_action", {"kind": "native", "action": "status"}),
            call_message(6, "session_action", {"kind": "native", "action": "capture", "session": "unused"}),
            call_message(7, "not_a_tool", {}),
            call_message(8, "run_command", []),
        ])
        cls.tools = {tool["name"]: tool for tool in cls.replies[2]["result"]["tools"]}

    def test_initialization_matches_package_version(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8-sig"))
        info = self.replies[1]["result"]
        self.assertEqual(info["protocolVersion"], "2025-06-18")
        self.assertEqual(info["serverInfo"]["name"], "Stealth Commander")
        self.assertEqual(info["serverInfo"]["version"], package["version"])

    def test_tool_inventory_and_root_argument_contracts(self):
        self.assertEqual(set(self.tools), EXPECTED_TOOLS)
        for name, tool in self.tools.items():
            with self.subTest(tool=name):
                schema = tool["inputSchema"]
                self.assertEqual(schema["type"], "object")
                self.assertIsInstance(schema.get("properties"), dict)
                self.assertTrue(set(schema.get("required", [])).issubset(schema["properties"]))
                self.assertFalse({"oneOf", "anyOf", "allOf"}.intersection(schema),
                                 "Composed root schemas can lose shared fields during host normalization")
                for annotation in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
                    self.assertIsInstance(tool["annotations"][annotation], bool)

    def test_session_and_observer_actions_are_discoverable(self):
        schema = self.tools["session_action"]["inputSchema"]
        self.assertEqual(set(schema["required"]), {"kind", "action", "session"})
        self.assertTrue({"x", "y", "ide_action", "name", "path", "selected", "scenario_file"}.issubset(schema["properties"]))
        self.assertIn("scenario", schema["properties"]["action"]["enum"])
        observer = self.tools["observer_action"]["inputSchema"]
        self.assertEqual(set(observer["required"]), {"action", "watch"})
        self.assertEqual(set(observer["properties"]["action"]["enum"]), {"status", "stop"})

    def test_every_tool_is_documented(self):
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8-sig")
        capabilities = (SKILL / "references" / "CAPABILITIES.md").read_text(encoding="utf-8-sig")
        for name in self.tools:
            with self.subTest(tool=name):
                self.assertIn(chr(96) + name + chr(96), skill)
                self.assertIn(chr(96) + name + chr(96), capabilities)

    def test_command_output_and_exit_status_cross_stdio(self):
        success = text_payload(self.replies[3])
        self.assertFalse(self.replies[3]["result"]["isError"])
        self.assertTrue(success["ok"])
        self.assertEqual(success["returncode"], 0)
        self.assertEqual(success["stdout"].strip(), "STEALTH_RELEASE_OK")
        self.assertEqual(success["stderr"], "")
        failure = text_payload(self.replies[4])
        self.assertTrue(self.replies[4]["result"]["isError"])
        self.assertFalse(failure["ok"])
        self.assertEqual(failure["returncode"], 7)
        self.assertEqual(failure["stderr"], "EXPECTED_FAILURE")

    def test_invalid_calls_return_errors_without_launch(self):
        for request_id in (5, 6, 7):
            with self.subTest(request=request_id):
                response = self.replies[request_id]
                self.assertTrue(response["result"]["isError"])
                self.assertFalse(text_payload(response)["ok"])
                self.assertTrue(text_payload(response).get("error"))
        self.assertIn("session", text_payload(self.replies[5])["error"].lower())
        self.assertEqual(self.replies[8]["error"]["code"], -32602)

    def test_shell_description_does_not_promise_gui_isolation(self):
        description = self.tools["run_command"]["description"].lower()
        self.assertNotIn("without touching the user's active desktop", description)
        self.assertIn("account", description)
        self.assertIn("gui", description)

class GuardedDispatchContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reviewed module imports stdlib and declares functions/data only.
        spec = importlib.util.spec_from_file_location("stealth_release_contract", SERVER)
        cls.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server)

    def test_all_declared_tools_have_a_dispatch_branch(self):
        tree = ast.parse(SERVER.read_text(encoding="utf-8-sig"))
        dispatch = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "handle_tool_call")
        branches = set()
        for node in ast.walk(dispatch):
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "name":
                for value in ast.walk(node):
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        branches.add(value.value)
        self.assertTrue(EXPECTED_TOOLS.issubset(branches), EXPECTED_TOOLS - branches)

    def test_missing_required_arguments_do_not_reach_side_effects(self):
        server = self.server
        tools = {tool["name"]: tool for tool in server.TOOLS}
        for name, tool in tools.items():
            if not tool["inputSchema"].get("required"):
                continue
            with self.subTest(tool=name), ExitStack() as guards:
                sinks = [guards.enter_context(mock.patch.object(server, target, side_effect=AssertionError("Unexpected side effect"))) for target in
                         ("ensure_dashboard", "ensure_remote_hub", "_read_observability_frame", "_record_usage_event")]
                process = guards.enter_context(mock.patch.object(server.subprocess, "run", side_effect=AssertionError("Unexpected process")))
                runner = mock.Mock(side_effect=AssertionError("Unexpected adapter call"))
                response = server.handle_rpc(call_message(1, name, {}), _runner=runner)
                self.assertTrue(response["result"]["isError"])
                runner.assert_not_called()
                process.assert_not_called()
                for sink in sinks:
                    sink.assert_not_called()

if __name__ == "__main__":
    unittest.main()
