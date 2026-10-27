"""Export exact published observer PNGs. This module never renders or captures a window."""
import base64
from collections import OrderedDict
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import threading
import time

import harness as h

MAX_AGE = 3.0
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_CACHE_BYTES = 32 * 1024 * 1024
MAX_METADATA_BYTES = 256 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class Unavailable(ValueError):
    pass


def _positive(value):
    return type(value) is int and value > 0


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _fresh(value, now):
    return _finite(value) and 0 <= now - value <= MAX_AGE


def _normal(path):
    return os.path.normcase(os.path.abspath(str(path)))


def _no_reparse(path, root):
    path, root = Path(path), Path(root)
    if (not path.is_absolute() or not root.is_absolute() or str(path).startswith(("\\\\", "//"))
            or ".." in path.parts or ".." in root.parts):
        raise Unavailable("Invalid local observation path.")
    try:
        path.relative_to(root)
    except ValueError:
        raise Unavailable("Observation path escaped its approved directory.") from None
    for component in (path, *path.parents):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise Unavailable("Reparse observation paths are unavailable.")
    return path


def _opened_path(fd):
    if os.name != "nt":
        return os.readlink(f"/proc/self/fd/{fd}")
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel.GetFinalPathNameByHandleW
    function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    function.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = function(msvcrt.get_osfhandle(fd), buffer, len(buffer), 0)
    if not length or length >= len(buffer):
        raise Unavailable("Opened observation path could not be verified.")
    result = buffer.value
    return result[4:] if result.startswith("\\\\?\\") else result


def _file_identity(info):
    # Windows fstat and lstat expose different legacy ctime meanings on Python 3.13.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read_regular(path, root, limit):
    path = _no_reparse(path, root)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_size > limit
                or _normal(_opened_path(stream.fileno())) != _normal(path)):
            raise Unavailable("Observation file could not be verified.")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        current = _no_reparse(path, root).lstat()
        if (len(data) > limit or _file_identity(before) != _file_identity(after)
                or _file_identity(after) != _file_identity(current)):
            raise Unavailable("Observation file changed during the read.")
        return data


def _read_json(path, root):
    raw = _read_regular(path, root, MAX_METADATA_BYTES)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise Unavailable("Invalid observation metadata.")
    return raw, value


def approved_config(raw):
    return {key: raw.get(key) for key in
            ("url", "source_identity", "server_pid", "server_created", "scope", "source", "anchor", "worker_pid", "worker_created")}


def _source_state(source, now):
    root = Path(source["root"])
    watch_raw, watch = _read_json(root / "watch.json", root)
    if (watch.get("schema") != "desktop-watch-v1" or _normal(watch.get("watch", "")) != _normal(root)
            or approved_config(watch) != source["config"] or watch.get("scope") not in ("session", "desktop")
            or not _positive(watch.get("worker_pid")) or not _finite(watch.get("worker_created"))):
        raise Unavailable("Approved observer identity changed.")
    _, state = _read_json(root / "browser-session.json", root)
    identity = watch["source_identity"]
    if (state.get("schema") != "rea-browser-v1" or state.get("status") != "running"
            or state.get("desktop") != identity[0] or state.get("pid") != identity[1]
            or state.get("worker_pid") != watch["worker_pid"] or state.get("worker_created") != watch["worker_created"]
            or _normal(state.get("session", "")) != _normal(root)
            or not _fresh(state.get("heartbeat"), now) or state.get("error") or state.get("capture_error")):
        raise Unavailable("Observer is unavailable or stale.")
    return watch_raw, watch


def display_affinity_allowed(hwnd):
    """Query the OS protection flag; failure and every protected mode deny export."""
    if os.name != "nt":
        return False
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    function = user.GetWindowDisplayAffinity
    function.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    function.restype = wintypes.BOOL
    affinity = wintypes.DWORD()
    return bool(function(hwnd, ctypes.byref(affinity))) and affinity.value == 0


def _verify_at_observer(source, record):
    """Ask the verified on-desktop observer to recheck the *current* OS capture state."""
    import http.client
    root = Path(source["root"])
    _source_state(source, time.time())
    _, state = _read_json(root / "browser-session.json", root)
    port, token = state.get("verifier_port"), state.get("verifier_token")
    worker_pid, created = state.get("worker_pid"), state.get("worker_created")
    if (not _positive(port) or port > 65535 or not isinstance(token, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{40,128}", token) is None
            or not _positive(worker_pid) or not _finite(created)):
        raise Unavailable("The on-desktop verifier is not available.")
    try:
        worker = h.psutil.Process(worker_pid)
        if (not worker.is_running() or abs(worker.create_time() - created) > .01
                or _normal(worker.exe()) != _normal(os.path.abspath(__import__("sys").executable))):
            raise Unavailable("The verifier process identity changed.")
        sockets = worker.net_connections(kind="tcp")
        if not any(conn.status == h.psutil.CONN_LISTEN
                   and conn.laddr.ip == "127.0.0.1" and conn.laddr.port == port
                   for conn in sockets):
            raise Unavailable("The approved verifier does not own its loopback listener.")
        request = json.dumps({
            "hwnd": record["hwnd"], "pid": record["pid"],
            "process_created": record["process_created"],
            "process_executable": record["process_executable"],
            "desktop": source["config"]["source_identity"][0],
            "scope": source["config"]["scope"],
        }, separators=(",", ":")).encode("utf-8")
        if len(request) > 1024:
            raise Unavailable("Verifier request exceeds its limit.")
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=1.5)
        try:
            client.request("POST", "/verify", request, {
                "Host": f"127.0.0.1:{port}", "Content-Type": "application/json",
                "X-Stealth-Observer-Token": token,
            })
            reply = client.getresponse()
            payload = reply.read(65)
            if reply.status != 200 or payload != b'{"ok":true}':
                raise Unavailable("Current capture permission was not confirmed.")
        finally:
            client.close()
        if (not worker.is_running() or abs(worker.create_time() - created) > .01
                or h.input_desktop() == source["config"]["source_identity"][0]):
            raise Unavailable("Verifier identity or input isolation changed.")
        _source_state(source, time.time())
    except (OSError, ValueError, TypeError, KeyError, AttributeError, h.psutil.Error) as exc:
        raise Unavailable("Observer verification could not be completed.") from exc


def verify_window_binding(config, record, source):
    """Controller validates process identity; OS window/affinity checks run on source desktop."""
    desktop, anchor_pid, created, executable = config["source_identity"]
    if h.input_desktop() == desktop:
        raise Unavailable("The source is the active input desktop.")
    anchor = h.owned_process({"pid": anchor_pid, "process_created": created, "ide_executable": executable})
    process = h.psutil.Process(record["pid"])
    expected = (record["process_created"], _normal(record["process_executable"]))
    if ((process.create_time(), _normal(process.exe())) != expected or not process.is_running()):
        raise Unavailable("The captured process identity changed.")
    if config["scope"] == "session":
        if process.pid != anchor.pid and anchor.pid not in {parent.pid for parent in process.parents()}:
            raise Unavailable("The captured window is outside the owned session tree.")
    elif config["scope"] != "desktop":
        raise Unavailable("Unrecognized observation scope.")
    if source["config"] != config:
        raise Unavailable("Approved observer configuration changed.")
    _verify_at_observer(source, record)
    if ((process.create_time(), _normal(process.exe())) != expected
            or h.input_desktop() == desktop):
        raise Unavailable("Window process identity or input isolation changed.")


def _frame(root, source, layout, item, now):
    record = item.get("capture")
    identity = source["config"]["source_identity"]
    if (item.get("status") != "captured" or not isinstance(record, dict)
            or record.get("source_identity") != identity or record.get("capture_scope") != source["config"]["scope"]
            or record.get("desktop") != identity[0] or not isinstance(record.get("input_desktop"), str)
            or not record["input_desktop"] or record["input_desktop"] == identity[0]
            or record.get("display_affinity") != 0 or type(record.get("display_affinity")) is not int
            or any(record.get(key) != item.get(key) for key in ("pid", "hwnd", "rect"))
            or not all(_positive(record.get(key)) for key in ("pid", "hwnd", "width", "height"))
            or not _finite(record.get("process_created")) or record["process_created"] < identity[2]
            or record["process_created"] > record.get("captured_at", 0)
            or not isinstance(record.get("process_executable"), str)
            or not Path(record["process_executable"]).is_absolute()
            or not _fresh(record.get("captured_at"), now)
            or not isinstance(record.get("sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None):
        raise Unavailable("Published frame identity is missing or mismatched.")
    path = Path(record.get("path", ""))
    if not path.is_absolute() or path.suffix.lower() != ".png":
        raise Unavailable("Invalid published image path.")
    try:
        path.relative_to(root / "captures")
    except ValueError:
        raise Unavailable("Image escaped the approved capture directory.") from None
    image = _read_regular(path, root, MAX_FRAME_BYTES)
    if (len(image) < 33 or not image.startswith(PNG_SIGNATURE) or image[12:16] != b"IHDR"
            or struct.unpack(">II", image[16:24]) != (record["width"], record["height"])
            or record["width"] * record["height"] > 16_000_000
            or hashlib.sha256(image).hexdigest() != record["sha256"]):
        raise Unavailable("Published PNG bytes do not match the frame reference.")
    provenance = {"schema": "observability-export-v1", "source": "published-observer-frame",
                  "desktop_id": source["id"], "desktop": identity[0], "hwnd": record["hwnd"],
                  "pid": record["pid"], "process_created": record["process_created"],
                  "capture_sequence": layout["capture_sequence"], "captured_at": record["captured_at"],
                  "published_at": layout["captured_at"], "sha256": record["sha256"],
                  "recorded_method": record.get("method"), "producer_pipeline": "existing-observer-d3d11-or-printwindow",
                  "max_age_seconds": MAX_AGE}
    return image, record, provenance


class ObservationCache:
    def __init__(self, max_bytes=MAX_CACHE_BYTES):
        self._frames = OrderedDict()
        self._bytes = 0
        self._limit = min(max_bytes, MAX_CACHE_BYTES)
        self._lock = threading.Lock()
        self._publish_lock = threading.Lock()

    def _expire(self, now):
        for key, frame in list(self._frames.items()):
            if not _fresh(frame["provenance"]["captured_at"], now):
                self._bytes -= len(frame["image"])
                del self._frames[key]

    def publish(self, source, hwnd=None):
        """Pin already-published bytes. This does not contact the producer or target."""
        with self._publish_lock:
            return self._publish(source, hwnd)

    def _publish(self, source, hwnd=None):
        try:
            now = time.time()
            root = Path(source["root"])
            watch_raw, _ = _source_state(source, now)
            manifest_raw, layout = _read_json(root / "captures" / "desktop.json", root)
            if (layout.get("schema") != "desktop-layout-v1" or not _positive(layout.get("capture_sequence"))
                    or not _fresh(layout.get("captured_at"), now) or not isinstance(layout.get("windows"), list)
                    or len(layout["windows"]) > 32):
                raise Unavailable("Published layout is unavailable or stale.")
            staged, seen, staged_bytes = [], set(), 0
            for item in layout["windows"]:
                if not isinstance(item, dict) or not _positive(item.get("hwnd")) or item["hwnd"] in seen:
                    raise Unavailable("Invalid published window selection.")
                seen.add(item["hwnd"])
                if hwnd is not None and item["hwnd"] != hwnd:
                    continue
                try:
                    image, record, provenance = _frame(root, source, layout, item, now)
                except (OSError, ValueError, TypeError, KeyError):
                    continue
                provenance["manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
                if staged_bytes + len(image) > self._limit:
                    break
                staged_bytes += len(image)
                key = (source["id"], record["hwnd"], record["sha256"], layout["capture_sequence"])
                staged.append((key, {"image": image, "record": dict(record), "provenance": provenance,
                                     "config": json.loads(json.dumps(source["config"])), "root": str(root)}))
            if (_read_regular(root / "captures" / "desktop.json", root, MAX_METADATA_BYTES) != manifest_raw
                    or _source_state(source, time.time())[0] != watch_raw):
                raise Unavailable("Published observation changed while pinning bytes.")
            with self._lock:
                self._expire(time.time())
                refs = []
                for key, frame in staged:
                    if not _fresh(frame["provenance"]["captured_at"], time.time()):
                        continue
                    if key not in self._frames:
                        self._frames[key] = frame
                        self._bytes += len(frame["image"])
                    while self._bytes > self._limit and self._frames:
                        _, removed = self._frames.popitem(last=False)
                        self._bytes -= len(removed["image"])
                    if key in self._frames:
                        refs.append({"hwnd": key[1], "sha256": key[2], "capture_sequence": key[3]})
                return refs
        except (OSError, ValueError, TypeError, KeyError):
            return []

    def export(self, source, hwnd, sha256=None, capture_sequence=None, verify_source=None):
        if (not _positive(hwnd) or (sha256 is None) != (capture_sequence is None)
                or (sha256 is not None and (not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256)
                                            or not _positive(capture_sequence)))):
            raise Unavailable("Invalid observability frame reference.")
        if sha256 is None:
            refs = self.publish(source, hwnd)
            if not refs:
                raise Unavailable("No fresh published frame is available for this window.")
            sha256, capture_sequence = refs[0]["sha256"], refs[0]["capture_sequence"]
        key = (source["id"], hwnd, sha256, capture_sequence)
        with self._lock:
            self._expire(time.time())
            frame = self._frames.get(key)
        if frame is None or frame["root"] != str(source["root"]) or frame["config"] != source["config"]:
            raise Unavailable("The exact published frame is unavailable or expired.")
        if verify_source is None:
            raise Unavailable("Observer identity verification is required.")
        verify_source(source["config"])
        _source_state(source, time.time())
        verify_window_binding(source["config"], frame["record"], source)
        verify_source(source["config"])
        _source_state(source, time.time())
        now = time.time()
        if not _fresh(frame["provenance"]["captured_at"], now) or not _fresh(frame["provenance"]["published_at"], now):
            raise Unavailable("The published frame expired during verification.")
        return {"ok": True, "mime_type": "image/png", "data": base64.b64encode(frame["image"]).decode("ascii"),
                "provenance": {**frame["provenance"], "read_at": now}}
