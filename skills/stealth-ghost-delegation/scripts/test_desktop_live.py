"""Live desktop-scope observer test on a fresh, non-input Win32 desktop."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
import urllib.request
import uuid

import harness as h


ROOT = Path(__file__).resolve().parent
WATCHER = ROOT / "desktop_watch.py"
DEFAULT_RUNTIME = Path(tempfile.gettempdir()) / "stealth-ghost-desktop-stream-tests"


def run_json(script: Path, *args: object, timeout: int = 45) -> dict:
    completed = subprocess.run(
        [sys.executable, "-B", str(script), *(str(arg) for arg in args)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{script.name} returned no JSON: {completed.stdout[-800:]} {completed.stderr[-800:]}") from error
    if completed.returncode or not result.get("ok"):
        raise RuntimeError(result.get("error") or completed.stderr or f"{script.name} failed")
    return result["result"]


def process_identity(pid: int) -> dict:
    process = h.psutil.Process(pid)
    return {"pid": pid, "created": process.create_time(), "exe": process.exe()}


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    for attempt in range(20):
        try:
            temporary.write_text(json.dumps(data), encoding="utf-8")
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.01)


def eventually(operation, predicate, timeout: float = 20):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = operation()
            if predicate(last):
                return last
        except Exception as error:
            last = f"{type(error).__name__}: {error}"
        time.sleep(.1)
    raise AssertionError(f"Expected live state did not appear before timeout: {last}")


def wait_fixture(process, state_file: Path, desktop: str):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        error_file = state_file.with_suffix(".error.txt")
        if error_file.exists():
            raise RuntimeError(error_file.read_text(encoding="utf-8"))
        if state_file.exists():
            state = json.loads(state_file.read_text(encoding="utf-8"))
            if state.get("pid") == process.pid and state.get("desktop") == desktop:
                return state
        if process.poll() is not None:
            raise RuntimeError(f"Fixture PID {process.pid} exited with code {process.poll()} before creating its window.")
        time.sleep(.1)
    raise TimeoutError(f"Fixture PID {process.pid} did not publish state: {state_file}")


def layout(url: str) -> dict:
    with urllib.request.urlopen(url.rstrip("/") + "/workspace", timeout=5) as response:
        return json.load(response)


def read_mjpeg_frame(response):
    header = bytearray()
    while not header.endswith(b"\r\n\r\n"):
        byte = response.read(1)
        if not byte:
            raise EOFError("MJPEG stream ended before a frame header.")
        header.extend(byte)
    lines = header.decode("ascii").split("\r\n")
    fields = dict(line.split(": ", 1) for line in lines if ": " in line)
    length = int(fields["Content-Length"])
    image = response.read(length)
    if len(image) != length or not image.startswith(b"\xff\xd8"):
        raise AssertionError("Stream part was not a complete JPEG frame.")
    if response.read(2) != b"\r\n":
        raise AssertionError("MJPEG part did not end with CRLF.")
    return fields, image


def fixture_stack_matches(layout_data, fixture_state, above_title=None, below_title=None):
    """Compare the same HWND subset, preserving order rather than unrelated ordinals."""
    expected = fixture_state.get("windows", [])
    if not expected:
        return False
    handles = {item["hwnd"] for item in expected}
    observed = [item for item in layout_data.get("windows", []) if item["hwnd"] in handles]
    if len(observed) != len(expected) or any(item.get("status") != "captured" for item in observed):
        return False
    expected = sorted(expected, key=lambda item: item["z_index"])
    observed = sorted(observed, key=lambda item: item["z_index"])
    if [item["hwnd"] for item in expected] != [item["hwnd"] for item in observed]:
        return False
    if any(actual["pid"] != wanted["pid"] or actual["rect"] != wanted["rect"]
           for actual, wanted in zip(observed, expected)):
        return False
    if above_title is not None or below_title is not None:
        titles = {item["title"]: item["z_index"] for item in expected}
        if above_title not in titles or below_title not in titles or titles[above_title] >= titles[below_title]:
            return False
    return True


def stable_fixture_layout(url, state_file):
    """Bracket an observer sample with stable fixture geometry and stack snapshots."""
    before = json.loads(state_file.read_text(encoding="utf-8"))
    observed = layout(url)
    after = json.loads(state_file.read_text(encoding="utf-8"))
    stable = before.get("windows") == after.get("windows")
    return observed, after, stable


def fixture_main(args) -> int:
    """A tiny Qt window controlled only through a polled file, never desktop input."""
    h.dependencies(False)
    h.native_modules()
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
    import win32con
    import win32gui
    import win32process
    import win32service

    app = QApplication([])
    window = QWidget(None, Qt.WindowType.Window | Qt.WindowType.WindowDoesNotAcceptFocus)
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    window.setWindowTitle(args.title)
    label = QLabel("frame 0000")
    label.setStyleSheet("background:#101820;color:#f0f0f0;font:18pt monospace;padding:20px")
    layout_widget = QVBoxLayout(window)
    layout_widget.addWidget(label)
    window.setGeometry(120, 110, 300, 120)
    window.show()

    popup = None
    count = 0
    last_command = 0

    def snapshot():
        windows = []
        z_order = []
        current = win32gui.GetTopWindow(0)
        while current and current not in z_order and len(z_order) < 1024:
            z_order.append(current)
            current = win32gui.GetWindow(current, win32con.GW_HWNDNEXT)
        for z_index, hwnd in enumerate(z_order):
            if win32gui.IsWindowVisible(hwnd):
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid in (os.getpid(), args.expected_pid):
                    windows.append({"hwnd": hwnd, "pid": pid, "title": win32gui.GetWindowText(hwnd),
                                    "rect": list(win32gui.GetWindowRect(hwnd)), "z_index": z_index})
        desktop = win32service.GetThreadDesktop(win32api.GetCurrentThreadId())
        return {"pid": os.getpid(), "desktop": h.desktop_name(desktop), "windows": windows}

    import win32api

    def tick():
        nonlocal popup, count, last_command
        count += 1
        label.setText(f"frame {count:04d}")
        if args.command_file.exists():
            try:
                command = json.loads(args.command_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                command = {}
            if command.get("sequence", 0) > last_command:
                last_command = command["sequence"]
                if "x" in command:
                    win32gui.SetWindowPos(int(window.winId()), None, int(command["x"]), int(command["y"]),
                                          300, 120, win32con.SWP_NOACTIVATE | win32con.SWP_NOZORDER)
                if command.get("popup") and popup is None:
                    popup = QWidget(None, Qt.WindowType.Window | Qt.WindowType.WindowDoesNotAcceptFocus)
                    popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
                    popup.setWindowTitle(args.title + " Popup")
                    popup.setStyleSheet("background:#203040;color:white")
                    popup.resize(190, 80)
                    popup.show()
                    win32gui.SetWindowPos(int(popup.winId()), win32con.HWND_TOP, 170, 145, 190, 80,
                                          win32con.SWP_NOACTIVATE)
                elif not command.get("popup") and popup is not None:
                    popup.close()
                    popup.deleteLater()
                    popup = None
                if command.get("popup_top") and popup is not None:
                    win32gui.SetWindowPos(int(popup.winId()), win32con.HWND_TOP, 170, 145, 190, 80,
                                          win32con.SWP_NOACTIVATE)
                if command.get("main_top"):
                    win32gui.SetWindowPos(int(window.winId()), win32con.HWND_TOP, 120, 110, 300, 120,
                                          win32con.SWP_NOACTIVATE)
        write_json(args.state_file, snapshot())

    timer = QTimer()
    def safe_tick():
        try:
            tick()
        except Exception:
            args.state_file.with_suffix(".error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            timer.stop()
            app.quit()

    timer.timeout.connect(safe_tick)
    timer.start(50)
    write_json(args.state_file, snapshot())
    return app.exec()


def start_fixture(desktop: str, title: str, folder: Path, expected_pid: int = 0):
    command, state = folder / (title.replace(" ", "-") + ".command.json"), folder / (title.replace(" ", "-") + ".state.json")
    command.unlink(missing_ok=True)
    state.unlink(missing_ok=True)
    process = h.NativeProcess([sys.executable, "-B", str(Path(__file__).resolve()), "--fixture",
                               "--title", title, "--command-file", command, "--state-file", state,
                               "--expected-pid", str(expected_pid)], desktop, cwd=folder)
    return process, command, state


def set_command(path: Path, sequence: int, **fields):
    write_json(path, {"sequence": sequence, **fields})


def terminate_owned(process) -> None:
    import win32event

    if process.poll() is None:
        h.win32process.TerminateProcess(process.process, 0)
        wait_result = win32event.WaitForSingleObject(process.process, 5000)
        if wait_result != win32event.WAIT_OBJECT_0:
            raise TimeoutError(f"Test fixture PID {process.pid} did not exit after targeted termination.")
    process.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--proof", type=Path, default=None)
    args = parser.parse_args()
    if os.name != "nt":
        raise RuntimeError("This live integration test requires Windows.")
    args.runtime_dir.mkdir(parents=True, exist_ok=True)
    args.proof = args.proof or args.runtime_dir / "desktop-live-proof.json"
    args.proof.parent.mkdir(parents=True, exist_ok=True)
    h.dependencies(False)
    h.native_modules()
    if not __import__("importlib.util").util.find_spec("PyQt6"):
        raise RuntimeError("PyQt6 is required for the isolated live-window fixture.")

    proof = {"test": "desktop-wide live reconstruction on a fresh private desktop", "checks": {}}
    source_processes = []
    watch = None
    desktop_handle = None
    observer_work = None
    scratch_path = None
    input_before = h.input_desktop()
    desktop_name = "PyCharmHarness_streamtest_" + uuid.uuid4().hex[:10]
    try:
        desktop_handle = h.win32service.CreateDesktop(
            desktop_name, 0, h.win32con.DESKTOP_CREATEWINDOW | h.win32con.DESKTOP_READOBJECTS |
            h.win32con.DESKTOP_WRITEOBJECTS | h.win32con.DESKTOP_ENUMERATE, None)
        if h.input_desktop() != input_before:
            raise AssertionError("Creating the test desktop changed the user's input desktop.")
        scratch_path = Path(tempfile.mkdtemp(prefix="live-", dir=args.runtime_dir))
        with contextlib.nullcontext(scratch_path) as scratch:
            observer_work = scratch_path / "watch"
            observer_work.mkdir()
            unsafe_desktop = "Default" if input_before == "Default" else input_before
            rejected = subprocess.run(
                [sys.executable, "-B", str(WATCHER), "start", "--desktop", unsafe_desktop,
                 "--pid", str(os.getpid()), "--scope", "desktop", "--work-dir", str(observer_work),
                 "--interval", "0.1", "--port", "0"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if rejected.returncode == 0:
                raise AssertionError("Observer accepted the input desktop as its source.")
            proof["checks"]["input_desktop_observation_rejected"] = True
            proof["input_desktop_rejection"] = rejected.stdout.strip()[-300:]

            first, first_command, first_state = start_fixture(desktop_name, "Stream Fixture", scratch_path)
            source_processes.append(first)
            first_snapshot = wait_fixture(first, first_state, desktop_name)
            second, second_command, second_state = start_fixture(desktop_name, "Independent Fixture", scratch_path)
            source_processes.append(second)
            wait_fixture(second, second_state, desktop_name)
            proof["checks"]["fresh_desktop_is_not_input"] = h.input_desktop() != desktop_name
            proof["checks"]["fixture_processes_on_fresh_desktop"] = True
            process_identities_before = {fixture.pid: process_identity(fixture.pid) for fixture in source_processes}
            thread_desktops = []
            for thread in h.psutil.Process(first.pid).threads():
                try:
                    thread_desktops.append({"tid": thread.id,
                                            "desktop": h.desktop_name(h.win32service.GetThreadDesktop(thread.id))})
                except Exception as error:
                    thread_desktops.append({"tid": thread.id, "error": str(error)})
            proof["anchor_thread_desktops"] = thread_desktops
            proof["anchor_fixture_desktop"] = first_snapshot["desktop"]

            watch = run_json(WATCHER, "start", "--desktop", desktop_name, "--pid", first.pid,
                             "--scope", "desktop", "--work-dir", observer_work,
                             "--interval", "0.1", "--port", "0", timeout=70)
            proof["watch_dir"] = watch["watch"]
            proof["viewer_url"] = watch["url"]
            proof["fixture_after_observer_start"] = []
            for fixture, state_file in zip(source_processes, (first_state, second_state)):
                record = {"pid": fixture.pid, "exit_code": fixture.poll()}
                if fixture.poll() is None:
                    record["identity"] = process_identity(fixture.pid)
                error_file = state_file.with_suffix(".error.txt")
                if error_file.exists():
                    record["error"] = error_file.read_text(encoding="utf-8")
                proof["fixture_after_observer_start"].append(record)

            def captured(layout_data):
                return {item["title"]: item for item in layout_data["windows"] if item["status"] == "captured"}

            def current_layout():
                state = run_json(WATCHER, "status", "--watch", watch["watch"])["health"]
                if state["status"] != "running":
                    raise RuntimeError(f"Observer stopped before its workspace became available: {state}")
                return layout(watch["url"])

            def initial_snapshot():
                return (current_layout(), json.loads(first_state.read_text(encoding="utf-8")),
                        json.loads(second_state.read_text(encoding="utf-8")))

            def initial_matches(value):
                layout_data, first_live, second_live = value
                entries = captured(layout_data)
                if "Stream Fixture" not in entries or "Independent Fixture" not in entries:
                    return False
                first_live_window = next((item for item in first_live["windows"]
                                          if item["title"] == "Stream Fixture"), None)
                second_live_window = next((item for item in second_live["windows"]
                                           if item["title"] == "Independent Fixture"), None)
                return (first_live_window is not None and second_live_window is not None and
                        entries["Stream Fixture"]["rect"] == first_live_window["rect"] and
                        entries["Independent Fixture"]["rect"] == second_live_window["rect"])

            initial, first_info, independent_info = eventually(initial_snapshot, initial_matches)
            first_item = next(item for item in initial["windows"] if item["hwnd"] ==
                              next(win["hwnd"] for win in first_info["windows"] if win["title"] == "Stream Fixture"))
            independent_item = next(item for item in initial["windows"] if item["hwnd"] ==
                                    next(win["hwnd"] for win in independent_info["windows"]
                                         if win["title"] == "Independent Fixture"))
            assert independent_item["pid"] == second.pid
            proof["checks"]["independent_process_on_same_desktop_was_discovered"] = True
            proof["checks"]["captured_rect_matches_GetWindowRect"] = True

            set_command(first_command, 1, popup=True)
            popup, popup_state, _ = eventually(
                lambda: stable_fixture_layout(watch["url"], first_state),
                lambda value: value[2] and fixture_stack_matches(
                    value[0], value[1], "Stream Fixture Popup", "Stream Fixture"))
            popup_hwnd = next(win["hwnd"] for win in popup_state["windows"]
                              if win["title"] == "Stream Fixture Popup")
            main_hwnd = next(win["hwnd"] for win in popup_state["windows"] if win["title"] == "Stream Fixture")
            assert fixture_stack_matches(popup, popup_state, "Stream Fixture Popup", "Stream Fixture")
            proof["checks"]["popup_appeared_with_live_rect_and_z_order"] = True

            set_command(first_command, 2, popup=True, x=350, y=260)
            moved_state = eventually(lambda: json.loads(first_state.read_text(encoding="utf-8")),
                                     lambda value: any(win["title"] == "Stream Fixture" and
                                                       win["rect"][0:2] == [350, 260]
                                                       for win in value.get("windows", [])))
            moved = eventually(lambda: layout(watch["url"]),
                               lambda value: any(item["hwnd"] == main_hwnd and item["rect"][0:2] == [350, 260]
                                                 for item in value["windows"]))
            moved_item = next(item for item in moved["windows"] if item["hwnd"] == main_hwnd)
            assert moved_item["rect"] == next(win["rect"] for win in moved_state["windows"]
                                               if win["hwnd"] == main_hwnd)
            proof["checks"]["window_move_reflected_in_reconstruction"] = True

            set_command(first_command, 3, popup=True, main_top=True)
            topped, topped_state, _ = eventually(
                lambda: stable_fixture_layout(watch["url"], first_state),
                lambda value: value[2] and fixture_stack_matches(
                    value[0], value[1], "Stream Fixture", "Stream Fixture Popup") and
                    any(item["hwnd"] == main_hwnd and item["z_index"] == 0
                        for item in value[0]["windows"]))
            assert fixture_stack_matches(topped, topped_state, "Stream Fixture", "Stream Fixture Popup")
            assert next(item["z_index"] for item in topped["windows"] if item["hwnd"] == main_hwnd) == 0
            proof["checks"]["stack_order_change_reflected"] = True

            connection = http.client.HTTPConnection("127.0.0.1", int(watch["url"].split(":")[2].split("/")[0]), timeout=5)
            try:
                stream_path = "/" + watch["url"].split("/")[-1] + "/stream.mjpg"
                connection.request("GET", stream_path, headers={"Host": watch["url"].split("/")[2]})
                response = connection.getresponse()
                if response.status != 200 or not response.getheader("Content-Type", "").startswith(
                        "multipart/x-mixed-replace"):
                    raise AssertionError(f"Live MJPEG endpoint unavailable: HTTP {response.status}")
                fields1, frame1 = read_mjpeg_frame(response)
                fields2, frame2 = read_mjpeg_frame(response)
                fields3, frame3 = read_mjpeg_frame(response)
                assert int(fields1["X-Sequence"]) < int(fields2["X-Sequence"]) < int(fields3["X-Sequence"])
                assert float(fields1["X-Captured-At"]) <= float(fields3["X-Captured-At"])
                assert len({hashlib.sha256(frame).hexdigest() for frame in (frame1, frame2, frame3)}) > 1
                proof["checks"]["mjpeg_frames_decode_and_change"] = True
                proof["stream_frames"] = [
                    {"sequence": fields["X-Sequence"], "captured_at": fields["X-Captured-At"],
                     "sha256": hashlib.sha256(frame).hexdigest()}
                    for fields, frame in ((fields1, frame1), (fields2, frame2), (fields3, frame3))]
            finally:
                connection.close()

            set_command(first_command, 4, popup=False)
            closed_state = eventually(lambda: json.loads(first_state.read_text(encoding="utf-8")),
                                      lambda value: not any(win["title"] == "Stream Fixture Popup"
                                                            for win in value.get("windows", [])))
            closed = eventually(lambda: layout(watch["url"]),
                                lambda value: all(item["hwnd"] != popup_hwnd for item in value["windows"]))
            assert all(item["hwnd"] != popup_hwnd for item in closed["windows"])
            proof["checks"]["closed_popup_disappeared"] = True
            proof["checks"]["popup_close_confirmed_by_fixture"] = True
            source_window_state_before = {
                fixture.pid: {(window["hwnd"], window["pid"], window["title"], tuple(window["rect"]))
                              for window in json.loads(state_file.read_text(encoding="utf-8"))["windows"]}
                for fixture, state_file in ((first, first_state), (second, second_state))
            }

            stopped = run_json(WATCHER, "stop", "--watch", watch["watch"], timeout=25)
            watch = None
            if stopped["health"]["status"] != "stopped" or stopped["health"].get("cleanup", {}).get(
                    "active_processes") != 0:
                raise AssertionError("Observer cleanup did not confirm an empty worker job.")
            proof["checks"]["observer_stopped_cleanly"] = True
            for fixture in source_processes:
                if fixture.poll() is not None:
                    raise AssertionError(f"Observer affected fixture process {fixture.pid}.")
                if process_identity(fixture.pid) != process_identities_before[fixture.pid]:
                    raise AssertionError(f"Fixture process identity changed for PID {fixture.pid}.")
            source_window_state_after = {
                fixture.pid: {(window["hwnd"], window["pid"], window["title"], tuple(window["rect"]))
                              for window in json.loads(state_file.read_text(encoding="utf-8"))["windows"]}
                for fixture, state_file in ((first, first_state), (second, second_state))
            }
            if source_window_state_after != source_window_state_before:
                raise AssertionError("Observation changed source fixture window identities or positions.")
            proof["checks"]["test_processes_still_alive_after_observer_stop"] = True
            proof["checks"]["source_process_creation_identity_unchanged"] = True
            proof["checks"]["source_window_positions_and_handles_unchanged"] = True
            proof["input_desktop_after_observer_stop"] = h.input_desktop()
            if proof["input_desktop_after_observer_stop"] != input_before:
                raise AssertionError("The user's input desktop changed during observation.")
            proof["checks"]["input_desktop_unchanged"] = True
            proof["passed"] = True
    except Exception as error:
        proof["error"] = f"{type(error).__name__}: {error}"
        proof["traceback"] = traceback.format_exc()
        raise
    finally:
        if watch:
            try:
                run_json(WATCHER, "stop", "--watch", watch["watch"], timeout=25)
            except Exception as error:
                proof["observer_cleanup_error"] = str(error)
        for fixture in reversed(source_processes):
            try:
                terminate_owned(fixture)
            except Exception as error:
                proof.setdefault("fixture_cleanup_errors", []).append(str(error))
        if desktop_handle:
            desktop_handle.CloseDesktop()
        if proof.get("passed") and scratch_path:
            cleanup_target = scratch_path.resolve()
            cleanup_root = args.runtime_dir.resolve()
            if cleanup_target == cleanup_root or not cleanup_target.is_relative_to(cleanup_root):
                raise RuntimeError("Refusing cleanup outside this test runtime directory.")
            shutil.rmtree(cleanup_target, ignore_errors=True)
        try:
            proof["input_desktop_final"] = h.input_desktop()
            proof["input_desktop_restored"] = proof["input_desktop_final"] == input_before
        except Exception as error:
            proof["input_desktop_final_error"] = str(error)
        args.proof.write_text(json.dumps(proof, indent=2), encoding="utf-8")
    print(json.dumps(proof, indent=2))
    return 0


class StackComparisonTests(unittest.TestCase):
    def setUp(self):
        self.popup = {"hwnd": 20, "pid": 1, "title": "Popup", "rect": [0, 0, 2, 2], "z_index": 0}
        self.main = {"hwnd": 10, "pid": 1, "title": "Main", "rect": [0, 0, 3, 3], "z_index": 1}
        self.fixture = {"windows": [self.popup, self.main]}
        self.observed = {"windows": [
            {"hwnd": 30, "pid": 2, "title": "Independent", "rect": [0, 0, 1, 1], "z_index": 0, "status": "captured"},
            {**self.popup, "z_index": 1, "status": "captured"},
            {**self.main, "z_index": 2, "status": "captured"}]}

    def test_independent_window_does_not_change_relative_stack_comparison(self):
        self.assertTrue(fixture_stack_matches(self.observed, self.fixture, "Popup", "Main"))
        self.fixture["windows"] = [{**self.popup, "z_index": 5}, {**self.main, "z_index": 8}]
        self.assertTrue(fixture_stack_matches(self.observed, self.fixture, "Popup", "Main"))

    def test_reversed_stack_and_unapplied_requested_order_are_rejected(self):
        self.observed["windows"][1]["z_index"] = 3
        self.assertFalse(fixture_stack_matches(self.observed, self.fixture, "Popup", "Main"))
        self.assertFalse(fixture_stack_matches(self.observed, self.fixture, "Main", "Popup"))

    def test_missing_moved_or_uncaptured_fixture_window_is_rejected(self):
        for changes in ({"rect": [1, 1, 3, 3]}, {"status": "unavailable"}, {"pid": 99}):
            with self.subTest(changes=changes):
                changed = {"windows": [self.observed["windows"][0], {**self.observed["windows"][1], **changes}, self.observed["windows"][2]]}
                self.assertFalse(fixture_stack_matches(changed, self.fixture))
        self.assertFalse(fixture_stack_matches({"windows": self.observed["windows"][:2]}, self.fixture))


if __name__ == "__main__":
    if "--fixture" in sys.argv:
        fixture_parser = argparse.ArgumentParser()
        fixture_parser.add_argument("--fixture", action="store_true")
        fixture_parser.add_argument("--title", required=True)
        fixture_parser.add_argument("--command-file", type=Path, required=True)
        fixture_parser.add_argument("--state-file", type=Path, required=True)
        fixture_parser.add_argument("--expected-pid", type=int, default=0)
        fixture_args = fixture_parser.parse_args()
        try:
            raise SystemExit(fixture_main(fixture_args))
        except Exception:
            fixture_args.state_file.with_suffix(".error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            raise
    raise SystemExit(main())
