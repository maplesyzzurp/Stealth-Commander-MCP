"""Dashboard boundary and browser behavior checks; no desktop is operated."""

import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import dashboard


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dashboard.h.dependencies(False)
        dashboard.h.native_modules()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.watch = self.root / "watch-test"
        self.watch.mkdir()
        (self.watch / "captures").mkdir()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        created = dashboard.h.psutil.Process().create_time()
        self.config = {"schema": "desktop-watch-v1", "watch": str(self.watch),
                       "url": f"http://127.0.0.1:{self.listener.getsockname()[1]}/" + "x" * 43,
                       "source_identity": ["PyCharmHarness_test", os.getpid(), created, sys.executable],
                       "server_pid": os.getpid(), "server_created": created}
        self.write_config()
        self.state = {"schema": "rea-browser-v1", "session": str(self.watch),
                      "desktop": "PyCharmHarness_test", "pid": os.getpid(),
                      "status": "running", "heartbeat": time.time()}
        self.write_state()
        self.layout = {"schema": "desktop-layout-v1", "captured_at": time.time(), "bounds": [0, 0, 640, 480],
                       "complete": True, "windows": [], "omitted_count": 0}
        self.write_layout()
        self.source = dashboard.approve_watch(self.watch, "Desktop one")
        self.server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler([self.source], "dashboard-test-token"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.listener.close()
        self.temp.cleanup()

    def write_config(self):
        (self.watch / "watch.json").write_text(json.dumps(self.config), encoding="utf-8")

    def write_state(self):
        (self.watch / "browser-session.json").write_text(json.dumps(self.state), encoding="utf-8")

    def write_layout(self):
        (self.watch / "captures" / "desktop.json").write_text(json.dumps(self.layout), encoding="utf-8")

    def request(self, path, *, host=None, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(method, path, headers={"Host": host or f"127.0.0.1:{self.server.server_port}"})
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def test_authenticated_read_only_page_and_sanitized_metadata(self):
        code, page, headers = self.request("/dashboard-test-token")
        self.assertEqual(code, 200)
        self.assertIn(b"<h1>Desktops</h1>", page)
        self.assertNotIn(b"innerHTML", page)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        code, body, _ = self.request("/dashboard-test-token/meta")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["desktops"][0]["state"], "live")
        self.assertEqual(json.loads(body)["desktops"][0]["url"], self.config["url"])
        for private in (str(self.watch), sys.executable, "source_identity", "server_pid", "PyCharmHarness_test"):
            self.assertNotIn(private, body.decode())
        for path in ("/wrong/meta", "/dashboard-test-token/../meta", "/dashboard-test-token/meta?watch=other", "/dashboard-test-token/%2e%2e/meta"):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request("/dashboard-test-token/meta", host="attacker.example")[0], 403)
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertEqual(self.request("/dashboard-test-token/meta", method=method)[0], 405)

    def test_watch_url_and_process_identity_are_approved_explicitly(self):
        original = dict(self.config)
        for url in ("http://example.com/" + "x" * 43, "http://localhost:12345/" + "x" * 43,
                    "https://127.0.0.1:12345/" + "x" * 43, "http://user@127.0.0.1:12345/" + "x" * 43,
                    "http://127.0.0.1:12345/short", original["url"] + "?query=1", original["url"] + "/other"):
            with self.subTest(url=url):
                self.config = {**original, "url": url}
                self.write_config()
                with self.assertRaises(ValueError):
                    dashboard.approve_watch(self.watch, "Rejected")
        self.config = {**original, "server_created": original["server_created"] - 10}
        self.write_config()
        with self.assertRaises(ValueError):
            dashboard.approve_watch(self.watch, "Reused process")
        self.config = {**original, "watch": str(self.root / "different")}
        self.write_config()
        with self.assertRaises(ValueError):
            dashboard.approve_watch(self.watch, "Wrong directory")

    def test_stale_stopped_changed_and_malformed_sources_fail_without_private_errors(self):
        self.layout["captured_at"] = time.time() - 10
        self.write_layout()
        self.assertEqual(dashboard.desktop_status(self.source)["state"], "stale")
        self.state.update(status="stopped", error="secret local path and error")
        self.write_state()
        result = dashboard.desktop_status(self.source)
        self.assertEqual(result["state"], "unavailable")
        self.assertNotIn("url", result)
        self.assertNotIn("secret", json.dumps(result))
        self.config["url"] = "http://127.0.0.1:12345/" + "y" * 43
        self.write_config()
        self.assertEqual(dashboard.desktop_status(self.source)["state"], "unavailable")
        (self.watch / "watch.json").write_text("[]", encoding="utf-8")
        self.assertEqual(dashboard.desktop_status(self.source)["state"], "unavailable")
        (self.watch / "watch.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(dashboard.desktop_status(self.source)["state"], "unavailable")

    def test_browser_keeps_stream_nodes_and_only_expands_locally(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js required for the browser-script check.")
        script = dashboard.HTML.decode().split("<script>", 1)[1].split("</script>", 1)[0]
        harness = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
class Element{
 constructor(){this.children=[];this.events={};this.attributes={};this.dataset={};this.hidden=false;this.classes=new Set();this.classList={contains:n=>this.classes.has(n),add:n=>this.classes.add(n),remove:n=>this.classes.delete(n)};this.loads=0;}
 appendChild(child){this.children.push(child);}setAttribute(n,v){this.attributes[n]=v;}addEventListener(n,f){this.events[n]=f;}
 set src(value){this.address=value;this.loads++;}get src(){return this.address;}
}
const elements=new Map(),events={},document={getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement(){return new Element();},addEventListener(n,f){events[n]=f;}};
const context=vm.createContext({document,location:{pathname:'/dashboard-token'},Date,Map,fetch:()=>new Promise(()=>{}),setInterval:()=>{}});
vm.runInContext(require('node:fs').readFileSync(0,'utf8'),context);
function render(items){context.items=items;vm.runInContext('render(items)',context);}
const first={id:'one',name:'Desktop one',state:'live',url:'http://127.0.0.1:12345/token-one'},second={id:'two',name:'Desktop two',state:'live',url:'http://127.0.0.1:23456/token-two'};
render([first,second]);const grid=document.getElementById('grid');assert.equal(grid.children.length,2);
const a=grid.children[0],b=grid.children[1],image=a.children[1].children[0];image.onload();
assert.equal(image.src,first.url+'/stream.mjpg');render([first,second]);assert.equal(image.loads,1,'health polling must not reconnect healthy streams');
render([{...first,state:'stale'},second]);assert.equal(a.dataset.state,'stale');assert.equal(image.loads,1);assert.equal(grid.children[1],b);
a.children[0].children[2].events.click();assert.equal(a.classList.contains('expanded'),true);assert.equal(image.loads,1);
events.keydown({key:'Escape'});assert.equal(a.classList.contains('expanded'),false);
render([{...first,state:'unavailable',url:undefined},second]);assert.equal(image.src,first.url+'/stream.mjpg','last source image must remain');assert.equal(image.loads,1);
image.onerror();render([first,second]);assert.equal(image.loads,1,'errors must not cause a retry every poll');
render([{...first,name:'<img onerror=alert(1)>'},second]);assert.equal(a.children[0].children[0].textContent,'<img onerror=alert(1)>');
console.log('Stable desktop streams and local expansion passed.');
"""
        completed = subprocess.run([node, "-e", harness], input=script, text=True, capture_output=True,
                                   timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
