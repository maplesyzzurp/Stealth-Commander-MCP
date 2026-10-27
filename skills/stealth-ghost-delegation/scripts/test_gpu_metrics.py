import threading
import time
import unittest
from unittest.mock import patch

from gpu_metrics import GpuSampler, aggregate_engines


def engine(pid, index=0):
    return f"pid_{pid}_luid_0x00000000_0x00016941_phys_0_eng_{index}_engtype_3D"


class GpuMetricsTests(unittest.TestCase):
    def test_busiest_engine_and_exact_pid_filter(self):
        rows = [(engine(12), 61.5, 0), (engine(12, 1), 70.0, 1),
                (engine(123), 99.0, 0), (engine(7), 0.0, 0)]
        result = aggregate_engines(rows, {12, 7, 9})
        self.assertEqual(result[12][0], 70.0)
        self.assertEqual(result[7][0], 0.0)
        self.assertNotIn(123, result)
        self.assertNotIn(9, result)

    def test_invalid_values_are_unavailable_and_percent_is_bounded(self):
        rows = [(engine(1), float("nan"), 0), (engine(2), 0.0, 0xC0000BC6),
                (engine(3), -1.0, 0), (engine(4), float("inf"), 0),
                ("bad_pid_5", 5.0, 0), (engine(6), 120.0, 0)]
        self.assertEqual(aggregate_engines(rows, set(range(1, 7))),
                         {6: (100.0, engine(6).split("_", 2)[2])})

    def sampler_without_thread(self, factory):
        with patch("gpu_metrics.threading.Thread"):
            return GpuSampler(_query_factory=factory)

    def test_first_sample_missing_counter_and_error_do_not_become_zero(self):
        class Query:
            calls = 0
            closed = False

            def sample(self):
                self.calls += 1
                if self.calls == 1:
                    return None
                if self.calls == 2:
                    return [(engine(12), 0.0, 0)]
                raise OSError("counter unavailable")

            def close(self):
                self.closed = True

        query = Query()
        sampler = self.sampler_without_thread(lambda: query)
        self.assertIsNone(sampler.snapshot([12])[12]["gpu_percent"])
        sampler._collect()
        self.assertEqual(sampler.snapshot([12])[12]["gpu_status"], "warming_up")
        sampler._collect()
        result = sampler.snapshot([12, 13])
        self.assertEqual(result[12]["gpu_percent"], 0.0)
        self.assertEqual(result[12]["gpu_engines"], {engine(12).split("_", 2)[2]: 0.0})
        self.assertEqual(result[13]["gpu_status"], "no_counter")
        self.assertIsNone(result[13]["gpu_engines"])
        self.assertIsNone(result[13]["gpu_percent"])
        sampler._collect()
        self.assertTrue(query.closed)
        self.assertEqual(sampler.snapshot([12])[12]["gpu_status"], "unavailable")
        self.assertIsNone(sampler.snapshot([12])[12]["gpu_percent"])

    def test_snapshot_never_waits_for_slow_native_query(self):
        entered, release = threading.Event(), threading.Event()

        class Query:
            def sample(self):
                entered.set()
                release.wait(3)
                return None

            def close(self):
                pass

        sampler = GpuSampler(_query_factory=Query)
        try:
            self.assertTrue(entered.wait(1))
            start = time.monotonic()
            self.assertIsNone(sampler.snapshot([12])[12]["gpu_percent"])
            self.assertLess(time.monotonic() - start, 0.1)
        finally:
            release.set()
            self.assertTrue(sampler.close())

    def test_stale_sample_is_not_reported_as_current(self):
        sampler = self.sampler_without_thread(lambda: None)
        sampler._values = {12: (3.0, "engine")}
        sampler._status = "ok"
        sampler._sample_monotonic = time.monotonic() - 4
        result = sampler.snapshot([12])[12]
        self.assertEqual(result["gpu_status"], "stale")
        self.assertIsNone(result["gpu_percent"])


if __name__ == "__main__":
    unittest.main()
