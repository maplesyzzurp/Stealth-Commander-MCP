"""Read-only resource samples for approved desktop windows and their anchor.

CPU percentages use the entire logical CPU capacity as 100%. Application totals
count observed PIDs, the approved anchor and their validated descendants once.
A process can work beyond this desktop; its cost is never per-window. Capture
FPS uses worker sequence/time counters where available, otherwise observed updates.
"""

import copy
from collections import deque
import math
import threading
import time

import psutil


_FIELDS = ("cpu_percent", "rss_bytes", "private_bytes", "threads", "handles",
           "read_bytes_per_second", "write_bytes_per_second")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _counter(value):
    return value if _number(value) and value >= 0 else None


def _pid(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _safe(call):
    try:
        return call()
    except (psutil.Error, OSError, AttributeError, NotImplementedError):
        return None


def _rate(current, previous, elapsed):
    if current is None or previous is None or elapsed <= 0 or current < previous:
        return None
    return (current - previous) / elapsed


def _unavailable(pid, hwnds, captured_hwnds, reason):
    return {"pid": pid, "process_created": None, "name": None,
            "hwnds": hwnds, "captured_hwnds": captured_hwnds,
            "window_count": len(hwnds), "uptime_seconds": None,
            "status": "unavailable", "reason": reason,
            **dict.fromkeys(_FIELDS)}


class DesktopMetricsSampler:
    """Use one shared instance; sample(root, approved_config, sanitized_layout).

    The dashboard must perform its existing approval, process and manifest checks
    first. This sampler reads no files or process environment/command lines. Results
    are cached per approved source for one second, including concurrent requests.
    """

    def __init__(self, interval=1.0):
        if not _number(interval) or interval < 1:
            raise ValueError("Resource sampling interval must be at least one second.")
        self.interval = float(interval)
        self._sources = {}
        self._lock = threading.Lock()

    def sample(self, root, config, layout):
        identity = config.get("source_identity")
        if (not isinstance(identity, (list, tuple)) or len(identity) != 4
                or not isinstance(identity[0], str) or not _pid(identity[1])
                or not _number(identity[2]) or identity[2] <= 0):
            raise ValueError("Approved anchor identity is required.")
        windows = layout.get("windows")
        captured_at = layout.get("captured_at")
        if not isinstance(windows, list) or not _number(captured_at):
            raise ValueError("A sanitized workspace layout is required.")
        for window in windows:
            if not isinstance(window, dict) or not _pid(window.get("pid")) or not _pid(window.get("hwnd")):
                raise ValueError("Invalid workspace process association.")
        source_key = (tuple(identity), config.get("server_pid"), config.get("server_created"))
        with self._lock:
            now = time.monotonic()
            state = self._sources.get(str(root))
            if state is None or state["identity"] != source_key:
                state = {"identity": source_key, "baselines": {}, "bindings": {},
                         "frames": deque(maxlen=11), "worker_frames": deque(maxlen=11),
                         "sampled_mono": None, "result": None}
                self._sources[str(root)] = state
            if (state["result"] is not None and state["sampled_mono"] is not None
                    and 0 <= now - state["sampled_mono"] < self.interval):
                return copy.deepcopy(state["result"])
            wall = time.time()
            capacity = psutil.cpu_count(logical=True)
            capacity = capacity if _pid(capacity) else None
            associations = {}
            for window in windows:
                item = associations.setdefault(window["pid"], {"hwnds": [], "captured_hwnds": []})
                if window["hwnd"] not in item["hwnds"]:
                    item["hwnds"].append(window["hwnd"])
                if window.get("status") == "captured" and window["hwnd"] not in item["captured_hwnds"]:
                    item["captured_hwnds"].append(window["hwnd"])
            associations.setdefault(identity[1], {"hwnds": [], "captured_hwnds": []})
            state["bindings"] = {pid: binding for pid, binding in state["bindings"].items()
                                 if pid in associations}
            rows, next_baselines = {}, {}
            for pid, association in sorted(associations.items()):
                expected = identity[2] if pid == identity[1] else None
                binding = state["bindings"].get(pid)
                if expected is None and binding and binding["manifest_at"] == captured_at:
                    expected = binding["created"]
                row, baseline = self._process(pid, expected, association, captured_at,
                                              wall, now, capacity, state["baselines"].get(pid))
                row["identity_source"] = "anchor" if pid == identity[1] else "observed_window"
                rows[pid] = row
                if baseline is not None:
                    next_baselines[pid] = baseline
                    state["bindings"][pid] = {"created": baseline["created"], "manifest_at": captured_at}
            descendant_complete = self._descendants(rows, next_baselines, state["baselines"],
                                                     wall, now, capacity)
            observer = None
            server_pid, server_created = config.get("server_pid"), config.get("server_created")
            if _pid(server_pid) and _number(server_created) and server_created > 0:
                if server_pid in rows and rows[server_pid]["process_created"] == server_created:
                    observer = copy.deepcopy(rows[server_pid])
                else:
                    observer, baseline = self._process(server_pid, server_created,
                        {"hwnds": [], "captured_hwnds": []}, None, wall, now, capacity,
                        state["baselines"].get(server_pid))
                    if baseline is not None and server_pid not in rows:
                        next_baselines[server_pid] = baseline
                observer["identity_source"] = "observer"
                observer["scope"] = "observer_server_process"
                observer["shared_with_apps"] = server_pid in rows
            processes = list(rows.values())
            apps = {field: (sum(row[field] for row in processes)
                            if all(row[field] is not None for row in processes) else None)
                    for field in _FIELDS}
            apps.update(process_count=len(processes),
                        unavailable_processes=sum(row["status"] == "unavailable" for row in processes),
                        scope="observed_processes_approved_anchor_and_validated_descendants",
                        descendant_coverage="complete" if descendant_complete else "partial")
            result = {"schema": "desktop-metrics-v1", "sampled_at": wall,
                      "sample_interval_seconds": self.interval, "cpu_capacity_count": capacity,
                      "cpu_convention": "whole_machine_capacity_100_percent",
                      "apps": apps, "processes": processes, "observer": observer,
                      "capture": self._capture(state["frames"], layout, wall, now,
                                               state["worker_frames"])}
            state.update(result=result, sampled_mono=now, baselines=next_baselines)
            return copy.deepcopy(result)

    def _descendants(self, rows, baselines, previous, wall, now, capacity):
        pending = [(row["pid"], row["process_created"]) for row in rows.values()
                   if row["process_created"] is not None]
        visited, complete = set(), True
        children_by_ppid = None
        if hasattr(psutil, "_ppid_map") and getattr(psutil.Process, "__module__", "").startswith("psutil"):
            try:
                children_by_ppid = {}
                for cpid, ppid in psutil._ppid_map().items():
                    children_by_ppid.setdefault(ppid, []).append(cpid)
            except Exception:
                children_by_ppid = None
        while pending and len(rows) < 1024:
            parent_pid, parent_created = pending.pop()
            if parent_pid in visited:
                continue
            visited.add(parent_pid)
            try:
                parent = psutil.Process(parent_pid)
                if parent.create_time() != parent_created:
                    complete = False
                    continue
                if children_by_ppid is not None:
                    child_pids = children_by_ppid.get(parent_pid, ())
                else:
                    child_pids = [c.pid for c in parent.children(recursive=False)]
            except (psutil.Error, OSError, AttributeError):
                complete = False
                continue
            for child_pid in child_pids:
                if child_pid in rows:
                    continue
                try:
                    current = psutil.Process(child_pid)
                    created = current.create_time()
                    if (not _number(created) or created < parent_created
                            or current.ppid() != parent_pid
                            or psutil.Process(parent_pid).create_time() != parent_created):
                        complete = False
                        continue
                except (psutil.Error, OSError):
                    complete = False
                    continue
                row, baseline = self._process(child_pid, created,
                    {"hwnds": [], "captured_hwnds": []}, None, wall, now, capacity,
                    previous.get(child_pid), parent=(parent_pid, parent_created))
                row.update(identity_source="descendant", parent_pid=parent_pid,
                           parent_process_created=parent_created)
                rows[child_pid] = row
                if baseline is not None:
                    baselines[child_pid] = baseline
                    pending.append((child_pid, created))
                else:
                    complete = False
                if len(rows) >= 1024:
                    break
        return complete and not pending

    def _process(self, pid, expected, association, manifest_at, wall, now, capacity, previous, parent=None):
        hwnds = sorted(association["hwnds"])
        captured_hwnds = sorted(association["captured_hwnds"])
        try:
            process = psutil.Process(pid)
            created = process.create_time()
            if not _number(created) or created <= 0:
                return _unavailable(pid, hwnds, captured_hwnds, "identity_unavailable"), None
            if expected is not None and created != expected:
                return _unavailable(pid, hwnds, captured_hwnds, "identity_changed"), None
            if manifest_at is not None and created > manifest_at:
                return _unavailable(pid, hwnds, captured_hwnds, "newer_than_manifest"), None
            if parent and (process.ppid() != parent[0]
                           or psutil.Process(parent[0]).create_time() != parent[1]):
                return _unavailable(pid, hwnds, captured_hwnds, "parent_identity_changed"), None
            with process.oneshot():
                name = _safe(process.name)
                cpu = _safe(process.cpu_times)
                memory = _safe(process.memory_info)
                io = _safe(process.io_counters)
                threads = _safe(process.num_threads)
                handles = _safe(lambda: process.num_handles())
            # Use a fresh Process identity because create_time() is cached by psutil.
            if psutil.Process(pid).create_time() != created:
                return _unavailable(pid, hwnds, captured_hwnds, "identity_changed"), None
            if parent and (psutil.Process(pid).ppid() != parent[0]
                           or psutil.Process(parent[0]).create_time() != parent[1]):
                return _unavailable(pid, hwnds, captured_hwnds, "parent_identity_changed"), None
        except (psutil.Error, OSError):
            return _unavailable(pid, hwnds, captured_hwnds, "process_unavailable"), None
        if isinstance(name, str):
            name = "".join(char for char in name.replace("\\", "/").rsplit("/", 1)[-1]
                           if char.isprintable())[:200] or None
        else:
            name = None
        cpu_time = (_counter(cpu.user + cpu.system) if cpu is not None else None)
        read = _counter(getattr(io, "read_bytes", None))
        write = _counter(getattr(io, "write_bytes", None))
        if previous is not None and previous["created"] == created:
            elapsed = now - previous["at"]
            cpu_rate = _rate(cpu_time, previous["cpu"], elapsed)
            read_rate = _rate(read, previous["read"], elapsed)
            write_rate = _rate(write, previous["write"], elapsed)
        else:
            cpu_rate = read_rate = write_rate = None
        row = {"pid": pid, "process_created": created, "name": name,
               "hwnds": hwnds, "captured_hwnds": captured_hwnds, "window_count": len(hwnds),
               "uptime_seconds": max(0.0, wall - created),
               "cpu_percent": min(100.0, cpu_rate * 100.0 / capacity)
                              if cpu_rate is not None and capacity else None,
               "rss_bytes": _counter(getattr(memory, "rss", None)),
               "private_bytes": _counter(getattr(memory, "private", None)),
               "threads": _counter(threads), "handles": _counter(handles),
               "read_bytes_per_second": read_rate, "write_bytes_per_second": write_rate,
               "reason": None}
        row["usage_scope"] = "whole_process_shared_by_associated_windows"
        row["status"] = "partial" if any(row[field] is None for field in _FIELDS) else "ok"
        baseline = {"created": created, "at": now, "cpu": cpu_time, "read": read, "write": write}
        return row, baseline

    def _capture(self, history, layout, wall, now, worker_history):
        stamp = layout["captured_at"]
        frames = [window["capture"]["captured_at"] for window in layout["windows"]
                  if window.get("status") == "captured" and isinstance(window.get("capture"), dict)
                  and _number(window["capture"].get("captured_at"))]
        if history and (now < history[-1][0] or stamp < history[-1][1]):
            history.clear()
        history.append((now, stamp, bool(frames)))
        while len(history) > 1 and history[0][0] < now - 10.0:
            history.popleft()
        fps = None
        if frames and len(history) > 1:
            elapsed = history[-1][0] - history[0][0]
            updates = sum(current[1] > previous[1] and current[2]
                          for previous, current in zip(history, list(history)[1:]))
            if elapsed > 0:
                fps = updates / elapsed
        basis, limited = "observed_manifest_updates", True
        sequence, worker_time = layout.get("capture_sequence"), layout.get("capture_monotonic")
        if _pid(sequence) and _number(worker_time) and worker_time > 0:
            if worker_history and (sequence < worker_history[-1][0] or worker_time < worker_history[-1][1]):
                worker_history.clear()
            if not worker_history or (sequence, worker_time) != worker_history[-1]:
                worker_history.append((sequence, worker_time))
            while len(worker_history) > 1 and worker_history[0][1] < worker_time - 10.0:
                worker_history.popleft()
            fps = None
            if len(worker_history) > 1:
                elapsed = worker_history[-1][1] - worker_history[0][1]
                if elapsed > 0:
                    fps = (worker_history[-1][0] - worker_history[0][0]) / elapsed
            if not frames:
                fps = None
            elif wall - stamp > 3 * self.interval:
                fps = 0.0
            basis, limited = "capture_worker_completed_passes", False
        else:
            worker_history.clear()
        return {"fps": fps, "fps_basis": basis,
                "fps_sampling_limited": limited,
                "frame_age_seconds": max(0.0, wall - max(frames)) if frames else None,
                "manifest_age_seconds": max(0.0, wall - stamp),
                "window_count": len(layout["windows"]), "captured_window_count": len(frames),
                "omitted_window_count": layout.get("omitted_count", 0)}
