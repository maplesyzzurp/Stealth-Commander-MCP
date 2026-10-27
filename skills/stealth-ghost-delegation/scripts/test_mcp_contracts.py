"""Regression contracts for owned MCP dispatch; no GUI, subprocess or socket work."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import harness
import stealth_commander_mcp as mcp


class McpContractTests(unittest.TestCase):
    def runner(self, script, args, timeout=120):
        self.calls.append((script, args, timeout))
        return {"ok": True}

    def setUp(self):
        self.calls = []

    def test_session_schema_keeps_required_arguments_at_root(self):
        schema = next(t for t in mcp.TOOLS if t["name"] == "session_action")["inputSchema"]
        self.assertNotIn("oneOf", schema)
        self.assertNotIn("anyOf", schema)
        self.assertEqual(set(schema["required"]), {"kind", "action", "session"})
        self.assertTrue({"session", "action", "x", "y", "ide_action", "scenario_file"} <= set(schema["properties"]))

    def test_missing_session_returns_actionable_error_without_running(self):
        result = mcp.handle_tool_call("session_action", {"kind": "native", "action": "status"}, _runner=self.runner)
        self.assertFalse(result["ok"])
        self.assertIn("session", result["error"])
        self.assertFalse(self.calls)

    def test_browser_scenario_runs_only_owned_adapter(self):
        result = mcp.handle_tool_call("session_action", {"kind": "browser", "action": "scenario", "session": "owned-session", "scenario_file": "scenario.json"}, _runner=self.runner)
        self.assertTrue(result["ok"])
        self.assertEqual(self.calls[0][:2], ("rea_bridge.py", ["scenario", "--session", "owned-session", "--file", "scenario.json"]))

    def test_browser_scenario_requires_file_and_rejects_native_scenario(self):
        for kind in ("browser", "native"):
            with self.subTest(kind=kind):
                result = mcp.handle_tool_call("session_action", {"kind": kind, "action": "scenario", "session": "owned"}, _runner=self.runner)
                self.assertFalse(result["ok"])
        self.assertFalse(self.calls)

    def test_typed_pycharm_click_and_ide_dispatch(self):
        result = mcp.handle_tool_call("session_action", {"kind": "pycharm", "action": "click", "session": "owned", "hwnd": 9, "x": 12, "y": 15}, _runner=self.runner)
        self.assertTrue(result["ok"])
        self.assertEqual(self.calls[-1][1], ["click", "--session", "owned", "--hwnd", "9", "--x", "12", "--y", "15"])
        result = mcp.handle_tool_call("session_action", {"kind": "pycharm", "action": "ide", "session": "owned", "ide_action": "toggle", "index": 2, "label": "Feature", "selected": False}, _runner=self.runner)
        self.assertTrue(result["ok"])
        self.assertIn("--action", self.calls[-1][1])
        self.assertEqual(self.calls[-1][1][-2:], ["--selected", "false"])

    def test_control_mutations_require_grounded_label_and_click_coordinates(self):
        for payload in ({"action": "invoke", "hwnd": 9, "index": 0}, {"action": "click", "hwnd": 9, "x": 1}):
            with self.subTest(payload=payload):
                result = mcp.handle_tool_call("session_action", {"kind": "pycharm", "session": "owned", **payload}, _runner=self.runner)
                self.assertFalse(result["ok"])
        self.assertFalse(self.calls)

    def test_legacy_extra_arguments_cannot_replace_session(self):
        for extra in (["--session", "other"], ["--session=other"]):
            result = mcp.handle_tool_call("session_action", {"kind": "native", "action": "stop", "session": "owned", "extra_args": extra}, _runner=self.runner)
            self.assertFalse(result["ok"])
        self.assertFalse(self.calls)

    def test_legacy_abbreviations_and_positional_commands_are_rejected(self):
        cases = [
            ("native", "stop", ["--sess", "other"]),
            ("pycharm", "ide", ["--act", "dump"]),
            ("pycharm", "capture", ["--hw", "9", "--output", "capture.png"]),
            ("native", "stop", ["stop", "--session", "other"]),
            ("browser", "scenario", ["--fil", "scenario.json"]),
        ]
        for kind, action, extra in cases:
            with self.subTest(extra=extra):
                result = mcp.handle_tool_call("session_action", {"kind": kind, "action": action, "session": "owned", "extra_args": extra}, _runner=self.runner)
                self.assertFalse(result["ok"])
        self.assertFalse(self.calls)

    def test_browser_scenario_outer_timeout_preserves_adapter_deadline(self):
        result = mcp.handle_tool_call("session_action", {"kind": "browser", "action": "scenario", "session": "owned", "scenario_file": "scenario.json"}, _runner=self.runner)
        self.assertTrue(result["ok"])
        self.assertEqual(self.calls[0][2], 210)

    def test_browser_scenario_timeout_reports_unknown_outcome(self):
        def timeout_runner(script, argv, timeout):
            raise mcp.subprocess.TimeoutExpired(argv, timeout)
        result = mcp.handle_tool_call("session_action", {"kind": "browser", "action": "scenario", "session": "owned", "scenario_file": "scenario.json"}, _runner=timeout_runner)
        self.assertFalse(result["ok"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["outcome"], "unknown")
        self.assertIn("Inspect", result["error"])

    def test_legacy_pycharm_ide_extra_args_remain_supported(self):
        result = mcp.handle_tool_call("session_action", {"kind": "pycharm", "action": "ide", "session": "owned", "extra_args": ["--action", "dump"]}, _runner=self.runner)
        self.assertTrue(result["ok"])
        self.assertEqual(self.calls[0][1], ["ide", "--session", "owned", "--action", "dump"])

    def test_observer_lifecycle_uses_watch_path(self):
        for action in ("status", "stop"):
            result = mcp.handle_tool_call("observer_action", {"action": action, "watch": "owned-watch"}, _runner=self.runner)
            self.assertTrue(result["ok"])
            self.assertEqual(self.calls[-1][:2], ("desktop_watch.py", [action, "--watch", "owned-watch"]))

    def test_invalid_observer_target_does_not_start_dashboard(self):
        for args in ({"work_dir": "work"}, {"work_dir": "work", "desktop": "owned"}, {"work_dir": "work", "session": "s", "desktop": "d", "pid": 3}, {"work_dir": "work", "session": "s", "interval": float("nan")}):
            with self.subTest(args=args), mock.patch.object(mcp, "ensure_dashboard") as dashboard, mock.patch.object(mcp.subprocess, "run", side_effect=AssertionError("Invalid input reached a subprocess")):
                result = mcp.handle_tool_call("start_observer", args)
                self.assertFalse(result["ok"])
                dashboard.assert_not_called()

    def test_portal_chrome_supplies_explicit_or_configured_paths(self):
        explicit = mcp._browser_launch_arguments("work", {"browser": "chosen-chrome", "rea_cli": "chosen-rea"})
        self.assertEqual(explicit, {"work_dir": "work", "browser": "chosen-chrome", "rea_cli": "chosen-rea"})
        with mock.patch.dict(mcp.os.environ, {"STEALTH_COMMANDER_BROWSER": "configured-chrome", "STEALTH_COMMANDER_REA_CLI": "configured-rea"}):
            configured = mcp._browser_launch_arguments("work", {})
        self.assertEqual(configured, {"work_dir": "work", "browser": "configured-chrome", "rea_cli": "configured-rea"})
        self.assertIn("browserPath", mcp.PORTAL_HTML)
        self.assertIn("reaCliPath", mcp.PORTAL_HTML)

    def test_cli_install_failure_has_nonzero_exit_status(self):
        with mock.patch.object(mcp.sys, "argv", ["stealth_commander_mcp.py", "install"]), mock.patch("install_local.install", side_effect=RuntimeError("synthetic failure")), contextlib.redirect_stdout(io.StringIO()) as output:
            status = mcp.main()
        self.assertEqual(status, 1)
        self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_cli_login_failure_has_nonzero_exit_status(self):
        with mock.patch.object(mcp.sys, "argv", ["stealth_commander_mcp.py", "login", "--email", "test@example.invalid"]), mock.patch.object(mcp, "account_login", return_value={"ok": False, "error": "synthetic denial"}), contextlib.redirect_stdout(io.StringIO()) as output:
            status = mcp.main()
        self.assertEqual(status, 1)
        self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_run_command_contract_discloses_gui_boundary(self):
        tool = next(t for t in mcp.TOOLS if t["name"] == "run_command")
        self.assertIn("does not isolate GUI", tool["description"])


class ReadinessContractTests(unittest.TestCase):
    def test_missing_ide_does_not_hide_missing_native_dependencies(self):
        with mock.patch("dependency_setup.validate_runtime", return_value={"bits": 64}), mock.patch.object(harness, "dependencies", return_value=["pywin32==311"]), mock.patch.object(harness, "discover_pycharm", side_effect=RuntimeError("PyCharm absent")):
            result = harness.inspect_environment()
        self.assertFalse(result["ready"])
        self.assertFalse(result["native_desktop_ready"])
        self.assertEqual(result["missing_packages"], ["pywin32==311"])
        self.assertFalse(result["checks"]["pycharm"]["ready"])

    def test_runtime_failure_does_not_hide_other_readiness_findings(self):
        with mock.patch("dependency_setup.validate_runtime", side_effect=RuntimeError("Requires Windows x64 Python 3.11")), mock.patch.object(harness, "dependencies", return_value=[]), mock.patch.object(harness, "discover_pycharm", return_value=Path("ide")):
            result = harness.inspect_environment()
        self.assertFalse(result["ready"])
        self.assertFalse(result["native_desktop_ready"])
        self.assertIn("Windows x64", result["checks"]["runtime"]["error"])
        self.assertTrue(result["checks"]["pycharm"]["installed"])

    def test_mcp_does_not_convert_failed_doctor_to_native_success(self):
        result = mcp.handle_tool_call("doctor", {}, _runner=lambda *_: {"ok": False, "error": "PyCharm check failed"})
        self.assertFalse(result["ok"])
        self.assertNotIn("native_desktop_ready", result)


if __name__ == "__main__":
    unittest.main()
