"""Synthetic capture backend/provenance regressions; no real windows or input."""
import contextlib
import ctypes
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock

from PIL import Image
import cached_capture as c
import desktop_watch as w
import observe


PIXELS = b"\x33\x22\x11\x00"


class BackendCaptureTests(unittest.TestCase):
    def raw_patches(self, backend):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        pixels = ctypes.create_string_buffer(PIXELS)
        user = types.SimpleNamespace(PrintWindow=mock.Mock(return_value=True))
        gdi = types.SimpleNamespace(GdiFlush=mock.Mock())
        gui = types.SimpleNamespace(GetWindowRect=lambda _: (0, 0, 1, 1), IsIconic=lambda _: False)
        stack.enter_context(mock.patch.object(w, "_ensure_thread_dpi"))
        stack.enter_context(mock.patch.object(w, "_capture_api", return_value=(object(), gdi, user, object())))
        stack.enter_context(mock.patch.object(w.h, "win32gui", gui, create=True))
        stack.enter_context(mock.patch.object(w, "_grab_window_dx11", return_value=(1, 1, PIXELS) if backend == "D3D11" else None))
        dib = stack.enter_context(mock.patch.object(w, "_thread_dib", return_value=(1, 2, 3, ctypes.addressof(pixels))))
        return user, gdi, dib, pixels

    def test_d3d11_success_records_backend_without_printwindow(self):
        user, gdi, dib, pixels = self.raw_patches("D3D11")
        self.assertEqual(w._grab_window_raw({"hwnd": 42}), ([0, 0, 1, 1], 1, 1, PIXELS, "D3D11"))
        user.PrintWindow.assert_not_called()
        gdi.GdiFlush.assert_not_called()
        dib.assert_not_called()

    def test_d3d11_unavailable_records_successful_printwindow_fallback(self):
        user, gdi, dib, pixels = self.raw_patches("PrintWindow")
        self.assertEqual(w._grab_window_raw({"hwnd": 42}), ([0, 0, 1, 1], 1, 1, PIXELS, "PrintWindow"))
        user.PrintWindow.assert_called_once_with(42, 1, 2)
        gdi.GdiFlush.assert_called_once_with()
        dib.assert_called_once()

    def test_failed_fallback_returns_no_capture(self):
        user, gdi, dib, pixels = self.raw_patches("PrintWindow")
        user.PrintWindow.return_value = False
        with self.assertRaisesRegex(RuntimeError, "PrintWindow failed"):
            w._grab_window_raw({"hwnd": 42})
        gdi.GdiFlush.assert_not_called()

    def test_public_capture_and_sidecar_preserve_selected_method_and_pixels(self):
        with tempfile.TemporaryDirectory() as tmp:
            for method in observe.CAPTURE_METHODS:
                with self.subTest(method=method), mock.patch.object(w, "_grab_window_raw", return_value=([0, 0, 1, 1], 1, 1, PIXELS, method)), mock.patch.object(w.h, "input_desktop", return_value="Default"):
                    path = Path(tmp) / (method + ".png")
                    record = w.capture_window({"desktop": "PyCharmHarness_Synthetic"}, {"hwnd": 42, "pid": 101}, path)
                    self.assertEqual(record["method"], method)
                    self.assertFalse(record["pixel_edits"])
                    self.assertEqual(json.loads(path.with_suffix(".capture.json").read_text()), record)
                    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), record["sha256"])
                    with Image.open(path) as image:
                        self.assertEqual(image.getpixel((0, 0)), (17, 34, 51))

    def test_encoder_refuses_missing_or_unknown_backend_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            for method in (None, "unknown", [], "<script>"):
                with self.subTest(method=method):
                    path = Path(tmp) / "invalid.png"
                    with self.assertRaisesRegex(ValueError, "backend"):
                        w._encode_window_png(path, 1, 1, PIXELS, {"method": method})
                    self.assertFalse(path.exists())

    def test_same_pixels_after_backend_change_get_new_provenance(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            folder = Path(tmp)
            (folder / "captures").mkdir()
            now = time.time()
            source = {"desktop": "PyCharmHarness_Synthetic", "pid": 101, "process_created": now - 100, "ide_executable": str(folder / "anchor.exe")}
            target = {"hwnd": 42, "pid": 101, "title": "Synthetic", "rect": [0, 0, 1, 1]}
            process = types.SimpleNamespace(create_time=lambda: now - 100, exe=lambda: source["ide_executable"], is_running=lambda: True)
            gui = types.SimpleNamespace(GetTopWindow=lambda _: 0, IsIconic=lambda _: False, GetClassName=lambda _: "Synthetic")
            for patcher in (mock.patch.object(w, "_ensure_thread_dpi"), mock.patch.object(w.h, "assert_isolation"),
                            mock.patch.object(w.h, "windows", return_value=[target]), mock.patch.object(w.h, "target_window", return_value=target),
                            mock.patch.object(w.h, "input_desktop", return_value="Default"), mock.patch.object(w.h, "win32gui", gui, create=True),
                            mock.patch.object(w.h, "psutil", types.SimpleNamespace(Process=lambda _: process), create=True),
                            mock.patch.object(c, "display_affinity_allowed", return_value=True)):
                stack.enter_context(patcher)
            raw = stack.enter_context(mock.patch.object(w, "_grab_window_raw", return_value=([0, 0, 1, 1], 1, 1, PIXELS, "D3D11")))
            cache = {}
            first = w._publish_frame(folder, w._stage_frame(folder, source, cache=cache), cache)
            first_record = first["windows"][0]["capture"]
            unchanged = w._stage_frame(folder, source, cache=cache)
            self.assertEqual(unchanged["changed_count"], 0)
            raw.return_value = ([0, 0, 1, 1], 1, 1, PIXELS, "PrintWindow")
            changed = w._stage_frame(folder, source, cache=cache)
            self.assertEqual(changed["changed_count"], 1)
            second = w._publish_frame(folder, changed, cache)
            second_record = second["windows"][0]["capture"]
            self.assertEqual((first_record["method"], second_record["method"]), ("D3D11", "PrintWindow"))
            self.assertEqual(first_record["sha256"], second_record["sha256"])
            self.assertNotEqual(first_record["path"], second_record["path"])
            self.assertEqual(json.loads(Path(second_record["path"]).with_suffix(".capture.json").read_text())["method"], "PrintWindow")


class ProvenanceValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "captures").mkdir()
        self.now = time.time()
        self.identity = ["PyCharmHarness_Synthetic", 101, self.now - 100, str(self.root / "anchor.exe")]
        self.source = {"id": "a" * 16, "config": {"source_identity": self.identity, "scope": "session"}}

    def frame(self, method):
        meta = {"method": method, "desktop": self.identity[0], "input_desktop": "Default", "hwnd": 42, "pid": 101,
                "rect": [0, 0, 1, 1], "captured_at": self.now, "source_identity": self.identity, "capture_scope": "session",
                "process_created": self.identity[2], "process_executable": self.identity[3], "display_affinity": 0}
        record = w._encode_window_png(self.root / "captures" / (method + ".png"), 1, 1, PIXELS, meta)
        item = {"hwnd": 42, "pid": 101, "rect": [0, 0, 1, 1], "title": "Synthetic", "z_index": 0,
                "status": "captured", "capture": record}
        layout = {"schema": "desktop-layout-v1", "capture_sequence": 1, "capture_monotonic": 1.0,
                  "captured_at": self.now, "bounds": [0, 0, 1, 1], "complete": True, "omitted_count": 0, "windows": [item]}
        (self.root / "captures" / "desktop.json").write_text(json.dumps(layout))
        (self.root / "captures" / "latest.json").write_text(json.dumps(record))
        return record, item, layout

    def test_both_backends_survive_viewer_and_cached_export_validation(self):
        for method in observe.CAPTURE_METHODS:
            with self.subTest(method=method):
                record, item, layout = self.frame(method)
                image = observe._capture_record(self.root, record)
                self.assertEqual(observe._capture(self.root)["capture_method"], method)
                private = observe._workspace(self.root)
                self.assertEqual(private["windows"][0]["capture"]["method"], method)
                public = observe._public_workspace(private)
                self.assertEqual(public["windows"][0]["capture_method"], method)
                self.assertNotIn(str(self.root), json.dumps(public))
                exported, actual_record, provenance = c._frame(self.root, self.source, layout, item, self.now)
                self.assertEqual(exported, image)
                self.assertEqual(provenance["recorded_method"], method)
                self.assertEqual(actual_record["sha256"], record["sha256"])

    def test_unknown_methods_and_existing_hash_path_time_checks_still_fail(self):
        record, item, layout = self.frame("D3D11")
        (self.root / "outside.png").write_bytes(Path(record["path"]).read_bytes())
        for bad in ({"method": "unknown"}, {"method": []}, {"sha256": "0" * 64},
                    {"path": str(self.root / "outside.png")}, {"captured_at": float("nan")}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                observe._capture_record(self.root, {**record, **bad})
        for bad in ({"sha256": "0" * 64}, {"width": 2}, {"desktop": "Default"}, {"display_affinity": 17}):
            with self.subTest(bad=bad), self.assertRaises(c.Unavailable):
                c._frame(self.root, self.source, layout, {**item, "capture": {**record, **bad}}, self.now)
        item["capture"] = {**record, "method": "unknown"}
        (self.root / "captures" / "desktop.json").write_text(json.dumps(layout))
        with self.assertRaises(ValueError):
            observe._workspace(self.root)

    def test_viewer_labels_real_mixed_and_unknown_backends_without_fps_promise(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js required for telemetry behavior check")
        html = observe.HTML.decode("utf-8")
        script = html.split("function captureMethodLabel(", 1)[1].split("function selectView(", 1)[0]
        script = "function captureMethodLabel(" + script
        harness = r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
const state={fpsBadge:{},tCadence:{},liveCaptureFps:null,currentLayout:null,view:'panels'};
const context=vm.createContext(state);vm.runInContext(require('node:fs').readFileSync(0,'utf8'),context);
const update=()=>vm.runInContext('updateFpsTelemetry()',context);
state.currentLayout={windows:[{status:'captured',capture_method:'D3D11'}]};update();
assert.match(state.fpsBadge.textContent,/D3D11/);assert.match(state.fpsBadge.textContent,/Measuring/);assert.doesNotMatch(state.fpsBadge.textContent,/60|GPU/);
state.currentLayout.windows.push({status:'captured',capture_method:'PrintWindow'});state.liveCaptureFps=12.34;update();
assert.match(state.fpsBadge.textContent,/D3D11 \+ PrintWindow/);assert.match(state.fpsBadge.textContent,/12\.3 FPS measured/);assert.match(state.fpsBadge.title,/target ceiling: 60 FPS/);
state.currentLayout.windows=[{status:'captured'}];state.liveCaptureFps=NaN;update();
assert.match(state.fpsBadge.textContent,/Backend unknown/);assert.doesNotMatch(state.fpsBadge.textContent,/NaN|60|GPU/);
state.currentLayout=null;vm.runInContext("updateFpsTelemetry('PrintWindow')",context);assert.match(state.fpsBadge.textContent,/PrintWindow/);
'''
        completed = subprocess.run([node, "-e", harness], input=script, text=True, capture_output=True,
                                   timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("DWM GPU", html)
        self.assertIn("target ceiling of 60 FPS", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
