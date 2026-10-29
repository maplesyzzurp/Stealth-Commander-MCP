"""Synthetic published frames only. No real windows, screenshots or producer launches."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from urllib.parse import urlencode

import cached_capture as c
import dashboard as d
import desktop_watch as w
import stealth_commander_mcp as m

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j4p0AAAAASUVORK5CYII=")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "captures").mkdir()
        self.now = time.time()
        self.identity = ["PyCharmHarness_Synthetic", 101, self.now - 100, str(self.root / "anchor.exe")]
        self.watch = {"schema": "desktop-watch-v1", "watch": str(self.root), "source_identity": self.identity,
                      "url": "http://127.0.0.1:12345/" + "t" * 43, "server_pid": 202,
                      "server_created": self.now - 50, "worker_pid": 303, "worker_created": self.now - 40,
                      "scope": "session", "source": None, "anchor": None}
        self.state = {"schema": "rea-browser-v1", "session": str(self.root), "desktop": self.identity[0],
                      "pid": 101, "worker_pid": 303, "worker_created": self.now - 40, "status": "running", "heartbeat": self.now}
        image = self.root / "captures" / "synthetic.png"
        image.write_bytes(PNG)
        self.record = {"path": str(image), "sha256": hashlib.sha256(PNG).hexdigest(), "width": 1, "height": 1,
                       "method": "PrintWindow", "pixel_edits": False, "desktop": self.identity[0],
                       "input_desktop": "SyntheticInput", "hwnd": 42, "pid": 101, "rect": [0, 0, 1, 1],
                       "captured_at": self.now, "source_identity": self.identity, "capture_scope": "session",
                       "process_created": self.identity[2], "process_executable": self.identity[3], "display_affinity": 0}
        self.layout = {"schema": "desktop-layout-v1", "capture_sequence": 1, "captured_at": self.now,
                       "bounds": [0, 0, 1, 1], "complete": True, "omitted_count": 0,
                       "windows": [{"hwnd": 42, "pid": 101, "rect": [0, 0, 1, 1], "title": "Synthetic", "z_index": 0,
                                    "status": "captured", "capture": self.record}]}
        self.source = {"id": "a" * 16, "root": self.root, "name": "Synthetic", "config": c.approved_config(self.watch)}
        self.write_all()

    def write_all(self):
        for path, value in ((self.root / "watch.json", self.watch), (self.root / "browser-session.json", self.state),
                            (self.root / "captures" / "desktop.json", self.layout)):
            path.write_text(json.dumps(value), encoding="utf8")

    def export(self, cache, exact=True):
        with mock.patch.object(c, "verify_window_binding"):
            return cache.export(self.source, 42, self.record["sha256"] if exact else None,
                                1 if exact else None, mock.Mock())


class CachedReadTests(Fixture):
    def test_export_returns_exact_bytes_and_provenance_without_target_calls_or_writes(self):
        cache = c.ObservationCache()
        self.assertEqual(len(cache.publish(self.source)), 1)
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with mock.patch.object(w, "_grab_window_raw", side_effect=AssertionError("No capture")), mock.patch.object(c.h, "capture", side_effect=AssertionError("No target render"), create=True), mock.patch.object(c.h, "windows", side_effect=AssertionError("No window action")), mock.patch.object(c.h, "native_modules", side_effect=AssertionError("No initialization")):
            result = self.export(cache)
        self.assertEqual(base64.b64decode(result["data"]), PNG)
        self.assertEqual(result["provenance"]["capture_sequence"], 1)
        self.assertEqual(result["provenance"]["sha256"], hashlib.sha256(PNG).hexdigest())
        self.assertEqual(result["provenance"]["source"], "published-observer-frame")
        self.assertEqual(before, {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_latest_is_one_read_and_exact_never_substitutes(self):
        cache = c.ObservationCache()
        self.assertEqual(self.export(cache, exact=False)["provenance"]["capture_sequence"], 1)
        with self.assertRaises(c.Unavailable):
            cache.export(self.source, 42, "f" * 64, 1, mock.Mock())
        with self.assertRaises(c.Unavailable):
            cache.export(self.source, 42, self.record["sha256"], None, mock.Mock())

    def test_missing_legacy_identity_or_affinity_metadata_fails_without_image(self):
        for key in ("process_created", "process_executable", "source_identity", "display_affinity"):
            with self.subTest(key=key):
                saved = self.record.pop(key)
                self.write_all()
                self.assertEqual(c.ObservationCache().publish(self.source), [])
                self.record[key] = saved

    def test_capture_and_publication_times_must_each_be_fresh_and_not_future(self):
        for target, key in ((self.record, "captured_at"), (self.layout, "captured_at"), (self.state, "heartbeat")):
            for value in (self.now - 4, self.now + 5, float("nan")):
                with self.subTest(key=key, value=value):
                    saved = target[key]
                    target[key] = value
                    self.write_all()
                    self.assertEqual(c.ObservationCache().publish(self.source), [])
                    target[key] = saved

    def test_session_desktop_source_identity_and_errors_fail_closed(self):
        for target, key, value in ((self.state, "desktop", "Default"), (self.state, "status", "stopped"),
                                   (self.state, "worker_pid", 999), (self.state, "error", "Synthetic"),
                                   (self.record, "desktop", "Default"), (self.record, "pid", 999),
                                   (self.record, "input_desktop", self.identity[0]), (self.record, "display_affinity", 17)):
            with self.subTest(key=key, value=value):
                saved, existed = target.get(key), key in target
                target[key] = value
                self.write_all()
                self.assertEqual(c.ObservationCache().publish(self.source), [])
                if existed:
                    target[key] = saved
                else:
                    del target[key]

    def test_invalid_paths_symlinks_reparse_and_opened_path_mismatch_are_denied(self):
        original = self.record["path"]
        for path in (str(self.root.parent / "outside.png"), "../outside.png", "https://attacker.invalid/a.png", r"\\attacker.invalid\share\a.png"):
            self.record["path"] = path
            self.write_all()
            self.assertEqual(c.ObservationCache().publish(self.source), [])
        self.record["path"] = original
        self.write_all()
        with mock.patch.object(c, "_opened_path", return_value=str(self.root.parent / "outside.png")):
            self.assertEqual(c.ObservationCache().publish(self.source), [])
        real_lstat = Path.lstat
        def reparse(path):
            if path == Path(original):
                return types.SimpleNamespace(st_mode=0o100600, st_file_attributes=0x400)
            return real_lstat(path)
        with mock.patch.object(Path, "lstat", new=reparse):
            self.assertEqual(c.ObservationCache().publish(self.source), [])
        link = self.root / "captures" / "link.png"
        try:
            link.symlink_to(Path(original))
        except OSError:
            return  # The reparse guard above still executes on hosts without symlink privileges.
        self.record["path"] = str(link)
        self.write_all()
        self.assertEqual(c.ObservationCache().publish(self.source), [])

    def test_hash_pixel_dimensions_size_and_changed_manifest_are_denied(self):
        for key, value in (("sha256", "f" * 64), ("width", 999999), ("height", 999999)):
            saved = self.record[key]
            self.record[key] = value
            self.write_all()
            self.assertEqual(c.ObservationCache().publish(self.source), [])
            self.record[key] = saved
        self.write_all()
        read = c._read_regular
        def changed(path, root, limit):
            raw = read(path, root, limit)
            return raw + b" " if Path(path).name == "desktop.json" else raw
        with mock.patch.object(c, "_read_regular", side_effect=changed):
            # Both reads must agree; this stable change remains valid JSON.
            self.assertEqual(len(c.ObservationCache().publish(self.source)), 1)
        calls = [0]
        def changing(path, root, limit):
            raw = read(path, root, limit)
            if Path(path).name == "desktop.json":
                calls[0] += 1
                return raw + b" " * calls[0]
            return raw
        with mock.patch.object(c, "_read_regular", side_effect=changing):
            self.assertEqual(c.ObservationCache().publish(self.source), [])
        with mock.patch.object(c, "MAX_FRAME_BYTES", 1):
            self.assertEqual(c.ObservationCache().publish(self.source), [])

    def test_60fps_publication_preserves_exact_ref_after_one_second_then_expires(self):
        cache = c.ObservationCache()
        self.assertEqual(len(cache.publish(self.source)), 1)
        stop = threading.Event()
        counter = [1]
        def publish():
            while not stop.wait(1 / 60):
                counter[0] += 1
                fresh = {**self.layout, "capture_sequence": counter[0], "captured_at": time.time()}
                temporary = self.root / "captures" / "desktop.tmp"
                temporary.write_text(json.dumps(fresh))
                os.replace(temporary, self.root / "captures" / "desktop.json")
        thread = threading.Thread(target=publish, daemon=True)
        thread.start()
        try:
            time.sleep(1)
            result = self.export(cache)
            self.assertGreater(counter[0], 30)
            self.assertEqual(result["provenance"]["capture_sequence"], 1)
            self.assertEqual(base64.b64decode(result["data"]), PNG)
        finally:
            stop.set()
            thread.join(2)
        with mock.patch.object(c.time, "time", return_value=self.now + 3.01):
            with self.assertRaises(c.Unavailable):
                self.export(cache)

    def test_concurrent_readers_get_identical_immutable_bytes_and_cache_is_bounded(self):
        cache = c.ObservationCache()
        cache.publish(self.source)
        with mock.patch.object(c, "verify_window_binding"):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: cache.export(self.source, 42, self.record["sha256"], 1, mock.Mock()), range(16)))
        self.assertEqual({row["data"] for row in results}, {base64.b64encode(PNG).decode()})
        small = c.ObservationCache(max_bytes=len(PNG) - 1)
        self.assertEqual(small.publish(self.source), [])
        self.assertLessEqual(small._bytes, len(PNG) - 1)


class IdentityTests(Fixture):
    def test_native_session_export_uses_guardian_job_membership_after_parent_exit(self):
        self.watch['source'] = str(self.root)
        (self.root / 'native-session.json').write_text('{}')
        self.source['config'] = c.approved_config(self.watch)
        record = {**self.record, 'pid': 404, 'process_created': self.now - 10}
        child = {'pid': 404, 'created': record['process_created'], 'exe': record['process_executable']}
        fresh = {'schema': 'stealth-native-v1', 'desktop': self.identity[0], 'pid': self.identity[1],
                 'process_created': self.identity[2], 'ide_executable': self.identity[3],
                 'processes': [child], 'windows': [{'hwnd': record['hwnd'], **child}]}
        process = types.SimpleNamespace(pid=404, create_time=lambda: record['process_created'],
            exe=lambda: record['process_executable'], is_running=lambda: True, parents=lambda: [])
        anchor = types.SimpleNamespace(pid=101)
        with (mock.patch.object(c.h, 'psutil', types.SimpleNamespace(Process=lambda pid: process), create=True),
              mock.patch.object(c.h, 'owned_process', return_value=anchor),
              mock.patch.object(c.h, 'input_desktop', return_value='SyntheticInput'),
              mock.patch.object(w, 'source_state', return_value=fresh),
              mock.patch.object(c, '_verify_at_observer')):
            c.verify_window_binding(self.source['config'], record, self.source)
            fresh['processes'] = []
            with self.assertRaises(c.Unavailable):
                c.verify_window_binding(self.source['config'], record, self.source)
            fresh['processes'] = [child]
            fresh['pid'] = 999
            with self.assertRaises(c.Unavailable):
                c.verify_window_binding(self.source['config'], record, self.source)

    def test_controller_rejects_wrong_scope_process_or_input_desktop_and_requires_worker(self):
        process = mock.Mock(pid=101)
        process.create_time.return_value = self.identity[2]
        process.exe.return_value = self.identity[3]
        process.is_running.return_value = True
        process.parents.return_value = []
        anchor = mock.Mock(pid=101)
        psutil = types.SimpleNamespace(Process=mock.Mock(return_value=process))
        with (mock.patch.object(c.h, "psutil", psutil, create=True),
              mock.patch.object(c.h, "owned_process", return_value=anchor),
              mock.patch.object(c.h, "input_desktop", return_value="SyntheticInput") as input_desktop,
              mock.patch.object(c, "_verify_at_observer") as verify):
            c.verify_window_binding(self.source["config"], self.record, self.source)
            verify.assert_called_once()
            input_desktop.return_value = self.identity[0]
            with self.assertRaises(c.Unavailable):
                c.verify_window_binding(self.source["config"], self.record, self.source)
            input_desktop.return_value = "SyntheticInput"
            process.create_time.return_value = self.identity[2] + 1
            with self.assertRaises(c.Unavailable):
                c.verify_window_binding(self.source["config"], self.record, self.source)
            process.create_time.return_value = self.identity[2]
            verify.side_effect = c.Unavailable("Permission rejected by observer")
            with self.assertRaises(c.Unavailable):
                c.verify_window_binding(self.source["config"], self.record, self.source)
            verify.side_effect = None
            process.pid = 999
            with self.assertRaises(c.Unavailable):
                c.verify_window_binding(self.source["config"], self.record, self.source)
            process.pid = 101
            with self.assertRaises(c.Unavailable):
                c.verify_window_binding({**self.source["config"], "scope": "other"}, self.record, self.source)

    def test_protected_producer_never_calls_rendering_or_publishes_new_image(self):
        process = mock.Mock()
        process.create_time.return_value = self.identity[2]
        process.exe.return_value = self.identity[3]
        target = {"hwnd": 42, "pid": 101, "title": "Synthetic", "rect": [0, 0, 1, 1]}
        gui = types.SimpleNamespace(GetTopWindow=lambda _: 0, IsIconic=lambda _: False)
        with mock.patch.object(w, "_ensure_thread_dpi"), mock.patch.object(w.h, "assert_isolation"), mock.patch.object(w.h, "windows", return_value=[target]), mock.patch.object(w.h, "target_window", return_value=target), mock.patch.object(w.h, "win32gui", gui, create=True), mock.patch.object(w.h, "psutil", types.SimpleNamespace(Process=lambda _: process), create=True), mock.patch.object(c, "display_affinity_allowed", return_value=False), mock.patch.object(w, "_grab_window_raw") as render:
            staged = w._stage_frame(self.root, {"desktop": self.identity[0]}, capture_sequence=1)
        render.assert_not_called()
        self.assertEqual(staged["staged_entries"][0][0]["status"], "unavailable")

    def test_native_session_capture_rejects_reused_job_pid_before_rendering(self):
        target = {"hwnd": 42, "pid": 101, "title": "Synthetic", "rect": [0, 0, 1, 1]}
        expected = {'pid': 101, 'created': self.identity[2], 'exe': self.identity[3]}
        process = types.SimpleNamespace(create_time=lambda: self.identity[2] + 1, exe=lambda: self.identity[3])
        gui = types.SimpleNamespace(GetTopWindow=lambda _: 0, IsIconic=lambda _: False)
        with (mock.patch.object(w, '_ensure_thread_dpi'), mock.patch.object(w.h, 'assert_isolation'),
              mock.patch.object(w, 'desktop_windows', return_value=[target]),
              mock.patch.object(w, 'desktop_target', return_value=target),
              mock.patch.object(w.h, 'win32gui', gui, create=True),
              mock.patch.object(w.h, 'psutil', types.SimpleNamespace(Process=lambda _: process), create=True),
              mock.patch.object(w, '_grab_window_raw', side_effect=AssertionError('No rendering')) as render):
            staged = w._stage_frame(self.root, {'desktop': self.identity[0]}, session_pids={101: expected})
        render.assert_not_called()
        self.assertEqual(staged['staged_entries'][0][0]['status'], 'unavailable')
        self.assertIn('identity changed', staged['staged_entries'][0][0]['error'])


class VerifierClientTests(Fixture):
    def setUp(self):
        super().setUp()
        self.requests = []
        self.state.update(verifier_port=23456, verifier_token="v" * 43)
        self.write_all()
        self.listener = types.SimpleNamespace(status="LISTEN", laddr=types.SimpleNamespace(
            ip="127.0.0.1", port=23456))
        # psutil 5.9.8 has Process.connections(), without Process.net_connections().
        self.worker = types.SimpleNamespace(is_running=lambda: True,
            create_time=lambda: self.watch["worker_created"], exe=lambda: sys.executable,
            connections=lambda kind: [self.listener])
        connection = types.SimpleNamespace(
            request=lambda method, path, body, headers: self.requests.append(json.loads(body)),
            getresponse=lambda: types.SimpleNamespace(status=200, read=lambda limit: b'{"ok":true}'),
            close=lambda: None)
        psutil = types.SimpleNamespace(Process=lambda pid: self.worker, CONN_LISTEN="LISTEN", Error=RuntimeError)
        for patcher in (mock.patch.object(c.h, "psutil", psutil, create=True),
                        mock.patch.object(c.h, "input_desktop", return_value="SyntheticInput"),
                        mock.patch("http.client.HTTPConnection", return_value=connection)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_pinned_psutil_worker_connections_api_can_verify_current_window(self):
        c._verify_at_observer(self.source, self.record)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["hwnd"], self.record["hwnd"])

    def test_modern_psutil_worker_connections_api_can_verify_current_window(self):
        self.worker.net_connections = self.worker.connections
        self.worker.connections = lambda kind: (_ for _ in ()).throw(AssertionError("Legacy API used"))
        c._verify_at_observer(self.source, self.record)
        self.assertEqual(len(self.requests), 1)

    def test_unowned_listener_or_reused_worker_never_contacts_verifier(self):
        for field, value in (("connections", lambda kind: []),
                             ("create_time", lambda: self.watch["worker_created"] + 1)):
            with self.subTest(field=field), mock.patch.object(self.worker, field, value):
                with self.assertRaises(c.Unavailable):
                    c._verify_at_observer(self.source, self.record)
        self.assertEqual(self.requests, [])


class HttpAndMcpTests(Fixture):
    def start_dashboard(self):
        self.source["config"] = c.approved_config(self.watch)
        server = ThreadingHTTPServer(("127.0.0.1", 0), d.make_handler([self.source], "t" * 43))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(2)))
        return server

    def request(self, server, query, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", "/" + "t" * 43 + "/capture?" + urlencode(query), headers=headers or {})
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_dashboard_control_is_cached_only_and_denies_cross_origin_and_bad_selection(self):
        server = self.start_dashboard()
        args = {"desktop_id": self.source["id"], "hwnd": 42}
        with mock.patch.object(d, "check_processes"), mock.patch.object(c, "verify_window_binding"):
            self.assertEqual(self.request(server, args)[0], 200)
            self.assertEqual(self.request(server, args, {"Origin": "https://attacker.invalid"})[0], 403)
            self.assertEqual(self.request(server, args, {"Sec-Fetch-Site": "cross-site"})[0], 403)
            self.assertEqual(self.request(server, {**args, "path": "../secret"})[0], 400)
            self.assertEqual(self.request(server, {**args, "desktop_id": "b" * 16})[0], 409)

    def test_mcp_image_once_no_startup_no_output_write_and_full_provenance(self):
        server = self.start_dashboard()
        url = f"http://127.0.0.1:{server.server_port}/" + "t" * 43
        args = {"desktop_id": self.source["id"], "hwnd": 42}
        with mock.patch.object(d, "check_processes"), mock.patch.object(c, "verify_window_binding"), mock.patch.object(m, "_saved_dashboard_url", return_value=url), mock.patch.object(m, "ensure_dashboard", side_effect=AssertionError("No startup")), mock.patch.object(m, "_spawn_detached", side_effect=AssertionError("No launch")), mock.patch.object(w, "_grab_window_raw", side_effect=AssertionError("No rendering")):
            response = m.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "capture_observation", "arguments": args}})["result"]
        self.assertFalse(response["isError"])
        self.assertEqual([block["type"] for block in response["content"]], ["image", "text"])
        self.assertEqual(base64.b64decode(response["content"][0]["data"]), PNG)
        self.assertNotIn("data", response["structuredContent"])
        self.assertEqual(response["structuredContent"]["provenance"]["desktop_id"], args["desktop_id"])

    def test_mcp_refuses_redirect_non_loopback_or_arbitrary_url_and_unknown_arguments(self):
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "https://attacker.invalid/sink")
                self.send_header("Content-Length", "0")
                self.end_headers()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        args = {"desktop_id": self.source["id"], "hwnd": 42}
        try:
            for url in (f"http://127.0.0.1:{server.server_port}/" + "t" * 43, "https://attacker.invalid/" + "t" * 43):
                with mock.patch.object(m, "_saved_dashboard_url", return_value=url):
                    self.assertFalse(m._read_observability_frame(args)["ok"])
            self.assertFalse(m._read_observability_frame({**args, "url": "https://attacker.invalid"})["ok"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
