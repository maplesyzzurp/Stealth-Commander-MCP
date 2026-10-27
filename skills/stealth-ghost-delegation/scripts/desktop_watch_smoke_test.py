"""Exercise the read-only multi-window watcher beside an isolated PyCharm session."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent
HARNESS = ROOT / "harness.py"
WATCHER = ROOT / "desktop_watch.py"


def run_json(script: Path, *args: object, timeout: int = 120) -> dict:
    completed = subprocess.run(
        [sys.executable, "-B", str(script), *(str(arg) for arg in args)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"{script.name} did not return JSON (exit {completed.returncode}): "
            f"{completed.stdout[-1200:]} {completed.stderr[-1200:]}"
        ) from error
    if completed.returncode or not data.get("ok"):
        raise RuntimeError(data.get("error") or completed.stderr or f"{script.name} failed")
    return data["result"]


def eventually(operation, predicate, seconds: int = 60):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            last = operation()
            if predicate(last):
                return last
        except Exception as error:  # transient while a capture or response is being written
            last = str(error).splitlines()[0]
        time.sleep(0.4)
    raise AssertionError(f"Expected watcher or application state did not appear: {last}")


def workspace(url: str) -> dict:
    with urlopen(url.rstrip("/") + "/workspace", timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"Workspace endpoint returned HTTP {response.status}")
        return json.load(response)


def captured_by_title(layout: dict, title: str) -> dict:
    return next(
        (item for item in layout["windows"] if item["title"] == title and item["status"] == "captured"),
        None,
    )


def make_project(project: Path) -> None:
    project.mkdir(parents=True, exist_ok=True)
    files = {
        "console_demo.py": "name = input('Enter a value: ')\nprint('Result: ' + name + '!')\n",
        "gui_demo.py": (
            "import sys\n"
            "from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget\n\n"
            "app = QApplication(sys.argv)\n"
            "window = QWidget()\n"
            "window.setWindowTitle('Harness example')\n"
            "layout = QVBoxLayout(window)\n"
            "value = QLineEdit()\n"
            "value.setAccessibleName('Value')\n"
            "result = QLabel('Enter a value and press Apply.')\n"
            "button = QPushButton('Apply')\n"
            "button.clicked.connect(lambda: result.setText('Result: ' + value.text() + '!'))\n"
            "for control in (value, button, result):\n"
            "    layout.addWidget(control)\n"
            "window.resize(420, 180)\n"
            "window.show()\n"
            "sys.exit(app.exec())\n"
        ),
    }
    for name, content in files.items():
        path = project / name
        if path.exists() and path.read_text(encoding="utf-8") != content:
            raise RuntimeError(f"Refusing to overwrite a different project file: {path}")
        if not path.exists():
            path.write_text(content, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, help="Neutral test project; defaults to WORK_DIR/test-project.")
    parser.add_argument("--work-dir", type=Path, required=True, help="Writable test directory with space for IDE caches.")
    parser.add_argument("--proof-dir", type=Path, help="Evidence directory; defaults to WORK_DIR/proof.")
    parser.add_argument("--pycharm", type=Path, help="Optional PyCharm installation; otherwise use harness discovery.")
    parser.add_argument("--hold-seconds", type=float, default=0,
                        help="Keep the tested source and watcher alive this long before cleanup (0-120).")
    args = parser.parse_args()
    if os.name != "nt":
        raise RuntimeError("This integration test requires Windows.")
    if not 0 <= args.hold_seconds <= 120:
        raise ValueError("--hold-seconds must be between 0 and 120.")

    args.project = args.project or args.work_dir / "test-project"
    args.proof_dir = args.proof_dir or args.work_dir / "proof"
    make_project(args.project)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    args.proof_dir.mkdir(parents=True, exist_ok=True)
    proof_path = args.proof_dir / "desktop-watch-smoke.json"
    proof = {
        "test": "PyCharm private desktop multi-window watcher integration",
        "scope": "captured owned windows and their reconstructed layout, not the full raw desktop",
        "project": str(args.project.resolve()),
        "work_dir": str(args.work_dir.resolve()),
        "checks": {},
    }
    session = None
    watch = None
    try:
        session = run_json(
            HARNESS,
            "start",
            "--project",
            args.project.resolve(),
            "--python",
            sys.executable,
            *(["--pycharm", args.pycharm.resolve()] if args.pycharm else []),
            "--work-dir",
            args.work_dir.resolve(),
            "--run",
            "Console example=console_demo.py",
            "--run",
            "GUI example=gui_demo.py",
            timeout=180,
        )
        session_dir = Path(session["session"])
        proof["source_session"] = str(session_dir)
        source_state = run_json(HARNESS, "status", "--session", session_dir)
        if not source_state.get("alive") or source_state["desktop"] == source_state["input_desktop"]:
            raise AssertionError("PyCharm source is not confirmed alive on its isolated desktop.")
        proof["checks"]["source_alive_on_separate_desktop_before_watch"] = True

        watch = run_json(
            WATCHER,
            "start",
            "--session",
            session_dir,
            "--work-dir",
            args.work_dir.resolve(),
            "--interval",
            "0.5",
            timeout=70,
        )
        watch_dir = Path(watch["watch"])
        proof["watch_session"] = str(watch_dir)
        proof["viewer_url"] = watch["url"]
        print(json.dumps({"live_viewer_url": watch["url"], "source_session": str(session_dir),
                          "watch_session": str(watch_dir)}, separators=(",", ":")), flush=True)

        def ready_layout():
            layout = workspace(watch["url"])
            captured = [item for item in layout["windows"] if item["status"] == "captured"]
            if captured and all(len(item.get("sha256", "")) == 64 for item in captured):
                return layout
            return None

        before = eventually(lambda: ready_layout(), lambda value: value is not None, seconds=60)
        proof["checks"]["workspace_endpoint_has_hashed_owned_window"] = True
        proof["initial_layout"] = {
            "complete": before["complete"],
            "bounds": before["bounds"],
            "window_count": len(before["windows"]),
            "captured_windows": [
                {key: item[key] for key in ("hwnd", "pid", "title", "rect", "z_index", "sha256")}
                for item in before["windows"]
                if item["status"] == "captured"
            ],
        }
        def harness(*parts):
            return run_json(HARNESS, *parts, "--session", session_dir)

        def ide(action, *parts):
            return harness("ide", "--action", action, *parts)

        ide("open", "--path", session_dir / "project" / "gui_demo.py")
        ide("select", "--name", "GUI example")
        ide("run")
        windows = eventually(
            lambda: harness("windows"),
            lambda items: any(item["title"] == "Harness example" and item["kind"] == "application" for item in items),
        )
        gui = next(item for item in windows if item["title"] == "Harness example" and item["kind"] == "application")
        gui_before = eventually(
            lambda: (lambda layout: (layout, captured_by_title(layout, "Harness example")))(workspace(watch["url"])),
            lambda value: value[1] is not None,
            seconds=45,
        )
        gui_before_layout, gui_before_window = gui_before
        distinct_rects = {tuple(item["rect"]) for item in gui_before_layout["windows"] if item["status"] == "captured"}
        if len(distinct_rects) < 2:
            raise AssertionError("Expected at least two captured owned windows at distinct positions while the GUI app was open.")
        proof["checks"]["at_least_two_distinct_owned_window_positions"] = True
        proof["checks"]["workspace_endpoint_has_two_hashed_owned_windows"] = True
        proof["gui_window_before_action"] = {
            key: gui_before_window[key] for key in ("hwnd", "pid", "title", "rect", "z_index", "sha256")
        }
        controls = harness("controls", "--hwnd", gui["hwnd"])
        field = next(item for item in controls if item["class"] == "QLineEdit" and item["name"] == "Value")
        harness("value", "--hwnd", gui["hwnd"], "--index", field["index"], "--label", "Value", "--text", "watcher")
        controls = harness("controls", "--hwnd", gui["hwnd"])
        button = next(item for item in controls if item["class"] == "QPushButton" and item["name"] == "Apply")
        harness("invoke", "--hwnd", gui["hwnd"], "--index", button["index"], "--label", "Apply")
        eventually(lambda: harness("controls", "--hwnd", gui["hwnd"]), lambda items: any(item["name"] == "Result: watcher!" for item in items))

        def gui_frame_changed():
            layout = workspace(watch["url"])
            item = captured_by_title(layout, "Harness example")
            if item and item.get("sha256") != gui_before_window.get("sha256"):
                return layout, item
            return None

        gui_change = eventually(gui_frame_changed, lambda value: value is not None, seconds=45)
        changed_layout, changed_window = gui_change
        proof["checks"]["gui_value_invoke_completed"] = True
        proof["checks"]["gui_printwindow_hash_changed_after_action"] = True
        proof["gui_changed_window"] = {
            key: changed_window[key] for key in ("hwnd", "pid", "title", "rect", "z_index", "sha256")
        }
        proof["gui_layout_after_action"] = {
            "captured_at": changed_layout["captured_at"],
            "complete": changed_layout["complete"],
            "window_count": len(changed_layout["windows"]),
            "captured_windows": [
                {key: item[key] for key in ("hwnd", "pid", "title", "rect", "z_index", "sha256")}
                for item in changed_layout["windows"]
                if item["status"] == "captured"
            ],
        }

        ide("open", "--path", session_dir / "project" / "console_demo.py")
        ide("select", "--name", "Console example")
        ide("run")
        eventually(lambda: ide("content", "--name", "Console example"), lambda text: "Enter a value:" in text)
        ide("input", "--text", "watcher-console")
        console_output = eventually(
            lambda: ide("content", "--name", "Console example"),
            lambda text: "Result: watcher-console!" in text and "Exit code: 0" in text,
        )
        (args.proof_dir / "console-output.txt").write_text(console_output, encoding="utf-8")
        proof["checks"]["console_run_completed_while_watcher_alive"] = True
        proof["checks"]["watcher_alive_after_both_gui_and_console"] = run_json(WATCHER, "status", "--watch", watch_dir)["health"]["status"] == "running"
        if args.hold_seconds:
            print(f"holding live tested viewer for {args.hold_seconds:g} seconds", flush=True)
            time.sleep(args.hold_seconds)

        stopped_watch = run_json(WATCHER, "stop", "--watch", watch_dir)
        if stopped_watch["health"]["status"] != "stopped" or stopped_watch["health"].get("cleanup", {}).get("active_processes") != 0:
            raise AssertionError("Watcher stop did not confirm zero owned job processes.")
        proof["checks"]["watcher_stopped_with_job_empty"] = True
        source_after = run_json(HARNESS, "status", "--session", session_dir)
        if not source_after.get("alive"):
            raise AssertionError("Stopping the watcher also stopped the PyCharm source session.")
        proof["checks"]["source_pycharmsession_still_alive_after_watcher_stop"] = True
        proof["final_source_status"] = {key: source_after.get(key) for key in ("status", "alive", "desktop", "input_desktop")}
        proof["passed"] = True
    except Exception as error:
        proof["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if watch:
            try:
                status = run_json(WATCHER, "status", "--watch", watch["watch"], timeout=30)
                if status.get("health", {}).get("status") == "running":
                    run_json(WATCHER, "stop", "--watch", watch["watch"], timeout=30)
            except Exception as error:
                proof["watch_cleanup_error"] = str(error)
        if session:
            try:
                proof["source_shutdown"] = run_json(HARNESS, "stop", "--session", session["session"], timeout=90)
            except Exception as error:
                proof["source_cleanup_error"] = str(error)
        proof_path.write_text(json.dumps(proof, indent=2), encoding="utf-8")
    print(json.dumps(proof, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"desktop watcher smoke test failed: {error}", file=sys.stderr)
        raise
