"""Discovery and ownership checks with injected desktop/process backends."""

from concurrent.futures import ThreadPoolExecutor
import copy
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import desktop_registry as registry


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / "runtime"
        self.work.mkdir()
        self.now = 0.0
        self.processes = {}
        self.configs = {}
        self.desktops = []
        self.active = "Default"
        self.started = []
        self.stopped = []
        self.start_fails = False
        self.manager = None
        self.clock_patch = patch.object(registry.time, "monotonic", side_effect=lambda: self.now)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.addCleanup(self.close)

    def close(self):
        if self.manager is not None:
            self.manager.close()

    def source(self, desktop="PyCharmHarness_one", pid=10, created=10.0):
        info = {"pid": pid, "created": created, "exe": sys.executable}
        self.processes[pid] = dict(info)
        return {"desktop": desktop, "window_count": 1, **info}

    def observer(self, source, root, server=1000):
        root.mkdir(parents=True, exist_ok=True)
        self.processes[server] = {"pid": server, "created": 20.0 + server, "exe": sys.executable}
        config = {"watch": str(root), "schema": "desktop-watch-v1",
                  "source_identity": [source["desktop"], source["pid"], source["created"], source["exe"]],
                  "server_pid": server, "server_created": 20.0 + server}
        self.configs[root] = config
        return copy.deepcopy(config)

    def starter(self, source):
        self.started.append(dict(source))
        if self.start_fails:
            raise RuntimeError("private C:/secret/path should never be reported")
        index = len(self.started)
        return self.observer(source, self.work / ("watch-new" + str(index)), server=2000 + index)

    def stopper(self, root):
        root = Path(root)
        self.stopped.append(root)
        self.processes.pop(self.configs[root]["server_pid"], None)

    def make(self, initial=(), **kwargs):
        self.manager = registry.DesktopRegistry(initial, self.work,
            enumerator=lambda: {"input_desktop": self.active, "desktops": copy.deepcopy(self.desktops)},
            starter=kwargs.pop("starter", self.starter), stopper=self.stopper,
            process_info=lambda pid: self.processes.get(pid),
            watch_reader=lambda root: copy.deepcopy(self.configs[Path(root)]), **kwargs)
        return self.manager

    def test_default_active_and_unapproved_desktops_never_start(self):
        self.active = "StealthREA_active"
        self.desktops = [self.source("Default", 1), self.source("Nexgent", 2),
                         self.source("StealthREA_active", 3), self.source("PyCharmHarness_Nexgent", 4),
                         self.source("OtherDesktop", 5), self.source("StealthREA_third", 6)]
        manager = self.make()
        manager.refresh_once()
        self.assertEqual([item["desktop"] for item in self.started], ["StealthREA_third"])
        self.assertEqual(len(manager.snapshot()), 1)

    def test_existing_observer_for_same_desktop_is_reused_and_never_stopped(self):
        existing = self.source()
        config = self.observer(existing, self.root / "explicit" / "watch-existing")
        # A different visible app may become the largest window on the same desktop.
        self.desktops = [self.source(pid=11, created=11.0)]
        manager = self.make([{"root": config["watch"], "name": "My desktop", "config": config}])
        manager.refresh_once()
        self.assertFalse(self.started)
        self.assertEqual(manager.snapshot()[0]["name"], "My desktop")
        manager.close()
        self.assertFalse(self.stopped)

    def test_known_scan_root_reuses_third_observer_without_starting_duplicate(self):
        source = self.source("StealthREA_third")
        parent = self.root / "known-third"
        config = self.observer(source, parent / "watch-third")
        self.desktops = [source]
        manager = self.make(scan_roots=[parent])
        manager.refresh_once()
        self.assertEqual(manager.snapshot()[0]["root"], Path(config["watch"]))
        self.assertFalse(self.started)
        manager.close()
        self.assertFalse(self.stopped)

    def test_source_exit_removes_only_owned_observer_and_leaves_apps_alone(self):
        source = self.source()
        self.desktops = [source]
        manager = self.make()
        manager.refresh_once()
        owned = manager.snapshot()[0]["root"]
        self.processes.pop(source["pid"])
        self.desktops = []
        manager.refresh_once()
        self.assertEqual(self.stopped, [owned])
        self.assertEqual(manager.snapshot(), [])
        manager.close()
        self.assertEqual(self.stopped, [owned])

    def test_close_stops_created_observers_but_not_explicit_or_scanned_ones(self):
        first, second = self.source(), self.source("StealthREA_two", 11)
        existing = self.observer(first, self.root / "external" / "watch-existing")
        self.desktops = [first, second]
        manager = self.make([{"root": existing["watch"], "name": "Existing", "config": existing}])
        manager.refresh_once()
        created = next(item["root"] for item in manager.snapshot() if item["root"] != Path(existing["watch"]))
        manager.close()
        self.assertEqual(self.stopped, [created])
        self.assertIn(first["pid"], self.processes)
        self.assertIn(second["pid"], self.processes)
        self.assertIn(existing["server_pid"], self.processes)

    def test_failed_start_has_backoff_and_three_attempt_limit_without_private_errors(self):
        self.desktops = [self.source()]
        self.start_fails = True
        manager = self.make()
        for now in (0, 1, 4, 5, 6, 14, 15, 1000):
            self.now = now
            manager.refresh_once()
        self.assertEqual(len(self.started), 3)
        self.assertEqual(manager.status()["errors"], ["observer_start_exhausted"])
        self.assertNotIn("secret", str(manager.status()))
        self.desktops = [self.source(created=30.0)]
        self.start_fails = False
        manager.refresh_once()
        self.assertEqual(len(self.started), 4)
        self.assertEqual(len(manager.snapshot()), 1)

    def test_source_pid_reuse_replaces_observer_with_fresh_pinned_identity(self):
        self.desktops = [self.source()]
        manager = self.make()
        manager.refresh_once()
        old = manager.snapshot()[0]["root"]
        self.desktops = [self.source(created=40.0)]
        manager.refresh_once()
        self.assertEqual(self.stopped, [old])
        self.assertEqual(self.started[-1]["created"], 40.0)
        self.assertNotEqual(manager.snapshot()[0]["root"], old)

    def test_failed_post_start_identity_validation_stops_just_created_observer(self):
        source = self.source()
        other = self.source("StealthREA_other", 12, 12.0)
        self.desktops = [source]
        def mismatched_starter(candidate):
            self.started.append(dict(candidate))
            return self.observer(other, self.work / "watch-wrong-source", 2100)
        manager = self.make(starter=mismatched_starter)
        manager.refresh_once()
        self.assertEqual(manager.snapshot(), [])
        self.assertEqual(self.stopped, [self.work / "watch-wrong-source"])
        self.assertIn(source["pid"], self.processes)
        self.assertIn(other["pid"], self.processes)

    def test_discovery_failure_preserves_last_snapshot_without_stopping_sources(self):
        self.desktops = [self.source()]
        manager = self.make()
        manager.refresh_once()
        previous = manager.snapshot()
        def failed_inventory():
            raise OSError("private C:/secret/backend")
        manager._enumerator = failed_inventory
        manager.refresh_once()
        self.assertEqual(manager.snapshot(), previous)
        self.assertFalse(self.stopped)
        self.assertEqual(manager.status()["errors"], ["discovery_unavailable"])

    def test_sequence_uses_initial_creation_order_then_discovery_order(self):
        late = self.source("StealthREA_late", 11, 20.0)
        early = self.source("StealthREA_early", 10, 5.0)
        new = self.source("StealthREA_new", 12, 30.0)
        a = self.observer(late, self.root / "a" / "watch-late", 1000)
        b = self.observer(early, self.root / "b" / "watch-early", 1001)
        self.desktops = [late, early, new]
        manager = self.make([{"root": a["watch"], "name": "Late", "config": a},
                             {"root": b["watch"], "name": "Early", "config": b}])
        manager.refresh_once()
        self.assertEqual([(item["name"], item["sequence"]) for item in manager.snapshot()],
                         [("Early", 1), ("Late", 2), ("Desktop new", 3)])

    def test_reused_observer_pid_is_never_sent_a_stop_request(self):
        self.desktops = [self.source()]
        manager = self.make()
        manager.refresh_once()
        root = manager.snapshot()[0]["root"]
        server = self.configs[root]["server_pid"]
        self.processes[server]["created"] += 1
        manager.close()
        self.assertFalse(self.stopped)
        self.assertIn(server, self.processes)

    def test_empty_desktop_does_not_start_and_input_change_stops_owned_observer(self):
        source = self.source()
        source["window_count"] = 0
        self.desktops = [source]
        manager = self.make()
        manager.refresh_once()
        self.assertFalse(self.started)
        source["window_count"] = 1
        manager.refresh_once()
        self.active = source["desktop"]
        manager.refresh_once()
        self.assertEqual(len(self.stopped), 1)
        self.assertEqual(manager.snapshot(), [])

    def test_snapshot_stays_immediate_while_background_starter_is_waiting(self):
        entered, release = threading.Event(), threading.Event()
        def slow_starter(source):
            entered.set()
            if not release.wait(3):
                raise TimeoutError()
            return self.starter(source)
        self.desktops = [self.source()]
        manager = self.make(starter=slow_starter).start()
        try:
            self.assertTrue(entered.wait(2))
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertEqual(pool.submit(manager.snapshot).result(timeout=1), [])
        finally:
            release.set()
            manager.close()


if __name__ == "__main__":
    unittest.main()
