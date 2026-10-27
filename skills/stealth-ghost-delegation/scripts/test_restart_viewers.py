"""Test restart boundaries with synthetic processes; no live service is stopped."""

import argparse
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import restart_dashboard as restart


class Process:
    pid = 123

    def __init__(self, script, state, flag="--watch"):
        self.created = 100.5
        self.working_directory = state
        self.command = [sys.executable, "-B", str(script), "--serve", flag, str(state)]
        self.listeners = [SimpleNamespace(status="LISTEN", laddr=SimpleNamespace(ip="127.0.0.1", port=48123))]
        self.waited = False

    def create_time(self):
        return self.created

    def exe(self):
        return sys.executable

    def cmdline(self):
        return self.command

    def cwd(self):
        return str(self.working_directory)

    def net_connections(self, kind):
        return self.listeners

    def wait(self, timeout):
        self.waited = True


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.url = "http://127.0.0.1:48123/" + "a" * 43
        self.process = Process(restart.SCRIPTS / "restart_observer.py", self.folder)
        self.owner = 123
        self.h = SimpleNamespace(psutil=SimpleNamespace(
            Process=lambda pid: self.process, CONN_LISTEN="LISTEN", TimeoutExpired=TimeoutError,
            net_connections=lambda kind: [SimpleNamespace(status="LISTEN", pid=self.owner,
                laddr=SimpleNamespace(ip="127.0.0.1", port=48123))]))
        self.config = {"server_pid": 123, "server_created": 100.5, "url": self.url,
                       "source_identity": ["StealthREA_test", 456, 99.0, sys.executable]}
        self.watch = SimpleNamespace(h=self.h, config_at=lambda folder: dict(self.config),
            observer_source=lambda config: {"pid": 456}, identity=lambda source: tuple(self.config["source_identity"]),
            read=lambda path: {"status": "stopped", "cleanup": {"active_processes": 0, "worker_alive": False}})

    def test_only_canonical_token_loopback_urls(self):
        self.assertEqual(restart.loopback_url(self.url), (48123, "a" * 43))
        for url in (self.url.replace("127.0.0.1", "example.com"),
                    self.url.replace("127.0.0.1", "user@127.0.0.1"),
                    self.url.replace("127.0.0.1", "localhost"),
                    self.url.replace(":48123", ":048123"),
                    self.url.replace(":48123", ":0"),
                    self.url.replace(":48123", ":65536"),
                    self.url.replace("http:", "https:"), self.url + "?", self.url + "#",
                    self.url + "?next=https://example.com", self.url + "/workspace", "\n" + self.url):
            with self.subTest(url=url), self.assertRaises(ValueError):
                restart.loopback_url(url)

    def test_owned_observer_stops_cooperatively(self):
        restart.stop_observer(self.watch, self.folder, self.config)
        self.assertTrue((self.folder / "stop").is_file())
        self.assertTrue(self.process.waited)

    def test_relative_launcher_uses_server_working_directory(self):
        self.process.working_directory = restart.SCRIPTS.parent
        self.process.command[2] = "scripts/restart_observer.py"
        accepted = restart.observer_server(self.watch, self.folder, self.config)
        self.assertEqual(accepted.pid, 123)
        self.process.working_directory = self.folder
        with self.assertRaisesRegex(ValueError, "launcher"):
            restart.observer_server(self.watch, self.folder, self.config)

    def test_relative_state_uses_server_working_directory(self):
        self.process.working_directory = self.folder.parent
        self.process.command[-1] = self.folder.name
        self.assertEqual(restart.observer_server(self.watch, self.folder, self.config).pid, 123)
        self.process.working_directory = self.folder
        with self.assertRaisesRegex(ValueError, "another state"):
            restart.observer_server(self.watch, self.folder, self.config)

    def test_legacy_psutil_listener_api_is_supported(self):
        self.process.connections = self.process.net_connections
        self.process.net_connections = None
        self.assertEqual(restart.observer_server(self.watch, self.folder, self.config).pid, 123)

    def test_stale_pid_or_unrelated_launcher_cannot_request_stop(self):
        for mutation in ("created", "launcher", "state", "duplicate_state", "listener", "owner", "source_overlap"):
            with self.subTest(mutation=mutation):
                self.setUp()
                if mutation == "created":
                    self.process.created += 1
                elif mutation == "launcher":
                    self.process.command[2] = str(self.folder / "unrelated.py")
                    self.process.command.append(str(restart.SCRIPTS / "restart_observer.py"))
                elif mutation == "state":
                    self.process.command[-1] = str(self.folder / "other-watch")
                elif mutation == "duplicate_state":
                    self.process.command += ["--watch", str(self.folder)]
                elif mutation == "listener":
                    self.process.listeners[0].laddr.ip = "0.0.0.0"
                elif mutation == "owner":
                    self.owner = 999
                else:
                    self.config["source_identity"][1] = 123
                with self.assertRaises(ValueError):
                    restart.stop_observer(self.watch, self.folder, self.config)
                self.assertFalse((self.folder / "stop").exists())
                self.assertFalse(self.process.waited)

    def test_metadata_change_cannot_request_stop(self):
        changed = dict(self.config, server_pid=987)
        self.watch.config_at = lambda folder: changed
        with self.assertRaises(ValueError):
            restart.stop_observer(self.watch, self.folder, self.config)
        self.assertFalse((self.folder / "stop").exists())

    def retired_record(self):
        class NoSuchProcess(Exception):
            pass
        def missing(pid):
            raise NoSuchProcess(pid)
        self.h.psutil.NoSuchProcess = NoSuchProcess
        self.h.psutil.Process = missing
        self.config["worker_pid"] = 789
        self.health = {"schema": "rea-browser-v1", "session": str(self.folder), "status": "stopped",
                       "desktop": "StealthREA_test", "pid": 456, "worker_pid": 789,
                       "cleanup": {"active_processes": 0, "worker_alive": False}}
        self.watch.read = lambda path: self.health
        (self.folder / "stop").touch()

    def test_retired_record_can_be_skipped_on_repeated_restarts(self):
        self.retired_record()
        discovery = self.folder / "discovery"
        retired = discovery / "registry-previous" / "watch-retired"
        retired.mkdir(parents=True)
        (retired / "watch.json").write_text("{}", encoding="utf-8")
        (retired / "stop").touch()
        self.health["session"] = str(retired)
        state = self.folder / "dashboard.json"
        state.write_text(json.dumps({"schema": "desktop-dashboard-v1", "status": "running",
                                    "url": self.url, "pid": 999, "created": 100.5}), encoding="utf-8")
        missing = self.h.psutil.Process
        self.process.created = 200.0
        self.process.terminate = lambda: self.fail("A process was terminated during preflight.")
        self.h.psutil.Process = lambda pid: self.process if pid == 999 else missing(pid)
        self.h.dependencies = lambda install: None
        self.h.native_modules = lambda: None
        dashboard = SimpleNamespace(h=self.h, approve_watch=lambda path, name: {
            "config": {"source_identity": self.config["source_identity"]}})
        with patch.dict(sys.modules, {"dashboard": dashboard, "desktop_watch": self.watch}):
            for attempt in range(2):
                with self.subTest(attempt=attempt), self.assertRaisesRegex(ValueError, "process identity changed"):
                    restart.main(["--state", str(state), "--runtime-dir", str(self.folder),
                                  "--watch", str(self.folder), "--discover-root", str(discovery)])
        self.assertEqual((retired / "watch.json").read_text(encoding="utf-8"), "{}")
        self.assertTrue((retired / "stop").is_file())
        self.assertFalse(self.process.waited)

    def test_retired_record_with_live_server_or_unknown_worker_is_rejected(self):
        self.retired_record()
        missing = self.h.psutil.Process
        for live_pid in (123, 789):
            self.h.psutil.Process = lambda pid: self.process if pid == live_pid else missing(pid)
            with self.subTest(live_pid=live_pid), self.assertRaises(ValueError):
                restart.confirmed_stopped(self.watch, self.folder, self.config)
        self.assertTrue((self.folder / "stop").is_file())
        self.assertFalse(self.process.waited)

    def test_retired_record_requires_complete_cleanup_proof(self):
        self.retired_record()
        del self.health["cleanup"]
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            restart.confirmed_stopped(self.watch, self.folder, self.config)
        self.assertTrue((self.folder / "stop").is_file())
        self.assertFalse(self.process.waited)

    def test_dashboard_arguments_preserve_requested_paths_and_port(self):
        args = argparse.Namespace(state=Path("state.json"), runtime_dir=Path("runtime"),
            discover_root=Path("discover"), scan_root=[Path("scan one"), Path("scan two")],
            watch=[Path("watch one"), Path("watch two")], name=["First", "Second"])
        self.assertEqual(restart.dashboard_command(args, 48123), [
            "--port", "48123", "--status-file", "state.json", "--discover-root", "discover",
            "--scan-root", "scan one", "--scan-root", "scan two", "--watch", "watch one", "--name", "First",
            "--watch", "watch two", "--name", "Second"])
        command = restart.restart_command(args)
        self.assertEqual(command[:3], [sys.executable, "-B", str(restart.SCRIPTS / "restart_dashboard.py")])
        self.assertEqual(command[3:], ["--state", "state.json", "--runtime-dir", "runtime", "--serve",
            "--discover-root", "discover", "--scan-root", "scan one", "--scan-root", "scan two",
            "--watch", "watch one", "--name", "First", "--watch", "watch two", "--name", "Second"])

    def test_dashboard_stale_state_never_terminates_a_process(self):
        state = self.folder / "dashboard.json"
        state.write_text(json.dumps({"schema": "desktop-dashboard-v1", "status": "running",
                                    "url": self.url, "pid": 123, "created": 100.5}), encoding="utf-8")
        self.process.command = [sys.executable, "-B", str(restart.SCRIPTS / "dashboard.py"),
                                "--status-file", str(state)]
        self.process.created = 200.0
        self.process.terminate = lambda: self.fail("An unrelated process was terminated.")
        self.h.dependencies = lambda install: None
        self.h.native_modules = lambda: None
        dashboard = SimpleNamespace(h=self.h, approve_watch=lambda path, name: {
            "config": {"source_identity": self.config["source_identity"]}})
        with patch.dict(sys.modules, {"dashboard": dashboard, "desktop_watch": self.watch}):
            with self.assertRaisesRegex(ValueError, "identity changed"):
                restart.main(["--state", str(state), "--runtime-dir", str(self.folder),
                              "--watch", str(self.folder)])


if __name__ == "__main__":
    unittest.main()
