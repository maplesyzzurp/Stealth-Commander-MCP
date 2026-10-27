"""Cached per-process busiest GPU engine, using Windows PDH without subprocesses.

Call snapshot(pids) with the processes to watch. The first result is unavailable
until two samples exist. Missing counters are unknown, including for idle apps.
This measures reported process engine time; it does not attribute DWM overhead.
Microsoft describes this aggregation at:
https://devblogs.microsoft.com/directx/gpus-in-the-task-manager/
"""

import ctypes
import math
import re
import threading
import time

COUNTER_PATH = r"\GPU Engine(*)\Utilization Percentage"
_INSTANCE = re.compile(r"^pid_(\d+)_(luid_.+_phys_\d+_eng_\d+_engtype_.+)$")


def aggregate_engines(rows, pids):
    """Return each requested PID's maximum valid engine sample, never a sum."""
    return {pid: (max(engines.values()), max(engines, key=engines.get))
            for pid, engines in _engine_values(rows, pids).items()}


def _engine_values(rows, pids):
    result = {}
    for name, value, status in rows:
        match = _INSTANCE.fullmatch(name)
        if not match or status not in (0, 1):
            continue
        pid = int(match[1])
        if pid not in pids or not math.isfinite(value) or value < 0:
            continue
        value = min(value, 100.0)
        engines = result.setdefault(pid, {})
        engines[match[2]] = max(value, engines.get(match[2], 0.0))
    return result


class _ValueUnion(ctypes.Union):
    _fields_ = [("doubleValue", ctypes.c_double), ("largeValue", ctypes.c_int64)]


class _Value(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("status", ctypes.c_uint32), ("value", _ValueUnion)]


class _Item(ctypes.Structure):
    _fields_ = [("name", ctypes.c_wchar_p), ("value", _Value)]


class _PdhQuery:
    def __init__(self):
        self.query = ctypes.c_void_p()
        self.counter = ctypes.c_void_p()
        self.primed = False
        self.dll = ctypes.WinDLL("pdh.dll")
        uint = ctypes.c_uint32
        ptr = ctypes.c_void_p
        signatures = {
            "PdhOpenQueryW": [ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(ptr)],
            "PdhAddEnglishCounterW": [ptr, ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(ptr)],
            "PdhCollectQueryData": [ptr],
            "PdhGetFormattedCounterArrayW": [ptr, uint, ctypes.POINTER(uint), ctypes.POINTER(uint), ptr],
            "PdhCloseQuery": [ptr],
        }
        for name, args in signatures.items():
            function = getattr(self.dll, name)
            function.argtypes = args
            function.restype = uint
        try:
            self._check(self.dll.PdhOpenQueryW(None, 0, ctypes.byref(self.query)))
            self._check(self.dll.PdhAddEnglishCounterW(self.query, COUNTER_PATH, 0, ctypes.byref(self.counter)))
        except Exception:
            self.close()
            raise

    @staticmethod
    def _check(status):
        if status:
            raise OSError(f"PDH status 0x{status:08x}")

    def sample(self):
        self._check(self.dll.PdhCollectQueryData(self.query))
        if not self.primed:
            self.primed = True
            return None
        # Keep each CStatus: some Python PDH wrappers discard invalid-item status.
        for _ in range(3):
            size, count = ctypes.c_uint32(), ctypes.c_uint32()
            status = self.dll.PdhGetFormattedCounterArrayW(
                self.counter, 0x200, ctypes.byref(size), ctypes.byref(count), None)
            if status == 0 and not count.value:
                return []
            if status != 0x800007D2:  # PDH_MORE_DATA
                self._check(status)
            buffer = ctypes.create_string_buffer(size.value)
            status = self.dll.PdhGetFormattedCounterArrayW(
                self.counter, 0x200, ctypes.byref(size), ctypes.byref(count), buffer)
            if status == 0x800007D2:
                continue
            self._check(status)
            items = ctypes.cast(buffer, ctypes.POINTER(_Item))
            return [(items[i].name or "", items[i].value.doubleValue,
                     items[i].value.status) for i in range(count.value)]
        raise OSError("GPU counter instances changed during collection")

    def close(self):
        if self.query:
            self.dll.PdhCloseQuery(self.query)
            self.query = ctypes.c_void_p()


class GpuSampler:
    """One persistent background query; snapshot never calls a native API.

    Requested PIDs remain watched for ten seconds after their last request,
    allowing several desktop cards to share one sampler. Call close on shutdown.
    """

    def __init__(self, interval=1.0, *, _query_factory=_PdhQuery):
        self.interval = max(1.0, float(interval))
        self._query_factory = _query_factory
        self._query = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._watched = {}
        self._values = {}
        self._engines = {}
        self._status = "warming_up"
        self._sample_time = None
        self._sample_monotonic = None
        self._thread = threading.Thread(target=self._run, name="gpu-metrics", daemon=True)
        self._thread.start()

    def _collect(self):
        try:
            if self._query is None:
                self._query = self._query_factory()
            rows = self._query.sample()
            now = time.monotonic()
            with self._lock:
                self._watched = {pid: seen for pid, seen in self._watched.items() if now - seen < 10}
                self._engines = _engine_values(rows or [], self._watched)
                self._values = {pid: (max(engines.values()), max(engines, key=engines.get))
                                for pid, engines in self._engines.items()}
                self._status = "warming_up" if rows is None else "ok"
                self._sample_time = time.time() if rows is not None else None
                self._sample_monotonic = now if rows is not None else None
        except Exception:
            if self._query is not None:
                self._query.close()
                self._query = None
            with self._lock:
                self._values = {}
                self._engines = {}
                self._status = "unavailable"
                self._sample_time = self._sample_monotonic = None

    def _run(self):
        try:
            while not self._stop.is_set():
                self._collect()
                self._stop.wait(self.interval)
        finally:
            if self._query is not None:
                self._query.close()

    def snapshot(self, pids):
        """Map explicit positive integer PIDs to nullable GPU fields.

        gpu_engines maps the adapter-LUID/physical-GPU/engine identity to percent.
        To aggregate a desktop, sum its PIDs within each identical engine key,
        cap each sum at 100, then take the maximum. Missing PIDs stay unknown.
        """
        pids = {pid for pid in pids if type(pid) is int and pid > 0}
        now = time.monotonic()
        with self._lock:
            self._watched.update(dict.fromkeys(pids, now))
            status = self._status
            if self._sample_monotonic is not None and now - self._sample_monotonic > 3 * self.interval:
                status = "stale"
            result = {}
            for pid in pids:
                value = self._values.get(pid) if status == "ok" else None
                result[pid] = {
                    "gpu_percent": value[0] if value else None,
                    "gpu_engine": value[1] if value else None,
                    "gpu_engines": dict(self._engines[pid]) if value else None,
                    "gpu_status": "no_counter" if status == "ok" and value is None else status,
                    "gpu_sampled_at": self._sample_time,
                }
            return result

    def close(self, timeout=2.0):
        self._stop.set()
        self._thread.join(timeout)
        return not self._thread.is_alive()
