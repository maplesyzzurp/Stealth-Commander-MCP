"""Opt-in native/observer/cached-image acceptance. Never discovered as a unit test."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "stealth-ghost-delegation" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import harness as h
import stealth_commander_mcp as mcp


HANDOFF_LAUNCHER = """import subprocess
import sys
import time
from pathlib import Path

child = subprocess.Popen([sys.executable, '-B', sys.argv[1]],
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
trigger = Path(sys.argv[2])
deadline = time.monotonic() + 180
while not trigger.is_file():
    if child.poll() is not None:
        raise SystemExit(child.returncode)
    if time.monotonic() >= deadline:
        raise SystemExit('Handoff trigger did not arrive within 180 seconds.')
    time.sleep(.05)
"""


def require(value, message):
    if not value:
        raise AssertionError(message)


def cli(script, *arguments, timeout=45):
    process = subprocess.run([sys.executable, "-B", str(SCRIPTS / script), *map(str, arguments)],
                             capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    payload = json.loads(process.stdout)
    require(process.returncode == 0 and payload.get("ok") is True, payload.get("error", process.stderr))
    return payload.get("result", payload)


def tool(name, arguments):
    payload = mcp.handle_tool_call(name, arguments)
    require(payload.get("ok") is True, payload.get("error", "Tool failed"))
    return payload.get("result", payload)


def eventually(operation, seconds=25):
    until = time.monotonic() + seconds
    last = None
    while time.monotonic() < until:
        try:
            last = operation()
            if last:
                return last
        except (OSError, ValueError, KeyError, AssertionError) as error:
            last = str(error)
        time.sleep(.2)
    raise TimeoutError("Expected live evidence did not appear: " + str(last))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scope", choices=("session", "desktop"), default="desktop")
    parser.add_argument("--handoff", action="store_true",
                        help="Verify the existing observer after a neutral launcher exits, leaving its owned GUI child.")
    args = parser.parse_args()
    base = args.work_dir.resolve() / ("native-capture-" + uuid.uuid4().hex[:10])
    base.mkdir(parents=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    session = base / "native"
    observer_root = base / "observers"
    observer_root.mkdir()
    status_file = base / "dashboard.json"
    h.dependencies(False)
    h.native_modules()
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    require(pythonw.is_file(), "Use a Windows Python installation with pythonw.exe.")
    before = h.input_desktop()
    proof = {"passed": False, "checks": {}, "work_dir": str(base), "cleanup_errors": []}
    checks = proof["checks"]
    watch = None
    dashboard = None
    dashboard_created = None
    log = None
    saved_url = os.environ.get("STEALTH_DESKTOP_DASHBOARD_URL")
    started_at = time.monotonic()
    try:
        gui_demo = SCRIPTS / "examples" / "smoke_project" / "gui_demo.py"
        launch_arguments = [gui_demo]
        handoff_trigger = base / "release-launcher"
        if args.handoff:
            launcher = base / "handoff_launcher.py"
            launcher.write_text(HANDOFF_LAUNCHER, encoding="utf-8")
            launch_arguments = [launcher, gui_demo, handoff_trigger]
        state = cli("native_desktop.py", "start", "--session", session,
                    "--executable", pythonw, "--work-dir", base, "--",
                    *launch_arguments, timeout=40)
        def ready_fixture():
            current = tool("session_action", {"kind": "native", "action": "status", "session": str(session)})
            require(current["inactive"] and current["input_desktop_unchanged"], "Native isolation failed.")
            require(current["desktop"] != before, "Native desktop equals input desktop.")
            for candidate in current.get("windows", []):
                if candidate["title"] == "Harness example":
                    return current, candidate
            return None
        state, window = eventually(ready_fixture, 15)
        checks["native_launch_and_status_isolated"] = True
        require(any(process["pid"] == window["pid"] for process in state["processes"]),
                "Fixture window process is not in the owned job.")
        source_created = h.psutil.Process(window["pid"]).create_time()
        if args.handoff:
            launch_identity = state["launch"]
            launch_process = h.psutil.Process(launch_identity["pid"])
            require(launch_identity["pid"] != window["pid"] and launch_process.is_running(),
                    "Handoff fixture did not retain a distinct live launcher.")
            require(launch_process.create_time() == launch_identity["created"],
                    "Handoff launcher identity changed.")
        # The nominal interface must recognize and validate this native session.
        watch = cli("desktop_watch.py", "start", "--session", session,
                    "--scope", args.scope, "--interval", ".1",
                    "--work-dir", observer_root, timeout=50)["watch"]
        observed = tool("observer_action", {"action": "status", "watch": watch})
        require(observed["health"]["status"] == "running", "Observer did not become ready.")
        checks["observer_start_and_typed_status"] = True
        if args.handoff:
            guardian = state["guardian"]
            guardian_identity = [state["desktop"], guardian["pid"], guardian["created"], guardian["exe"]]
            require(observed["source_identity"] == guardian_identity,
                    "Native observer did not use its exact guardian as the stable anchor.")
            observer_identity = {key: observed[key] for key in
                                 ("server_pid", "server_created", "worker_pid", "worker_created")}
        log = (base / "dashboard.log").open("wb")
        dashboard = subprocess.Popen(
            [sys.executable, "-B", str(SCRIPTS / "dashboard.py"), "--watch", watch,
             "--status-file", str(status_file), "--port", "0"],
            stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        dashboard_created = h.psutil.Process(dashboard.pid).create_time()
        status = eventually(lambda: json.loads(status_file.read_text()) if status_file.is_file() else None, 15)
        require(status["pid"] == dashboard.pid and abs(status["created"] - dashboard_created) < .01,
                "Dashboard process identity mismatch.")
        os.environ["STEALTH_DESKTOP_DASHBOARD_URL"] = status["url"]

        def read_exact_image():
            meta = tool("dashboard_status", {"url": status["url"]})
            for card in meta.get("desktops", []):
                if card.get("state") != "live":
                    continue
                for reference in card.get("capture_refs", []):
                    if reference["hwnd"] != window["hwnd"]:
                        continue
                    arguments = {"desktop_id": card["id"], **reference}
                    response = mcp.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": "capture_observation", "arguments": arguments}})
                    result = response["result"]
                    if result.get("isError"):
                        continue
                    image = next(item for item in result["content"] if item["type"] == "image")
                    pixels = base64.b64decode(image["data"], validate=True)
                    provenance = result["structuredContent"]["provenance"]
                    require(image["mimeType"] == "image/png" and pixels.startswith(b"\x89PNG\r\n\x1a\n"),
                            "MCP did not return a PNG image.")
                    require(hashlib.sha256(pixels).hexdigest() == reference["sha256"] == provenance["sha256"],
                            "Published and returned image hashes differ.")
                    require(provenance["capture_sequence"] == reference["capture_sequence"],
                            "Exact image sequence differs.")
                    return arguments, pixels, provenance
            return None

        reference, pixels, provenance = eventually(read_exact_image, 25)
        if args.handoff:
            before_path = args.output.with_name(args.output.stem + "-before.png")
            before_path.write_bytes(pixels)
            before_capture = {"path": str(before_path), **provenance}
            require(provenance["pid"] == window["pid"] and provenance["process_created"] == source_created,
                    "Before-handoff frame does not belong to the exact owned child.")
            checks["actual_published_png_before_handoff"] = True
            handoff_trigger.write_text("Observer and exact child frame are ready.\n", encoding="utf-8")

            def completed_handoff():
                current, child_window = ready_fixture() or (None, None)
                if current is None or launch_process.is_running():
                    return None
                require(not any(process["pid"] == launch_identity["pid"]
                                and process["created"] == launch_identity["created"]
                                for process in current["processes"]),
                        "Exited launcher is still in the native job snapshot.")
                require(child_window["hwnd"] == window["hwnd"] and child_window["pid"] == window["pid"],
                        "Handoff replaced the owned child window.")
                require(h.psutil.Process(window["pid"]).create_time() == source_created,
                        "Owned child process identity changed during handoff.")
                current_observer = tool("observer_action", {"action": "status", "watch": watch})
                require(current_observer["health"]["status"] == "running",
                        "The existing observer stopped during handoff.")
                require(current_observer["source_identity"] == guardian_identity
                        and all(current_observer[key] == value for key, value in observer_identity.items()),
                        "Handoff changed the guardian anchor or replaced the observer.")
                return current

            eventually(completed_handoff, 15)
            handoff_confirmed_at = time.time()
            checks["native_owned_child_survives_launcher_exit"] = True
            checks["observer_guardian_identity_stable_after_handoff"] = True

            def read_after_handoff():
                current = read_exact_image()
                if current is None:
                    return None
                _, _, frame = current
                require(frame["captured_at"] >= handoff_confirmed_at
                        and frame["capture_sequence"] > before_capture["capture_sequence"],
                        "Observer has not published a new frame after launcher exit.")
                require(frame["pid"] == window["pid"] and frame["process_created"] == source_created,
                        "After-handoff frame does not belong to the exact owned child.")
                return current

            reference, pixels, provenance = eventually(read_after_handoff, 25)
            checks["actual_published_png_after_handoff"] = True
            proof["handoff"] = {"scope": args.scope, "launcher": launch_identity,
                                "guardian": guardian, "child_pid": window["pid"],
                                "before_capture": before_capture, "confirmed_at": handoff_confirmed_at}
        require(provenance.get("recorded_method") in ("D3D11", "PrintWindow"), "Unknown capture backend.")
        image_path = args.output.with_suffix(".png")
        image_path.write_bytes(pixels)
        proof["capture"] = {"path": str(image_path), "sha256": provenance["sha256"],
                            "capture_sequence": provenance["capture_sequence"], "recorded_method": provenance["recorded_method"]}
        checks["actual_published_png_over_mcp_with_exact_hash_sequence"] = True
        wrong = mcp.handle_tool_call("capture_observation", {**reference, "sha256": "0" * 64})
        require(wrong.get("ok") is False, "Mismatched exact frame was accepted.")
        checks["mismatched_frame_rejected"] = True
        stopped = tool("observer_action", {"action": "stop", "watch": watch})
        require(stopped["health"]["status"] == "stopped"
                and stopped["health"].get("cleanup", {}).get("active_processes") == 0,
                "Observer cleanup did not confirm an empty owned job.")
        watch = None
        checks["observer_typed_stop_and_empty_job"] = True
        state = tool("session_action", {"kind": "native", "action": "status", "session": str(session)})
        require(state["inactive"] and any(process["pid"] == window["pid"] for process in state["processes"]),
                "Stopping observer affected native source.")
        require(abs(h.psutil.Process(window["pid"]).create_time() - source_created) < .01,
                "Native source process identity changed.")
        checks["observer_stop_preserves_source"] = True
        stopped = tool("session_action", {"kind": "native", "action": "stop", "session": str(session)})
        require(stopped.get("status") == "stopped" and stopped.get("cleanup", {}).get("active_processes") == 0,
                "Native stop did not confirm an empty owned job.")
        checks["native_typed_stop_and_empty_job"] = True
        require(h.input_desktop() == before, "Input desktop changed.")
        checks["input_desktop_unchanged"] = True
        proof["passed"] = True
    except Exception as error:
        proof["error"] = f"{type(error).__name__}: {error}"
    finally:
        # The unique test root contains only this run's state. No broad process cleanup.
        watches = [Path(watch)] if watch else []
        for candidate in observer_root.glob("watch-*"):
            if candidate not in watches and (candidate / "browser-session.json").is_file():
                state = json.loads((candidate / "browser-session.json").read_text())
                if state.get("status") not in ("stopped", "cleanup_failed"):
                    watches.append(candidate)
        for candidate in watches:
            try:
                tool("observer_action", {"action": "stop", "watch": str(candidate)})
            except Exception as error:
                proof["cleanup_errors"].append("Observer: " + str(error))
        if (session / "native-session.json").is_file():
            try:
                state = json.loads((session / "native-session.json").read_text())
                if state.get("status") != "stopped":
                    tool("session_action", {"kind": "native", "action": "stop",
                                           "session": str(session), "force_owned_job": True})
            except Exception as error:
                proof["cleanup_errors"].append("Native: " + str(error))
        if dashboard is not None and dashboard.poll() is None:
            try:
                process = h.psutil.Process(dashboard.pid)
                require(abs(process.create_time() - dashboard_created) < .01,
                        "Dashboard PID creation identity changed; no process targeted.")
                dashboard.terminate()
                dashboard.wait(timeout=5)
            except Exception as error:
                proof["cleanup_errors"].append("Dashboard: " + str(error))
        if log:
            log.close()
        if saved_url is None:
            os.environ.pop("STEALTH_DESKTOP_DASHBOARD_URL", None)
        else:
            os.environ["STEALTH_DESKTOP_DASHBOARD_URL"] = saved_url
        try:
            require(h.input_desktop() == before, "Final input desktop differs.")
        except Exception as error:
            proof["cleanup_errors"].append(str(error))
        proof["elapsed_seconds"] = round(time.monotonic() - started_at, 2)
        proof["passed"] = proof["passed"] and not proof["cleanup_errors"]
        args.output.write_text(json.dumps(proof, indent=2), encoding="utf-8")
    print(json.dumps(proof, indent=2))
    return 0 if proof["passed"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
