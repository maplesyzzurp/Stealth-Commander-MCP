"""Resource counter checks with deterministic process snapshots; no GUI actions."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import psutil

import metrics


class Process:
    def __init__(self, pid, created=10.0, cpu=1.0, read=100, write=200, rss=4096):
        self.pid, self.created, self.cpu = pid, created, cpu
        self.read, self.write, self.rss = read, write, rss
        self.memory_denied = False
        self.identity_reads = 0
        self.cpu_reads = 0
        self.name_value = "example.exe"
        self.parent_pid = 0
        self.child_pids = []
        self.lookup = None

    def create_time(self):
        self.identity_reads += 1
        return self.created

    def oneshot(self):
        return nullcontext()

    def name(self):
        return self.name_value

    def cpu_times(self):
        self.cpu_reads += 1
        return SimpleNamespace(user=self.cpu, system=0.0)

    def memory_info(self):
        if self.memory_denied:
            raise psutil.AccessDenied(self.pid)
        return SimpleNamespace(rss=self.rss, private=2048)

    def io_counters(self):
        return SimpleNamespace(read_bytes=self.read, write_bytes=self.write)

    def num_threads(self):
        return 3

    def num_handles(self):
        return 12

    def ppid(self):
        return self.parent_pid

    def children(self, recursive=False):
        return [self.lookup(pid) for pid in self.child_pids]


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.now, self.wall = 0.0, 1000.0
        self.items = {11: Process(11), 22: Process(22, rss=8192), 99: Process(99, created=5.0, rss=1024)}
        self.config = {"source_identity": ["PyCharmHarness_test", 11, 10.0, "C:/private/anchor.exe"],
                       "server_pid": 99, "server_created": 5.0}
        self.layout = {"captured_at": 999.5, "omitted_count": 2, "windows": [
            {"pid": 11, "hwnd": 101, "status": "captured", "title": "C:/secret/document",
             "capture": {"captured_at": 999.0, "sha256": "a" * 64, "path": "C:/secret/frame.png"}},
            {"pid": 11, "hwnd": 102, "status": "minimized"},
            {"pid": 22, "hwnd": 201, "status": "unavailable"}]}
        self.patches = [patch.object(metrics.psutil, "Process", side_effect=self.process),
                        patch.object(metrics.psutil, "cpu_count", return_value=4),
                        patch.object(metrics.time, "monotonic", side_effect=lambda: self.now),
                        patch.object(metrics.time, "time", side_effect=lambda: self.wall)]
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])
        self.sampler = metrics.DesktopMetricsSampler()

    def process(self, pid):
        if pid not in self.items:
            raise psutil.NoSuchProcess(pid)
        self.items[pid].lookup = self.process
        return self.items[pid]

    def advance(self, seconds=1.0):
        self.now += seconds
        self.wall += seconds

    def sample(self):
        return self.sampler.sample("approved-watch", self.config, self.layout)

    def test_first_sample_deduplicates_windows_and_separates_observer(self):
        result = self.sample()
        self.assertEqual(result["apps"]["process_count"], 2)
        self.assertEqual(result["apps"]["rss_bytes"], 12288)
        self.assertEqual(result["apps"]["threads"], 6)
        self.assertIsNone(result["apps"]["cpu_percent"])
        self.assertIsNone(result["apps"]["read_bytes_per_second"])
        self.assertEqual(result["observer"]["rss_bytes"], 1024)
        self.assertEqual(result["processes"][0]["hwnds"], [101, 102])
        self.assertEqual(result["processes"][0]["captured_hwnds"], [101])
        self.assertEqual(self.items[11].cpu_reads, 1)
        self.assertIsNone(result["capture"]["fps"])
        self.assertEqual(result["capture"]["frame_age_seconds"], 1.0)

    def test_cpu_uses_total_capacity_and_io_uses_elapsed_seconds(self):
        self.sample()
        self.advance(2.0)
        self.items[11].cpu += 2.0
        self.items[11].read += 1000
        self.items[11].write += 400
        result = self.sample()
        row = result["processes"][0]
        self.assertEqual(row["cpu_percent"], 25.0)
        self.assertEqual(row["read_bytes_per_second"], 500.0)
        self.assertEqual(row["write_bytes_per_second"], 200.0)
        self.assertEqual(result["apps"]["cpu_percent"], 25.0)
        self.assertEqual(row["uptime_seconds"], 992.0)

    def test_explicit_anchor_without_windows_and_shared_observer_are_counted_once(self):
        self.layout["windows"] = []
        self.config.update(server_pid=11, server_created=10.0)
        result = self.sample()
        self.assertEqual(result["apps"]["process_count"], 1)
        self.assertEqual(result["apps"]["rss_bytes"], 4096)
        self.assertEqual(result["processes"][0]["identity_source"], "anchor")
        self.assertEqual(result["processes"][0]["window_count"], 0)
        self.assertTrue(result["observer"]["shared_with_apps"])
        self.assertEqual(self.items[11].cpu_reads, 1)
        self.assertIsNone(result["capture"]["frame_age_seconds"])
        self.advance()
        self.items[11].cpu += 1
        result = self.sample()
        self.assertEqual(result["observer"]["cpu_percent"], 25.0)
        self.assertEqual(result["apps"]["cpu_percent"], 25.0)

    def test_cached_concurrent_clients_do_not_resample_or_mutate_state(self):
        first = self.sample()
        self.advance(.4)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.sample(), range(12)))
        self.assertTrue(all(result == first for result in results))
        self.assertEqual(self.items[11].cpu_reads, 1)
        results[0]["processes"][0]["rss_bytes"] = -100
        self.assertEqual(self.sample()["processes"][0]["rss_bytes"], 4096)
        self.advance(.7)
        self.sample()
        self.assertEqual(self.items[11].cpu_reads, 2)

    def test_pid_reuse_resets_rates_and_anchor_reuse_is_refused(self):
        self.sample()
        self.advance()
        self.items[22] = Process(22, created=20.0, cpu=50000.0, read=1000000)
        self.layout["captured_at"] += 1
        result = self.sample()
        self.assertIsNone(result["processes"][1]["cpu_percent"])
        self.assertIsNone(result["processes"][1]["read_bytes_per_second"])
        self.advance()
        self.items[22].cpu += 1
        self.assertEqual(self.sample()["processes"][1]["cpu_percent"], 25.0)
        self.advance()
        self.items[11] = Process(11, created=30.0)
        result = self.sample()
        self.assertEqual(result["processes"][0]["reason"], "identity_changed")
        self.assertIsNone(result["apps"]["rss_bytes"])

    def test_stale_manifest_cannot_bind_to_a_newly_reused_pid(self):
        self.items[22] = Process(22, created=1000.0)
        result = self.sample()
        self.assertEqual(result["processes"][1]["reason"], "newer_than_manifest")
        self.assertEqual(self.items[22].cpu_reads, 0)

    def test_same_stale_manifest_rejects_reuse_even_when_pid_predates_manifest(self):
        self.sample()
        self.advance()
        self.items[22] = Process(22, created=20.0)
        row = self.sample()["processes"][1]
        self.assertEqual(row["reason"], "identity_changed")
        self.assertEqual(self.items[22].cpu_reads, 0)
        self.advance()
        self.layout["captured_at"] += 1
        self.assertEqual(self.sample()["processes"][1]["process_created"], 20.0)

    def test_descendants_are_included_once_and_unrelated_processes_are_excluded(self):
        self.items[33] = Process(33, created=12.0, rss=16384)
        self.items[44] = Process(44, created=13.0, rss=32768)
        self.items[55] = Process(55, created=14.0, rss=999999)
        self.items[11].child_pids = [22, 33]
        self.items[22].parent_pid = self.items[33].parent_pid = 11
        self.items[33].child_pids = [44]
        self.items[44].parent_pid = 33
        self.items[55].parent_pid = 123
        result = self.sample()
        rows = {row["pid"]: row for row in result["processes"]}
        self.assertEqual(set(rows), {11, 22, 33, 44})
        self.assertEqual(result["apps"]["rss_bytes"], 61440)
        self.assertEqual(result["apps"]["descendant_coverage"], "complete")
        self.assertEqual(rows[22]["identity_source"], "observed_window")
        self.assertEqual(rows[44]["parent_pid"], 33)
        self.assertEqual(rows[44]["window_count"], 0)
        self.assertEqual(self.items[22].cpu_reads, 1)

    def test_stale_child_list_and_parent_reuse_are_not_adopted(self):
        self.items[33] = Process(33, created=12.0)
        self.items[11].child_pids = [33]
        self.items[33].parent_pid = 22
        result = self.sample()
        self.assertNotIn(33, [row["pid"] for row in result["processes"]])
        self.assertEqual(result["apps"]["descendant_coverage"], "partial")
        self.assertEqual(self.items[33].cpu_reads, 0)
        self.advance()
        self.items[33].parent_pid = 11
        original = self.items[33].cpu_times
        def replace_parent():
            self.items[11].created = 50.0
            return original()
        self.items[33].cpu_times = replace_parent
        rows = {row["pid"]: row for row in self.sample()["processes"]}
        self.assertEqual(rows[33]["reason"], "parent_identity_changed")
        self.assertIsNone(rows[33]["rss_bytes"])

    def test_capture_worker_counter_measures_faster_source_and_resets(self):
        self.layout.update(capture_sequence=10, capture_monotonic=50.0)
        self.assertIsNone(self.sample()["capture"]["fps"])
        self.advance()
        self.layout.update(capture_sequence=15, capture_monotonic=51.0)
        capture = self.sample()["capture"]
        self.assertEqual(capture["fps"], 5.0)
        self.assertEqual(capture["fps_basis"], "capture_worker_completed_passes")
        self.assertFalse(capture["fps_sampling_limited"])
        self.advance()
        self.layout.update(capture_sequence=1, capture_monotonic=52.0)
        self.assertIsNone(self.sample()["capture"]["fps"])
        self.advance()
        self.layout.update(capture_sequence=5, capture_monotonic=53.0)
        self.layout["captured_at"] = self.wall
        self.assertEqual(self.sample()["capture"]["fps"], 4.0)

    def test_access_denial_exit_and_counter_reset_do_not_make_false_totals(self):
        self.sample()
        self.advance()
        self.items[22].memory_denied = True
        result = self.sample()
        self.assertIsNone(result["apps"]["rss_bytes"])
        self.assertEqual(result["processes"][0]["rss_bytes"], 4096)
        self.advance()
        del self.items[22]
        result = self.sample()
        self.assertEqual(result["apps"]["unavailable_processes"], 1)
        self.assertIsNone(result["apps"]["cpu_percent"])
        self.advance()
        self.items[11].cpu = .1
        self.items[11].read = 1
        result = self.sample()
        self.assertIsNone(result["processes"][0]["cpu_percent"])
        self.assertIsNone(result["processes"][0]["read_bytes_per_second"])

    def test_capture_rate_counts_timestamps_even_for_unchanged_pixels(self):
        self.sample()
        self.advance()
        self.assertEqual(self.sample()["capture"]["fps"], 0.0)
        self.advance()
        self.layout["captured_at"] += 1
        self.layout["windows"][0]["capture"]["captured_at"] += 1
        capture = self.sample()["capture"]
        self.assertEqual(capture["fps"], .5)
        self.assertEqual(capture["frame_age_seconds"], 2.0)
        self.assertEqual(capture["window_count"], 3)
        self.assertEqual(capture["captured_window_count"], 1)
        self.layout["captured_at"] -= 20
        self.advance()
        self.assertIsNone(self.sample()["capture"]["fps"])

    def test_no_paths_titles_or_secret_process_metadata_escape(self):
        self.items[11].name_value = "C:\\private\\secret\\example.exe"
        result = self.sample()
        encoded = json.dumps(result)
        self.assertEqual(result["processes"][0]["name"], "example.exe")
        for forbidden in ("C:/", "C:\\\\", "secret", "source_identity", "command", "environment"):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(result["cpu_convention"], "whole_machine_capacity_100_percent")

    def test_identity_change_during_read_discards_the_sample(self):
        original = self.items[22].create_time
        reads = 0
        def changed():
            nonlocal reads
            reads += 1
            return original() if reads == 1 else 40.0
        self.items[22].create_time = changed
        row = self.sample()["processes"][1]
        self.assertEqual(row["status"], "unavailable")
        self.assertIsNone(row["rss_bytes"])
        self.assertIsNone(row["cpu_percent"])


if __name__ == "__main__":
    unittest.main()
