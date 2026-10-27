"""Focused localhost boundary tests for observe.py; no desktop or app required."""

import base64
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import struct
import tempfile
import threading
import time
import unittest
import zlib

import observe
from PIL import Image


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/"
    "7xoAAAAASUVORK5CYII="
)


def rgba_png(color):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00" + bytes(color))) + chunk(b"IEND", b"")


class ObserveHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.captures = self.root / "captures"
        self.captures.mkdir()
        self.png_path = self.captures / "frame.png"
        self.png_path.write_bytes(PNG)
        self.digest = hashlib.sha256(PNG).hexdigest()
        (self.root / "browser-session.json").write_text(json.dumps({
            "schema": "rea-browser-v1", "desktop": "private-desktop-name", "status": "running"
        }), encoding="utf-8")
        self._write_manifest(self.png_path)
        self.token = "test-token"
        self.server = observe.http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), observe.make_handler(self.root, self.token)
        )
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.temp.cleanup()

    def _write_manifest(self, path, captured_at=None):
        (self.captures / "latest.json").write_text(json.dumps({
            "path": str(path.resolve()), "sha256": self.digest,
            "captured_at": time.time() if captured_at is None else captured_at,
            "method": "PrintWindow", "desktop": "private-desktop-name",
            "input_desktop": "user-desktop-name"
        }), encoding="utf-8")

    def _write_layout(self, windows, *, bounds=(-10, -10, 600, 500), complete=True, omitted_count=0):
        (self.captures / "desktop.json").write_text(json.dumps({
            "schema": "desktop-layout-v1", "captured_at": time.time(), "bounds": list(bounds),
            "complete": complete, "windows": windows, "omitted_count": omitted_count
        }), encoding="utf-8")

    @staticmethod
    def _window(hwnd, pid, title, rect, z_index, status="captured", capture=None):
        item = {"hwnd": hwnd, "pid": pid, "title": title, "rect": list(rect),
                "z_index": z_index, "status": status}
        if capture is not None:
            item["capture"] = capture
        return item

    @staticmethod
    def _window_capture(path, data):
        return {"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest(),
                "captured_at": time.time(), "method": "PrintWindow"}

    def _request(self, target, host=None, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(method, target, headers={"Host": self.host if host is None else host})
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def test_valid_page_metadata_and_hash_checked_frame(self):
        status, page, headers = self._request("/" + self.token)
        self.assertEqual(status, 200)
        self.assertIn(b"Live JPEG stream", page)
        self.assertIn(b"This page cannot control the session.", page)
        self.assertNotIn(b"https://", page)
        self.assertNotIn(b"http://", page)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), None)

        status, payload, _ = self._request("/" + self.token + "/meta")
        self.assertEqual(status, 200)
        metadata = json.loads(payload)
        self.assertEqual(metadata["sha256"], self.digest)
        self.assertTrue(metadata["available"])
        self.assertNotIn(str(self.root), payload.decode())
        self.assertNotIn("desktop", metadata)

        status, image, headers = self._request("/" + self.token + "/frame?sha256=" + self.digest)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(image, PNG)

    def test_bad_token_host_and_traversal_are_rejected(self):
        self.assertEqual(self._request("/wrong/meta")[0], 404)
        self.assertEqual(self._request("/" + self.token + "/meta", host="attacker.example")[0], 403)
        self.assertEqual(self._request("/" + self.token + "/../meta")[0], 404)
        self.assertEqual(self._request("/" + self.token + "/%2e%2e/meta")[0], 404)
        self.assertEqual(self._request("/" + self.token + "/frame?sha256=" + "0" * 64)[0], 409)
        self.assertEqual(self._request("/" + self.token + "/meta", method="POST")[0], 405)
        self.assertEqual(self._request("/" + self.token + "/stream.mjpg", host="attacker.example")[0], 403)
        self.assertEqual(self._request("/" + self.token + "/stream.mjpg", method="POST")[0], 405)

    def test_panel_discovery_updates_and_layout_switch_in_browser_script(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the browser-script behavior check.")
        script = observe.HTML.decode("utf-8").split("<script>", 1)[1].split("</script>", 1)[0]
        harness = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
class Element {
  constructor(){this.children=[];this.hidden=false;this.dataset={};this.events={};this.attributes={};this.textContent='';}
  appendChild(child){child.parent=this;this.children.push(child);return child;}
  remove(){this.parent.children=this.parent.children.filter(child=>child!==this);}
  removeAttribute(name){delete this.attributes[name];if(name==='src')this.src='';}
  setAttribute(name,value){this.attributes[name]=value;}
  addEventListener(name,callback){this.events[name]=callback;}
}
const elements=new Map(),loads=[];
const document={getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement(){return new Element();}};
class Image extends Element {set src(value){this.url=value;loads.push(this);}get src(){return this.url;}}
const context=vm.createContext({document,Image,location:{pathname:'/test-token'},Date,Map,Set,encodeURIComponent,
  fetch:()=>new Promise(()=>{}),setInterval:()=>{},window:{}});
vm.runInContext(require('node:fs').readFileSync(0,'utf8'),context);
const run=code=>vm.runInContext(code,context),el=id=>document.getElementById(id);
const item=(hwnd,pid,title,hash)=>({hwnd,pid,title,sha256:hash,status:'captured'});
function render(windows,age=0){context.fixture={windows,captured_at:Date.now()/1000-age,complete:true,omitted_count:0};run('renderWorkspace(fixture)');}
function finish(){for(const image of loads.splice(0))image.onload();}
function childText(element){return element.textContent+' '+element.children.map(childText).join(' ');}
const first=item(10,20,'Editor','aaa'),second=item(11,21,'Notes','bbb');
render([first,second]);assert.equal(el('windowList').children.length,2);assert.equal(loads.length,2);finish();
const originalCards=[...el('windowList').children],originalImage=originalCards[0].children[1];
assert.equal(originalImage.hidden,false);assert.equal(el('liveScene').src,undefined,'panels must not open the MJPEG stream');
render([second,first]);assert.equal(loads.length,0,'unchanged images must not load again');
assert.equal(el('windowList').children[0],originalCards[0],'existing panels must keep their nodes and positions');
const changed={...first,title:'Editor updated',sha256:'ccc'};render([changed,second]);
assert.equal(loads.length,1);assert.equal(originalImage.src,'/test-token/window?hwnd=10&sha256=aaa','last image stays visible while replacement loads');
finish();assert.equal(originalImage.src,'/test-token/window?hwnd=10&sha256=ccc');
assert.ok(childText(originalCards[0]).includes('Editor updated'));
render([changed,{...second,status:'minimized'}]);assert.ok(childText(originalCards[1]).includes('Minimized'));
assert.equal(originalCards[1].children[1].hidden,false,'minimizing must retain the last captured image');
render([changed,second],8);assert.equal(originalCards[0].dataset.state,'stale');
assert.ok(childText(originalCards[0]).includes('Stale capture'));
render([{...changed,captured_at:Date.now()/1000-8},second]);assert.equal(originalCards[0].dataset.state,'stale','an old individual capture stays stale even in a fresh layout');
const failing={...changed,sha256:'ddd'};render([failing,second]);assert.equal(loads.length,1);loads.shift().onerror();
assert.equal(originalCards[0].dataset.state,'unavailable');assert.equal(originalImage.hidden,false);
render([failing,second]);assert.equal(loads.length,1,'failed capture loads retry on the next polling pass');finish();
render([failing,second,item(12,22,'New window','eee')]);assert.equal(el('windowList').children.length,3);
const late=loads.shift();render([failing,second]);late.onload();assert.equal(el('windowList').children.length,2,'late image completion cannot revive a closed window');
el('layoutView').events.click();assert.equal(el('panels').hidden,true);assert.equal(el('layoutViewContent').hidden,false);
assert.equal(el('liveScene').src,'/test-token/stream.mjpg');assert.equal(el('layoutView').attributes['aria-pressed'],'true');
assert.ok(el('viewNote').textContent.includes('target ceiling of 60 frames per second'));
el('panelsView').events.click();assert.equal(el('panels').hidden,false);assert.equal(el('liveScene').src,'');
assert.ok(el('viewNote').textContent.includes('once per second'));assert.ok(!el('viewNote').textContent.includes('60 frames'));
assert.equal(el('windowList').children[0],originalCards[0]);
run('markCardsStale()');assert.equal(originalCards[0].dataset.state,'stale');
render([{...failing,title:'<img src=x onerror=alert(1)>'},second]);assert.ok(childText(originalCards[0]).includes('<img src=x onerror=alert(1)>'));
originalCards[0].children[0].children[1].events.click();finish();assert.equal(el('detail').hidden,false);
render([second]);assert.equal(el('detail').hidden,true);assert.equal(el('windowList').children[0],originalCards[1]);
render([]);assert.equal(el('windowList').children.length,0);assert.equal(el('emptyWindows').hidden,false);
console.log('Panel discovery, stable updates, errors, stale frames, close races, and view switching passed.');
"""
        completed = subprocess.run([node, "-e", harness], input=script, text=True,
                                   capture_output=True, timeout=20,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_live_stream_is_bounded_jpeg_reconstruction_with_stack_order(self):
        red, blue, green = (rgba_png(color) for color in (
            (230, 25, 25, 255), (20, 35, 235, 255), (20, 220, 30, 255)))
        records = []
        for name, data in (("behind", red), ("front", blue), ("minimized", green)):
            path = self.captures / f"{name}.png"
            path.write_bytes(data)
            records.append(self._window_capture(path, data))
        self._write_layout([
            self._window(201, 301, "Behind", (0, 0, 160, 130), 1, capture=records[0]),
            self._window(202, 302, "Front", (40, 30, 140, 110), 0, capture=records[1]),
            self._window(203, 303, "Minimized", (170, 0, 230, 60), 2,
                         status="minimized"),
        ], bounds=(0, 0, 240, 160))

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("GET", "/" + self.token + "/stream.mjpg", headers={"Host": self.host})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(response.getheader("Content-Type").startswith("multipart/x-mixed-replace; boundary="))
            self.assertIn("reconstructed", response.getheader("X-Stream-Description"))
            header = bytearray()
            while not header.endswith(b"\r\n\r\n"):
                header.extend(response.read(1))
            self.assertTrue(header.startswith(b"--codexframe\r\n"))
            part_headers = header.decode("ascii").split("\r\n")
            fields = dict(line.split(": ", 1) for line in part_headers[1:] if ": " in line)
            self.assertEqual(fields["X-Sequence"], "1")
            self.assertEqual(fields["X-Frame-State"], "current")
            self.assertGreater(float(fields["X-Captured-At"]), 0)
            length = int(fields["Content-Length"])
            jpeg = response.read(length)
            self.assertEqual(jpeg[:2], b"\xff\xd8")
            with Image.open(io.BytesIO(jpeg)) as rendered:
                self.assertEqual(rendered.format, "JPEG")
                self.assertLessEqual(rendered.width * rendered.height, observe.MAX_RENDER_PIXELS)
                front_pixel = rendered.convert("RGB").getpixel((80, 60))
                back_pixel = rendered.convert("RGB").getpixel((15, 15))
                minimized_pixel = rendered.convert("RGB").getpixel((200, 20))
            self.assertGreater(front_pixel[2], front_pixel[0] * 2)
            self.assertGreater(back_pixel[0], back_pixel[2] * 2)
            self.assertLess(minimized_pixel[2], 90)
        finally:
            connection.close()

    def test_stale_workspace_keeps_captured_content_visible(self):
        data = rgba_png((230, 25, 25, 255))
        path = self.captures / "retained.png"
        path.write_bytes(data)
        self._write_layout([
            self._window(401, 501, "Retained", (0, 0, 200, 140), 0,
                         capture=self._window_capture(path, data))
        ], bounds=(0, 0, 240, 160))
        layout = observe._workspace(self.root)
        layout["captured_at"] = time.time() - 10
        jpeg, stale = observe._render_workspace_jpeg(self.root, layout)
        self.assertTrue(stale)
        with Image.open(io.BytesIO(jpeg)) as rendered:
            pixel = rendered.convert("RGB").getpixel((80, 80))
        self.assertGreater(pixel[0], pixel[2] * 2, "A late frame must retain the last application image.")

    def test_out_of_capture_path_and_old_capture_are_not_served_as_fresh(self):
        outside = self.root.parent / (self.root.name + "-outside.png")
        outside.write_bytes(PNG)
        try:
            self._write_manifest(outside)
            status, payload, _ = self._request("/" + self.token + "/meta")
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(payload)["available"])
        finally:
            outside.unlink(missing_ok=True)
        self._write_manifest(self.png_path, time.time() - 4)
        status, payload, _ = self._request("/" + self.token + "/meta")
        self.assertEqual(status, 200)
        metadata = json.loads(payload)
        self.assertTrue(metadata["available"])
        self.assertTrue(metadata["stale"])

    def test_windows_liveness_probe_does_not_terminate_the_session_process(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            (self.root / "browser-session.json").write_text(json.dumps({
                "schema": "rea-browser-v1", "desktop": "private-desktop-name",
                "status": "running", "pid": process.pid
            }), encoding="utf-8")
            for _ in range(2):
                status, payload, _ = self._request("/" + self.token + "/meta")
                self.assertEqual(status, 200)
                self.assertFalse(json.loads(payload)["stale"])
                self.assertIsNone(process.poll(), "metadata liveness check must not terminate the process")
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)

    def test_error_flags_and_worker_health_are_sanitized_and_visible(self):
        private_error = "private error C:/Users/secret/project"
        state_path = self.root / "browser-session.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update(pid=os.getpid(), worker_pid=os.getpid(), heartbeat=time.time() - 26,
                     error=private_error, capture_error=private_error)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        status, payload, _ = self._request("/" + self.token + "/meta")
        self.assertEqual(status, 200)
        metadata = json.loads(payload)
        self.assertTrue(metadata["session_error"])
        self.assertTrue(metadata["capture_error"])
        self.assertTrue(metadata["worker_alive"])
        self.assertTrue(metadata["worker_stale"])
        self.assertGreaterEqual(metadata["worker_heartbeat_age_seconds"], 25)
        self.assertTrue(metadata["stale"])
        self.assertEqual(metadata["message"], "Window capture failed; inspect its local logs.")
        self.assertNotIn(private_error, payload.decode())
        self.assertNotIn(str(self.root), payload.decode())

        state["status"] = "stopped"
        state.pop("capture_error")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        status, payload, _ = self._request("/" + self.token + "/meta")
        self.assertEqual(status, 200)
        metadata = json.loads(payload)
        self.assertEqual(metadata["message"], "Session stopped after an error; inspect its local logs.")
        self.assertTrue(metadata["stale"])
        self.assertNotIn(private_error, payload.decode())

    def test_workspace_sanitizes_layout_and_serves_only_approved_windows(self):
        second = self.captures / "second.png"
        second_png = rgba_png((10, 80, 190, 255))
        second.write_bytes(second_png)
        malicious = '<img src=x onerror="alert(1)">'
        self._write_layout([
            self._window(101, 201, "Behind", (0, 0, 200, 140), 1,
                         capture=self._window_capture(self.png_path, PNG)),
            self._window(102, 202, malicious, (90, 40, 260, 160), 0,
                         capture=self._window_capture(second, second_png)),
        ], bounds=(-10, -10, 300, 200), omitted_count=4)
        status, payload, _ = self._request("/" + self.token + "/workspace")
        self.assertEqual(status, 200)
        layout = json.loads(payload)
        self.assertEqual(layout["schema"], "desktop-layout-v1")
        self.assertEqual(layout["bounds"], [-10.0, -10.0, 300.0, 200.0])
        self.assertEqual([item["hwnd"] for item in layout["windows"]], [102, 101])
        self.assertEqual(layout["windows"][0]["title"], malicious)
        self.assertEqual(layout["omitted_count"], 4)
        self.assertEqual(layout["windows"][0]["sha256"], hashlib.sha256(second_png).hexdigest())
        self.assertNotIn("path", payload.decode())
        self.assertNotIn(str(self.root), payload.decode())
        page_status, page, _ = self._request("/" + self.token)
        self.assertEqual(page_status, 200)
        self.assertIn(b"textContent", page)
        self.assertNotIn(b"innerHTML", page)
        self.assertNotIn(malicious.encode(), page)
        self.assertIn(b"detailGeneration", page)
        self.assertIn(b"generation===detailGeneration", page)
        self.assertIn(b"additional window(s) not shown", page)

        digest2 = hashlib.sha256(second_png).hexdigest()
        status, image, _ = self._request("/" + self.token + f"/window?hwnd=102&sha256={digest2}")
        self.assertEqual(status, 200)
        self.assertEqual(image, second_png)
        self.assertEqual(self._request("/" + self.token + f"/window?hwnd=999&sha256={digest2}")[0], 404)
        self.assertEqual(self._request("/" + self.token + f"/window?hwnd=102&sha256={self.digest}")[0], 404)

    def test_workspace_reports_missing_minimized_and_zero_windows(self):
        missing = self.captures / "missing.png"
        self._write_layout([
            self._window(10, 20, "Gone", (0, 0, 100, 80), 0,
                         capture=self._window_capture(missing, PNG)),
            self._window(11, 21, "Minimized app", (100, 0, 200, 80), 1, status="minimized"),
            self._window(12, 22, "Unavailable app", (200, 0, 300, 80), 2, status="unavailable"),
        ], bounds=(-10, -10, 400, 120))
        status, payload, _ = self._request("/" + self.token + "/workspace")
        self.assertEqual(status, 200)
        layout = json.loads(payload)
        self.assertEqual([item["status"] for item in layout["windows"]], ["unavailable", "minimized", "unavailable"])
        self.assertEqual(layout["windows"][0]["message"], "Image file is missing.")
        self.assertEqual(self._request("/" + self.token + f"/window?hwnd=10&sha256={self.digest}")[0], 404)

        self._write_layout([], bounds=(-10, -10, 300, 120))
        status, payload, _ = self._request("/" + self.token + "/workspace")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["windows"], [])

    def test_workspace_rejects_unsafe_paths_and_unbounded_geometry(self):
        outside = self.root.parent / (self.root.name + "-workspace.png")
        outside.write_bytes(PNG)
        try:
            self._write_layout([self._window(10, 20, "Escaping", (0, 0, 100, 80), 0,
                                              capture=self._window_capture(outside, PNG))])
            self.assertEqual(self._request("/" + self.token + "/workspace")[0], 422)
        finally:
            outside.unlink(missing_ok=True)
        self._write_layout([], bounds=(0, 0, 100_000_000, 100))
        self.assertEqual(self._request("/" + self.token + "/workspace")[0], 422)

    def test_workspace_rejects_invalid_omitted_count(self):
        for value in (-1, 1.5, True, "2"):
            with self.subTest(value=value):
                self._write_layout([], omitted_count=value)
                self.assertEqual(self._request("/" + self.token + "/workspace")[0], 422)

    def test_capture_directory_symlink_escape_is_rejected(self):
        outside = Path(self.temp.name).parent / (Path(self.temp.name).name + "-outside")
        outside.mkdir()
        try:
            try:
                self.captures.rename(self.root / "captures-original")
                self.captures.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"Symlink creation unavailable: {error}")
            status, payload, _ = self._request("/" + self.token + "/meta")
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(payload)["available"])
        finally:
            if self.captures.is_symlink():
                self.captures.unlink()
            original = self.root / "captures-original"
            if original.exists():
                original.rename(self.captures)
            outside.rmdir()


if __name__ == "__main__":
    unittest.main()
