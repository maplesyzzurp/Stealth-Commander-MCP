"""Discover approved inactive desktops and manage only their new observers."""

import argparse
import ctypes
from ctypes import wintypes as W
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

import psutil


def _eligible(name, active):
    return (isinstance(name, str) and name != active and "nexgent" not in name.casefold()
            and re.fullmatch(r"(?:PyCharmHarness|StealthREA)_[A-Za-z0-9_-]{1,100}", name) is not None)


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _pid(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _same(actual, pid, created, executable):
    return (actual is not None and actual.get("pid") == pid and actual.get("created") == created
            and str(actual.get("exe", "")).replace("\\", "/").casefold()
            == str(executable).replace("\\", "/").casefold())


def _process_info(pid):
    process = psutil.Process(pid)
    created = process.create_time()
    executable = process.exe()
    if not process.is_running() or psutil.Process(pid).create_time() != created:
        raise psutil.NoSuchProcess(pid)
    return {"pid": pid, "created": created, "exe": executable}


def _read_watch(root):
    root = Path(root).resolve(strict=True)
    path = (root / "watch.json").resolve(strict=True)
    if not path.is_relative_to(root) or path.stat().st_size > 65536:
        raise ValueError("Invalid observer metadata.")
    config = json.loads(path.read_text(encoding="utf-8"))
    identity = config.get("source_identity")
    if (config.get("schema") != "desktop-watch-v1" or Path(config.get("watch", "")).resolve() != root
            or not isinstance(identity, list) or len(identity) != 4
            or not isinstance(identity[0], str) or not _pid(identity[1]) or not _positive(identity[2])
            or not isinstance(identity[3], str) or not Path(identity[3]).is_absolute()
            or not _pid(config.get("server_pid")) or not _positive(config.get("server_created"))):
        raise ValueError("Invalid observer metadata.")
    return config


def _native():
    if os.name != "nt":
        raise OSError("Desktop discovery requires Windows.")
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.GetProcessWindowStation.restype = W.HANDLE
    u.GetThreadDesktop.argtypes = [W.DWORD]
    u.GetThreadDesktop.restype = W.HANDLE
    u.GetUserObjectInformationW.argtypes = [W.HANDLE, ctypes.c_int, W.LPVOID, W.DWORD, ctypes.POINTER(W.DWORD)]
    u.OpenInputDesktop.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    u.OpenInputDesktop.restype = W.HANDLE
    u.OpenDesktopW.argtypes = [W.LPCWSTR, W.DWORD, W.BOOL, W.DWORD]
    u.OpenDesktopW.restype = W.HANDLE
    u.CloseDesktop.argtypes = [W.HANDLE]
    u.IsWindowVisible.argtypes = [W.HWND]
    u.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
    u.GetWindowThreadProcessId.restype = W.DWORD
    u.GetWindowRect.argtypes = [W.HWND, ctypes.POINTER(W.RECT)]
    return u


def _object_name(u, handle):
    buffer = ctypes.create_unicode_buffer(256)
    needed = W.DWORD()
    if not handle or not u.GetUserObjectInformationW(handle, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer.value


def _input_name(u):
    handle = u.OpenInputDesktop(0, False, 0x1)
    try:
        return _object_name(u, handle)
    finally:
        if handle:
            u.CloseDesktop(handle)


def _probe_worker(desktop, output):
    u = _native()
    kernel = ctypes.WinDLL("kernel32")
    active = _input_name(u)
    if (_object_name(u, u.GetProcessWindowStation()) != "WinSta0" or not _eligible(desktop, active)
            or _object_name(u, u.GetThreadDesktop(kernel.GetCurrentThreadId())) != desktop):
        raise ValueError("Inactive desktop identity check failed.")
    handle = u.OpenDesktopW(desktop, 0, False, 0x1 | 0x40)
    callback_type = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    windows = {}
    def visit(hwnd, _):
        if u.IsWindowVisible(hwnd):
            pid = W.DWORD()
            tid = u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            rect = W.RECT()
            if (pid.value and tid and u.GetWindowRect(hwnd, ctypes.byref(rect))
                    and rect.right > rect.left and rect.bottom > rect.top):
                try:
                    if _object_name(u, u.GetThreadDesktop(tid)) == desktop:
                        value = windows.setdefault(pid.value, {"count": 0, "area": 0})
                        value["count"] += 1
                        value["area"] += (rect.right - rect.left) * (rect.bottom - rect.top)
                except OSError:
                    pass
        return True
    callback = callback_type(visit)
    u.EnumDesktopWindows.argtypes = [W.HANDLE, callback_type, W.LPARAM]
    try:
        if not handle or not u.EnumDesktopWindows(handle, callback, 0):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if handle:
            u.CloseDesktop(handle)
    anchors = []
    for pid in sorted(windows, key=lambda value: (-windows[value]["area"], value)):
        try:
            anchors.append(_process_info(pid))
        except (psutil.Error, OSError):
            continue
    if _input_name(u) != active:
        raise ValueError("Input desktop changed during discovery.")
    result = {"desktop": desktop, "window_count": sum(item["count"] for item in windows.values()),
              "anchors": anchors, "input_desktop": active}
    Path(output).write_text(json.dumps(result), encoding="utf-8")


def _query_desktop(desktop, folder):
    import win32api
    import win32con
    import win32event
    import win32job
    import win32process
    output = folder / ("probe-" + uuid.uuid4().hex + ".json")
    job = win32job.CreateJobObject(None, "")
    process = thread = None
    try:
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        startup = win32process.STARTUPINFO()
        startup.lpDesktop = "WinSta0\\" + desktop
        args = [sys.executable, "-B", str(Path(__file__).resolve()), "_probe", desktop, str(output)]
        process, thread, _, _ = win32process.CreateProcess(sys.executable, subprocess.list2cmdline(args),
            None, None, False, win32con.CREATE_NO_WINDOW | win32con.CREATE_SUSPENDED, None, str(folder), startup)
        win32job.AssignProcessToJobObject(job, process)
        win32process.ResumeThread(thread)
        if win32event.WaitForSingleObject(process, 4000) != win32event.WAIT_OBJECT_0:
            win32job.TerminateJobObject(job, 1)
            raise TimeoutError("Desktop query timed out.")
        if win32process.GetExitCodeProcess(process) or not output.exists() or output.stat().st_size > 65536:
            raise ValueError("Desktop query failed.")
        return json.loads(output.read_text(encoding="utf-8"))
    finally:
        if process and win32process.GetExitCodeProcess(process) == win32con.STILL_ACTIVE:
            win32process.TerminateProcess(process, 1)
        for handle in (thread, process, job):
            if handle:
                win32api.CloseHandle(handle)
        output.unlink(missing_ok=True)


class DesktopRegistry:
    """Background discovery; snapshot() performs no discovery or process calls.

    initial_sources are approved dashboard dictionaries with root/name fields.
    scan_roots are explicit existing observer parent directories, never a drive
    search. New observers live in private attempt directories under workdir.
    """

    def __init__(self, initial_sources, workdir, *, scan_roots=(), interval=5.0,
                 enumerator=None, starter=None, stopper=None, process_info=None, watch_reader=None):
        if not _positive(interval) or interval < 1:
            raise ValueError("Discovery interval must be at least one second.")
        self.workdir = Path(workdir).resolve()
        self.interval = float(interval)
        def creation_order(item):
            source = item.get("config", {}).get("source_identity", [])
            return source[2] if len(source) == 4 and _positive(source[2]) else float("inf")
        ordered = sorted(initial_sources, key=creation_order)
        self._initial = [{"root": Path(item["root"]).resolve(), "name": item["name"], "sequence": index + 1}
                         for index, item in enumerate(ordered)]
        self._desktop_order = {item["config"]["source_identity"][0]: index + 1
                              for index, item in enumerate(ordered) if item.get("config", {}).get("source_identity")}
        self._next_sequence = len(ordered) + 1
        self._scan_roots = list(dict.fromkeys([self.workdir, *map(lambda p: Path(p).resolve(), scan_roots),
                                             *(item["root"].parent for item in self._initial)]))
        self._process = process_info or _process_info
        self._reader = watch_reader or _read_watch
        self._enumerator = enumerator or self._enumerate
        self._starter = starter or self._start_observer
        self._stopper = stopper or self._stop_observer
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._thread = None
        self._owned = {}
        self._failures = {}
        self._snapshot = list(self._initial)
        self._errors = []
        self._last_scan = None

    def start(self):
        if self._stop.is_set():
            raise RuntimeError("Registry is closed.")
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="desktop-discovery", daemon=True)
            self._thread.start()
        return self

    def snapshot(self):
        with self._lock:
            return [dict(item) for item in self._snapshot]

    def status(self):
        with self._lock:
            return {"last_scan_at": self._last_scan, "errors": list(self._errors)}

    def _info(self, pid):
        try:
            return self._process(pid)
        except (psutil.Error, OSError, ValueError):
            return None

    def _watch(self, root):
        config = self._reader(root)
        identity = config["source_identity"]
        if (not _same(self._info(identity[1]), identity[1], identity[2], identity[3])
                or not _same(self._info(config["server_pid"]), config["server_pid"],
                             config["server_created"], sys.executable)):
            raise ValueError("Observer identity changed.")
        return config

    def _watch_paths(self):
        paths = [item["root"] for item in self._initial]
        for root in self._scan_roots:
            if not root.is_dir():
                continue
            for pattern in ("watch-*", "registry-*/watch-*", "*/watch-*", "*/*/watch-*", "*/*/*/watch-*"):
                for candidate in sorted(root.glob(pattern))[:128]:
                    resolved = candidate.resolve()
                    if resolved.is_relative_to(root):
                        paths.append(resolved)
        return list(dict.fromkeys([*paths, *self._owned]))

    def refresh_once(self):
        """One synchronous pass for the background loop and deterministic tests."""
        with self._refresh_lock:
            if self._stop.is_set():
                return
            try:
                inventory = self._enumerator()
                active = inventory["input_desktop"]
                if not isinstance(active, str) or not active:
                    raise ValueError("Input desktop is unavailable.")
                candidates = {}
                for item in inventory["desktops"]:
                    desktop = item.get("desktop")
                    anchors = item.get("anchors", [item])
                    if not _eligible(desktop, active) or not _positive(item.get("window_count")):
                        continue
                    for anchor in anchors:
                        if (_pid(anchor.get("pid")) and _positive(anchor.get("created"))
                                and isinstance(anchor.get("exe"), str)
                                and _same(self._info(anchor["pid"]), anchor["pid"], anchor["created"], anchor["exe"])):
                            candidates[desktop] = {"desktop": desktop, **anchor}
                            break
                present = {name for name in inventory.get("present_desktops", candidates)
                           if _eligible(name, active)}
            except Exception:
                with self._lock:
                    self._errors = ["discovery_unavailable"]
                return
            errors = []
            for root, record in list(self._owned.items()):
                source = record["config"]["source_identity"]
                if source[0] not in present or not _same(self._info(source[1]), source[1], source[2], source[3]):
                    if not self._release(root, record):
                        errors.append("observer_cleanup_unconfirmed")
            names = {item["root"]: item["name"] for item in self._initial}
            available = {}
            for root in self._watch_paths():
                try:
                    config = self._watch(root)
                    desktop = config["source_identity"][0]
                    if desktop in present and _eligible(desktop, active):
                        available.setdefault(desktop, {"root": root,
                            "name": names.get(root, self._friendly(desktop))})
                except Exception:
                    continue
            for desktop, candidate in candidates.items():
                if desktop in available or self._stop.is_set():
                    continue
                pin = (candidate["pid"], candidate["created"], candidate["exe"])
                failure = self._failures.get(desktop)
                if failure and failure["identity"] == pin:
                    if failure["attempts"] >= 3 or time.monotonic() < failure["retry_at"]:
                        errors.append("observer_start_backoff" if failure["attempts"] < 3 else "observer_start_exhausted")
                        continue
                else:
                    failure = {"identity": pin, "attempts": 0, "retry_at": 0}
                created_root = None
                try:
                    result = self._starter(candidate)
                    root = Path(result["watch"]).resolve()
                    if not root.is_relative_to(self.workdir):
                        raise ValueError("New observer is outside its private runtime directory.")
                    self._owned[root] = {"config": result, "name": self._friendly(desktop)}
                    created_root = root
                    config = self._watch(root)
                    if config["source_identity"] != [desktop, *pin]:
                        raise ValueError("Observer source identity changed.")
                    self._owned[root] = {"config": config, "name": self._friendly(desktop)}
                    available[desktop] = {"root": root, "name": self._friendly(desktop)}
                    self._failures.pop(desktop, None)
                except Exception:
                    if created_root is not None and created_root in self._owned:
                        if not self._release(created_root, self._owned[created_root]):
                            errors.append("observer_cleanup_unconfirmed")
                    failure["attempts"] += 1
                    failure["retry_at"] = time.monotonic() + min(60, 5 * 2 ** (failure["attempts"] - 1))
                    self._failures[desktop] = failure
                    errors.append("observer_start_failed")
            self._failures = {key: value for key, value in self._failures.items() if key in candidates}
            with self._lock:
                for desktop in available:
                    if desktop not in self._desktop_order:
                        self._desktop_order[desktop] = self._next_sequence
                        self._next_sequence += 1
                    available[desktop]["sequence"] = self._desktop_order[desktop]
                self._snapshot = [available[key] for key in sorted(available, key=self._desktop_order.get)]
                self._last_scan = time.time()
                self._errors = errors

    @staticmethod
    def _friendly(desktop):
        return "Desktop " + desktop.split("_", 1)[1].replace("_", " ")[:80]

    def _release(self, root, record):
        config = record["config"]
        pid, created = config.get("server_pid"), config.get("server_created")
        if not _pid(pid) or not _positive(created):
            return False
        current = self._info(pid)
        if current is not None and not _same(current, pid, created, sys.executable):
            self._owned.pop(root, None)
            return False
        try:
            if current is not None:
                actual = self._reader(root)
                if any(actual.get(key) != config.get(key) for key in ("server_pid", "server_created", "source_identity")):
                    return False
                self._stopper(root)
            self._owned.pop(root, None)
            return True
        except Exception:
            return False

    def _run(self):
        try:
            while not self._stop.is_set():
                self.refresh_once()
                self._stop.wait(self.interval)
        finally:
            for root, record in list(self._owned.items()):
                self._release(root, record)

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=60)
            if self._thread.is_alive():
                raise TimeoutError("Observer registry cleanup is still pending.")
        else:
            for root, record in list(self._owned.items()):
                self._release(root, record)
        with self._lock:
            self._snapshot = []
        if self._owned:
            raise RuntimeError("Owned observer cleanup could not be confirmed.")

    def _enumerate(self):
        self.workdir.mkdir(parents=True, exist_ok=True)
        u = _native()
        if _object_name(u, u.GetProcessWindowStation()) != "WinSta0":
            raise ValueError("Expected the interactive window station.")
        active = _input_name(u)
        names = []
        callback_type = ctypes.WINFUNCTYPE(W.BOOL, W.LPWSTR, W.LPARAM)
        def visit(name, _):
            names.append(name)
            return True
        callback = callback_type(visit)
        u.EnumDesktopsW.argtypes = [W.HANDLE, callback_type, W.LPARAM]
        if not u.EnumDesktopsW(u.GetProcessWindowStation(), callback, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        desktops = []
        for desktop in sorted(name for name in names if _eligible(name, active))[:32]:
            if self._stop.is_set():
                break
            try:
                result = _query_desktop(desktop, self.workdir)
                if result["input_desktop"] == active:
                    desktops.append(result)
            except Exception:
                continue
        if _input_name(u) != active:
            raise ValueError("Input desktop changed during discovery.")
        return {"input_desktop": active, "desktops": desktops,
                "present_desktops": [name for name in names if _eligible(name, active)]}

    def _start_observer(self, candidate):
        attempt = self.workdir / ("registry-" + uuid.uuid4().hex[:12])
        attempt.mkdir(parents=True)
        args = [sys.executable, "-B", str(Path(__file__).with_name("desktop_watch.py")), "start",
                "--desktop", candidate["desktop"], "--pid", str(candidate["pid"]), "--scope", "desktop",
                "--interval", ".0167", "--port", "0", "--work-dir", str(attempt)]
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        started = time.monotonic()
        try:
            while not self._stop.is_set() and time.monotonic() - started < 50:
                try:
                    stdout, _ = process.communicate(timeout=.2)
                    result = json.loads(stdout)
                    if process.returncode or not result.get("ok"):
                        raise RuntimeError("Observer start failed.")
                    config = result["result"]
                    if not Path(config["watch"]).resolve().is_relative_to(attempt):
                        raise ValueError("Observer returned an unexpected runtime directory.")
                    return config
                except subprocess.TimeoutExpired:
                    continue
            raise TimeoutError("Observer start cancelled or timed out.")
        except Exception:
            for root in attempt.glob("watch-*"):
                try:
                    self._stop_observer(root)
                except Exception:
                    pass
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)

    @staticmethod
    def _stop_observer(root):
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("desktop_watch.py")),
                                 "stop", "--watch", str(root)], capture_output=True, text=True,
                                timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        response = json.loads(result.stdout)
        if result.returncode or not response.get("ok") or response["result"].get("health", {}).get("status") != "stopped":
            raise RuntimeError("Observer stop was not confirmed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("_probe",))
    parser.add_argument("desktop")
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    _probe_worker(arguments.desktop, arguments.output)
