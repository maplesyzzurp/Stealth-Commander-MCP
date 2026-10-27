"""Model-agnostic stdio & remote HTTP MCP server ('Stealth Commander') for hidden Windows desktop delegation."""
import getpass
import base64
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlencode, urlsplit
import urllib.request

SCRIPTS_DIR = Path(__file__).resolve().parent
DEFAULT_DASHBOARD_PORT = 61076
DEFAULT_HUB_PORT = 61077
PAGES_ORIGIN = "https://stealthcommander.pages.dev"
SERVER_INFO = {"name": "Stealth Commander", "version": "1.1.3-private.4"}

TOOLS = [
    {
        "name": "capture_observation",
        "description": "Read an already-published PNG from an approved inactive session's observability cache. Requires the paired PC control scope. With no exact reference, pins the latest observed frame; with sha256 and capture_sequence, returns only that exact frame. It never requests a new target capture, starts an observer, or sends application input. Missing, stale, protected or mismatched frames fail.",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "desktop_id": {"type": "string", "pattern": "^[0-9a-f]{16}$", "description": "Approved desktop id from observability metadata."},
                "hwnd": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807, "description": "Observed window handle on that desktop."},
                "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$", "description": "Optional exact published PNG hash; also supply capture_sequence."},
                "capture_sequence": {"type": "integer", "minimum": 1, "description": "Optional exact published capture sequence; also supply sha256."},
            },
            "required": ["desktop_id", "hwnd"],
            "dependentRequired": {"sha256": ["capture_sequence"], "capture_sequence": ["sha256"]},
        },
    },
    {
        "name": "doctor",
        "description": "Check Stealth Commander hidden-desktop environment readiness and Python/IDE dependencies.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "dashboard_status",
        "description": "Read live hidden-desktop cards, window counts, FPS, CPU, and GPU metrics from the local observability dashboard.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Optional http://127.0.0.1:<port>/<token> dashboard URL."},
            },
        },
    },
    {
        "name": "list_hidden_desktops",
        "description": "List active hidden Windows desktops (PyCharmHarness_* and StealthREA_*) and their anchor processes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "work_dir": {"type": "string", "description": "Optional writable directory for probe output."},
            },
        },
    },
    {
        "name": "start_native_app",
        "description": "Launch a native Windows executable on a new isolated hidden desktop without touching the user's mouse or keyboard.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string", "description": "New session directory path."},
                "executable": {"type": "string", "description": "Absolute path to the executable."},
                "work_dir": {"type": "string", "description": "Working directory for the launched process."},
                "args": {"type": "array", "items": {"type": "string"}, "description": "Optional command-line arguments."},
            },
            "required": ["session", "executable", "work_dir"],
        },
    },
    {
        "name": "start_browser",
        "description": "Start an owned hidden Chrome browser session on a separate StealthREA_* desktop.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "work_dir": {"type": "string", "description": "Parent directory for the new browser session."},
                "browser": {"type": "string", "description": "Installed Chrome executable path."},
                "rea_cli": {"type": "string", "description": "Installed REA CLI script path."},
            },
            "required": ["work_dir", "browser", "rea_cli"],
        },
    },
    {
        "name": "start_pycharm",
        "description": "Start an isolated PyCharm project copy on a separate PyCharmHarness_* hidden desktop.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Source project directory path."},
                "runs": {"type": "array", "items": {"type": "string"}, "description": "Name=relative_script.py run configurations."},
                "work_dir": {"type": "string", "description": "Optional parent directory for the session."},
                "python_exe": {"type": "string", "description": "Optional project Python interpreter path."},
            },
            "required": ["project"],
        },
    },
    {
        "name": "session_action",
        "description": "Control an owned session. Browser supports status, scenario and stop; native supports status and stop; PyCharm supports status, windows, controls, invoke, value, click, ide, capture and stop. Supply fresh owned window/control identities for mutations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["pycharm", "browser", "native"], "description": "Session type."},
                "action": {
                    "type": "string",
                    "enum": ["status", "windows", "controls", "invoke", "value", "click", "ide", "capture", "scenario", "stop"],
                    "description": "Session action to run.",
                },
                "session": {"type": "string", "description": "Session directory path."},
                "hwnd": {"type": "integer", "description": "Target window handle for window/UI actions."},
                "index": {"type": "integer", "description": "Control index for invoke/value."},
                "label": {"type": "string", "description": "Expected control label for invoke/value."},
                "text": {"type": "string", "description": "Text value to enter."},
                "output": {"type": "string", "description": "PNG output path for capture."},
                "x": {"type": "integer", "description": "Window-local click x coordinate; PyCharm click requires x and y."},
                "y": {"type": "integer", "description": "Window-local click y coordinate."},
                "ide_action": {"type": "string", "enum": ["dump", "click", "toggle", "text", "trust-project", "dismiss-tour", "roots", "open", "select", "run", "input", "content", "layout"], "description": "Owned PyCharm IDE operation when action is ide."},
                "name": {"type": "string", "description": "IDE run configuration or operation name."},
                "path": {"type": "string", "description": "IDE project file path for a supported operation."},
                "selected": {"type": "boolean", "description": "Desired IDE toggle state."},
                "scenario_file": {"type": "string", "description": "JSON scenario file for the owned browser; no browser endpoint may be supplied in the scenario."},
                "force_owned_job": {"type": "boolean", "description": "Native stop only: terminate this owned job if graceful shutdown leaves windows open; unsaved work may be lost."},
                "extra_args": {"type": "array", "items": {"type": "string"}, "description": "Legacy CLI flags. Cannot replace session ownership or duplicate structured fields."},
            },
            "required": ["kind", "action", "session"],
        },
    },
    {
        "name": "start_observer",
        "description": "Start a 60 FPS Direct3D11/PrintWindow live observer stream for a hidden desktop or session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "work_dir": {"type": "string", "description": "Observer output directory."},
                "session": {"type": "string", "description": "Owned session directory to watch; omit desktop and pid when supplied."},
                "desktop": {"type": "string", "description": "Owned hidden desktop name; requires pid and scope desktop, and excludes session."},
                "pid": {"type": "integer", "minimum": 1, "description": "Anchor PID required with desktop; omit with session."},
                "scope": {"type": "string", "enum": ["session", "desktop"], "default": "desktop"},
                "interval": {"type": "number", "minimum": 0.015, "maximum": 30, "default": 0.0167},
                "port": {"type": "integer", "minimum": 0, "maximum": 65535, "default": 0},
            },
            "required": ["work_dir"],
        },
    },
    {
        "name": "observer_action",
        "description": "Read status or stop an owned observer using the exact watch directory returned by start_observer. Stopping the observer does not stop its source application.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "stop"]},
                "watch": {"type": "string", "description": "Exact owned observer watch directory."},
            },
            "required": ["action", "watch"],
        },
    },
    {
        "name": "run_command",
        "description": "Run a task-scoped background command under the Windows account, hiding its console when supported. This does not isolate GUI programs or sandbox filesystem/network access. Use start_native_app for owned hidden-desktop GUI launch. Maximum timeout 300 seconds.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "argv": {"type": "array", "minItems": 1, "items": {"type": "string"}, "description": "Executable and arguments list."},
                "cwd": {"type": "string", "description": "Optional working directory."},
                "timeout": {"type": "number", "minimum": 1, "maximum": 300, "default": 30, "description": "Timeout in seconds (max 300)."},
            },
            "required": ["argv"],
        },
    },
    {
        "name": "start_tunnel",
        "description": "Start or retrieve the token-protected Stealth Commander cloudflared HTTPS tunnel for phone control and remote /mcp access.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]
for _t in TOOLS:
    _t["annotations"] = {
        "readOnlyHint": _t["name"] in {"doctor", "capture_observation"},
        "destructiveHint": _t["name"] in {"start_native_app", "start_browser", "start_pycharm", "session_action", "observer_action", "run_command", "start_tunnel"},
        "idempotentHint": _t["name"] in {"doctor", "dashboard_status", "list_hidden_desktops", "capture_observation"},
        "openWorldHint": _t["name"] in {"start_native_app", "start_browser", "start_pycharm", "session_action", "observer_action", "run_command", "start_tunnel"},
    }


def _run_script(script_name, args, timeout=120):
    cmd = [sys.executable, "-B", str(SCRIPTS_DIR / script_name), *[str(a) for a in args]]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = (proc.stdout or "").strip()
    try:
        return json.loads(out) if out else {"ok": proc.returncode == 0, "stderr": (proc.stderr or "").strip()}
    except Exception:
        return {"ok": proc.returncode == 0, "stdout": out, "stderr": (proc.stderr or "").strip()}


def _dashboard_config_file():
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return base / "StealthDesktop" / "plugin.json"


def _saved_dashboard_url():
    url = os.environ.get("STEALTH_DESKTOP_DASHBOARD_URL")
    if url:
        return str(url).rstrip("/")
    cfg = _dashboard_config_file()
    if cfg.is_file():
        try:
            url = json.loads(cfg.read_text(encoding="utf-8")).get("dashboard_url")
            if url:
                return str(url).rstrip("/")
        except Exception:
            pass
    return None


def _ports_config_file():
    return _tunnel_config_file().with_name("ports.json")


def _load_ports():
    try:
        with _ports_config_file().open("r", encoding="utf-8") as stream:
            raw = stream.read(257)
        data = json.loads(raw) if len(raw) <= 256 else None
        if (isinstance(data, dict) and set(data) <= {"dashboard", "hub"}
                and all(type(port) is int and 1024 <= port <= 65535 for port in data.values())):
            return data
    except (OSError, ValueError, UnicodeError):
        pass
    return {}


def _configured_port(kind):
    return _load_ports().get(kind, DEFAULT_HUB_PORT if kind == "hub" else DEFAULT_DASHBOARD_PORT)


def _save_port(kind, port):
    if kind not in ("dashboard", "hub") or type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Invalid loopback port setting.")
    ports = _load_ports()
    if ports.get(kind) != port:
        ports[kind] = port
        _write_local_json(_ports_config_file(), ports)
    return port


def _choose_loopback_port(kind):
    candidate = _configured_port(kind)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", candidate))
        except OSError:
            probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return _save_port(kind, port)


def _validated_loopback_url(url):
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (parsed and parsed.scheme == "http" and type(parsed.port) is int
                and 1024 <= parsed.port <= 65535 and parsed.netloc == f"127.0.0.1:{parsed.port}"
                and not parsed.query and not parsed.fragment
                and re.fullmatch(r"/[A-Za-z0-9_-]{32,128}", parsed.path)):
            return parsed
    except ValueError:
        pass
    return None


def _port_open(port, timeout=0.3):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _fetch_dashboard_meta(url, timeout=4.0):
    if not url or not str(url).startswith("http://127.0.0.1:"):
        return None
    try:
        parsed = urlsplit(str(url).rstrip("/"))
        conn = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=timeout)
        try:
            conn.request("GET", parsed.path + "/meta", headers={"Host": f"127.0.0.1:{parsed.port}", "Connection": "close"})
            resp = conn.getresponse()
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()
    except Exception:
        return None


def _spawn_detached(cmd_args):
    args_list = [str(x) for x in cmd_args]
    if os.name == "nt" and not os.environ.get("STEALTH_DISABLE_WMI_SPAWN"):
        try:
            import base64
            cmdline = subprocess.list2cmdline(args_list)
            ps_cmdline = "'" + cmdline.replace("'", "''") + "'"
            ps_cmd = (
                "$ProgressPreference = 'SilentlyContinue'; "
                "$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}; "
                f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine={ps_cmdline}; ProcessStartupInformation=$si}}; "
                "if ($r.ReturnValue -eq 0) { Write-Output ('PID=' + $r.ProcessId) } else { exit 1 }"
            )
            enc = base64.b64encode(ps_cmd.encode("utf-16le")).decode("ascii")
            out = subprocess.check_output(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", enc],
                stdin=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                text=True,
            )
            m = re.search(r"PID=(\d+)", out)
            if m:
                return int(m.group(1))
        except Exception:
            pass
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    proc = subprocess.Popen(
        args_list,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )
    return proc.pid


def ensure_dashboard(work_dir=None):
    deadline = time.monotonic() + 8.0
    url = _saved_dashboard_url()
    parsed = _validated_loopback_url(url)
    meta = _fetch_dashboard_meta(url, timeout=0.5) if parsed else None
    try:
        if meta is not None:
            _save_port("dashboard", parsed.port)
            return {"ok": True, "url": url, "meta": meta, "started": False}
        port = _choose_loopback_port("dashboard")
    except (OSError, ValueError):
        return {"ok": False, "started": False, "error": "Cannot select and save a usable loopback dashboard port."}
    token = secrets.token_urlsafe(32)
    target_url = f"http://127.0.0.1:{port}/{token}"
    base_root = Path(work_dir) if work_dir else Path(tempfile.gettempdir()) / "stealth-commander"
    daemon_script = str(Path(__file__).resolve())
    _spawn_detached([sys.executable, "-B", daemon_script, "--serve-dashboard", str(port), token, str(base_root)])
    while time.monotonic() < deadline:
        time.sleep(min(0.15, max(0.0, deadline - time.monotonic())))
        saved_url = _saved_dashboard_url()
        saved = _validated_loopback_url(saved_url)
        live_url = saved_url if saved and saved.port == port else target_url
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        meta = _fetch_dashboard_meta(live_url, timeout=min(0.3, remaining))
        if meta is not None:
            return {"ok": True, "url": live_url, "meta": meta, "started": True}
    return {"ok": False, "url": target_url, "started": False, "error": "Dashboard startup did not become ready on the selected loopback port."}


def _serve_dashboard(port, token, base_root_str):
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import dashboard as d
    from desktop_registry import DesktopRegistry
    from gpu_metrics import GpuSampler
    from metrics import DesktopMetricsSampler
    from http.server import ThreadingHTTPServer

    d.h.dependencies(False)
    d.h.native_modules()
    base_root = Path(base_root_str).resolve()
    discover_root = base_root / "discovered"
    discover_root.mkdir(parents=True, exist_ok=True)
    sources = []
    for pattern in ("*/watch.json", "*/*/watch.json", "*/*/*/watch.json"):
        for watch_json in base_root.glob(pattern):
            if "discovered" in watch_json.parts:
                continue
            try:
                sources.append(d.approve_watch(watch_json.parent, f"Desktop · {watch_json.parent.parent.name}"))
            except Exception:
                pass
            if len(sources) >= 16:
                break
    registry = DesktopRegistry(sources, discover_root, scan_roots=[base_root])
    registry.start()
    sampler, gpu = DesktopMetricsSampler(), GpuSampler()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", int(port)), d.make_handler(sources, token, sampler, gpu, registry))
    except OSError as exc:
        registry.close()
        gpu.close()
        raise RuntimeError(f"Dashboard could not bind loopback port {port}.") from exc
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_port}/{token}"
    cfg = _dashboard_config_file()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dashboard_url": url}, indent=2) + "\n", encoding="utf-8")
    _save_port("dashboard", server.server_port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        registry.close()
        gpu.close()


def _extra_value(extra, flag):
    for index, token in enumerate(extra):
        if token.startswith(flag + "="):
            return token.split("=", 1)[1]
        if token == flag and index + 1 < len(extra):
            return extra[index + 1]
    return None


def _legacy_argument_error(kind, action, extra):
    """Accept canonical action flags only; argparse abbreviations must never change ownership."""
    pycharm = {
        "status": set(), "windows": set(), "stop": set(),
        "controls": {"--hwnd"},
        "invoke": {"--hwnd", "--index", "--label"},
        "value": {"--hwnd", "--index", "--label", "--text"},
        "click": {"--hwnd", "--x", "--y"},
        "capture": {"--hwnd", "--output"},
        "ide": {"--action", "--name", "--path", "--selected", "--index", "--label", "--text"},
    }
    allowed = (pycharm[action] if kind == "pycharm" else
               {"--file"} if kind == "browser" and action == "scenario" else
               {"--force-owned-job"} if kind == "native" and action == "stop" else set())
    seen = set()
    index = 0
    while index < len(extra):
        token = extra[index]
        flag, equals, _ = token.partition("=")
        if flag not in allowed:
            return f"unsupported legacy flag or positional argument: {flag}. Use canonical action flags or structured fields."
        if flag in seen:
            return f"Duplicate legacy flag: {flag}"
        seen.add(flag)
        if flag == "--force-owned-job":
            if equals:
                return "--force-owned-job does not take a value."
        elif not equals:
            index += 1
            if index >= len(extra) or extra[index].startswith("--"):
                return f"Legacy flag {flag} requires a value."
        index += 1
    return None


def _validate_tool_arguments(name, args):
    """Validate before dashboard/process side effects; keep schemas flat for MCP hosts."""
    spec = next((tool for tool in TOOLS if tool["name"] == name), None)
    if spec is None:
        return f"Unknown tool: {name}"
    if not isinstance(args, dict):
        return "Tool arguments must be an object."
    schema = spec["inputSchema"]
    if name == "start_browser" and (not args.get("browser") or not args.get("rea_cli")):
        return "Browser startup requires explicit browser and rea_cli paths. The browser adapter uses its own separate Chrome profile."
    for key in schema.get("required", []):
        if key not in args or args[key] is None or (isinstance(args[key], (str, list)) and not args[key]):
            return f"Missing required argument: {key}"
    for key, value in args.items():
        rule = schema.get("properties", {}).get(key)
        if rule is None:
            continue
        kind = rule.get("type")
        valid = {"string": isinstance(value, str), "integer": type(value) is int,
                 "number": type(value) in (int, float) and math.isfinite(value),
                 "boolean": type(value) is bool, "array": isinstance(value, list)}.get(kind, True)
        if not valid:
            return f"Argument {key} must be a valid {kind}."
        if kind == "array" and (len(value) < rule.get("minItems", 0) or not all(isinstance(item, str) for item in value)):
            return f"Argument {key} must be a non-empty list of strings." if rule.get("minItems") else f"Argument {key} must be a list of strings."
        if "enum" in rule and value not in rule["enum"]:
            return f"Unsupported {key}: {value}"
        if kind in ("integer", "number") and (value < rule.get("minimum", -math.inf) or value > rule.get("maximum", math.inf)):
            return f"Argument {key} is outside the supported range."
    if name == "run_command" and not args["argv"][0].strip():
        return "argv must begin with an executable."
    if name == "start_observer":
        session, desktop = bool(args.get("session")), bool(args.get("desktop"))
        if session == desktop or (session and args.get("pid") is not None) or (desktop and not args.get("pid")):
            return "Supply either session, or desktop with its positive anchor pid; do not mix targets."
        if desktop and args.get("scope", "desktop") != "desktop":
            return "A desktop anchor requires scope desktop."
    if name == "session_action":
        allowed = {"pycharm": {"status", "windows", "controls", "invoke", "value", "click", "ide", "capture", "stop"},
                   "browser": {"status", "scenario", "stop"}, "native": {"status", "stop"}}
        kind, action = args["kind"], args["action"]
        if action not in allowed[kind]:
            return f"The requested {kind} action {action} is unsupported; supported actions: {', '.join(sorted(allowed[kind]))}."
        extra = args.get("extra_args") or []
        if any(token == "--session" or token.startswith("--session=") for token in extra):
            return "extra_args cannot replace the owned session."
        legacy_error = _legacy_argument_error(kind, action, extra)
        if legacy_error:
            return legacy_error
        flags = {"hwnd": "--hwnd", "index": "--index", "label": "--label", "text": "--text", "output": "--output",
                 "x": "--x", "y": "--y", "ide_action": "--action", "name": "--name", "path": "--path", "selected": "--selected",
                 "scenario_file": "--file", "force_owned_job": "--force-owned-job"}
        for key, flag in flags.items():
            if key in args and any(token == flag or token.startswith(flag + "=") for token in extra):
                return f"Use either structured {key} or its legacy CLI flag, not both."
        required = {"controls": ("hwnd",), "invoke": ("hwnd", "index", "label"),
                    "value": ("hwnd", "index", "label", "text"), "click": ("hwnd", "x", "y"),
                    "capture": ("hwnd", "output"), "ide": ("ide_action",), "scenario": ("scenario_file",)}.get(action, ())
        for key in required:
            if args.get(key) is None and _extra_value(extra, flags[key]) is None:
                return f"{action} requires {key}. Inspect the owned session before acting."
        if args.get("force_owned_job") and (kind != "native" or action != "stop"):
            return "force_owned_job is only supported for native stop."
    return None


def _browser_launch_arguments(work_dir, request):
    """Resolve configuration only; the browser adapter validates and creates its own profile."""
    browser = request.get("browser") or os.environ.get("STEALTH_COMMANDER_BROWSER")
    if not browser:
        candidates = [Path(os.environ.get(root, "")) / suffix for root, suffix in (
            ("ProgramFiles", "Google/Chrome/Application/chrome.exe"),
            ("ProgramFiles(x86)", "Google/Chrome/Application/chrome.exe"),
            ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe")) if os.environ.get(root)]
        browser = next((str(path) for path in candidates if path.is_file()), "")
    return {"work_dir": str(work_dir), "browser": browser or "",
            "rea_cli": request.get("rea_cli") or os.environ.get("STEALTH_COMMANDER_REA_CLI", "")}


def handle_tool_call(name, arguments, *, _runner=_run_script):
    args = {} if arguments is None else arguments
    error = _validate_tool_arguments(name, args)
    if error:
        return {"ok": False, "error": error}
    if name == "capture_observation":
        return _read_observability_frame(args)
    if name == "start_browser" and (not args.get("browser") or not args.get("rea_cli")):
        return {"ok": False, "error": "Browser startup requires explicit browser and rea_cli paths. The browser adapter uses its own separate Chrome profile."}
    auto_dash = None
    if _runner is _run_script and name in ("start_native_app", "start_browser", "start_pycharm", "start_observer", "list_hidden_desktops"):
        auto_dash = ensure_dashboard(args.get("work_dir"))
    if name == "doctor":
        return _runner("harness.py", ["doctor"])
    if name == "dashboard_status":
        url = args.get("url")
        if url:
            meta = _fetch_dashboard_meta(url, timeout=5.0)
            dash = {"ok": meta is not None, "url": str(url).rstrip("/"), "meta": meta, "started": False}
        else:
            dash = ensure_dashboard(args.get("work_dir"))
        if not dash.get("ok") or not dash.get("meta"):
            return {"ok": False, "error": "Dashboard unavailable or could not be auto-started."}
        meta = json.loads(json.dumps(dash["meta"]))
        for card in meta.get("desktops") or []:
            card.pop("url", None)
        return {"ok": True, "dashboard_url": dash["url"], "started": dash.get("started", False), "result": meta}
    if name == "list_hidden_desktops":
        import desktop_registry
        work = Path(args.get("work_dir") or Path(tempfile.gettempdir()) / "stealth-commander-probe")
        reg = desktop_registry.DesktopRegistry([], work, interval=60)
        res = {"ok": True, "result": reg._enumerate()}
        if auto_dash and auto_dash.get("url"):
            res["dashboard_url"] = auto_dash["url"]
        return res
    if name == "start_native_app":
        cli = ["start", "--session", args["session"], "--executable", args["executable"], "--work-dir", args["work_dir"]]
        if args.get("args"):
            cli.extend(["--", *[str(x) for x in args["args"]]])
        res = _runner("native_desktop.py", cli)
        if auto_dash and auto_dash.get("url") and isinstance(res, dict):
            res["dashboard_url"] = auto_dash["url"]
        return res
    if name == "start_browser":
        cli = ["start", "--work-dir", args["work_dir"]]
        if args.get("browser"):
            cli.extend(["--browser", args["browser"]])
        if args.get("rea_cli"):
            cli.extend(["--rea-cli", args["rea_cli"]])
        res = _runner("rea_bridge.py", cli)
        if auto_dash and auto_dash.get("url") and isinstance(res, dict):
            res["dashboard_url"] = auto_dash["url"]
        return res
    if name == "start_pycharm":
        cli = ["start", "--project", args["project"]]
        for run_spec in args.get("runs") or []:
            cli.extend(["--run", run_spec])
        if args.get("work_dir"):
            cli.extend(["--work-dir", args["work_dir"]])
        if args.get("python_exe"):
            cli.extend(["--python", args["python_exe"]])
        res = _runner("harness.py", cli, timeout=180)
        if auto_dash and auto_dash.get("url") and isinstance(res, dict):
            res["dashboard_url"] = auto_dash["url"]
        return res
    if name == "session_action":
        script_map = {"pycharm": "harness.py", "browser": "rea_bridge.py", "native": "native_desktop.py"}
        script = script_map.get(args.get("kind"))
        if not script:
            return {"ok": False, "error": f"Unsupported session kind: {args.get('kind')}"}
        cli = [args["action"], "--session", args["session"]]
        for flag, key in (("--hwnd", "hwnd"), ("--index", "index"), ("--label", "label"), ("--text", "text"), ("--output", "output"),
                          ("--x", "x"), ("--y", "y"), ("--action", "ide_action"), ("--name", "name"), ("--path", "path"), ("--file", "scenario_file")):
            if args.get(key) is not None:
                cli.extend([flag, str(args[key])])
        if args.get("selected") is not None:
            cli.extend(["--selected", str(args["selected"]).lower()])
        if args.get("force_owned_job"):
            cli.append("--force-owned-job")
        if args.get("extra_args"):
            cli.extend(args["extra_args"])
        if args["kind"] == "browser" and args["action"] == "scenario":
            try:
                return _runner(script, cli, timeout=210)
            except subprocess.TimeoutExpired:
                return {"ok": False, "timed_out": True, "outcome": "unknown",
                        "error": "Browser scenario timed out; its outcome is unknown. Inspect the owned session state and worker.log before retrying a mutation."}
        return _runner(script, cli)
    if name == "observer_action":
        return _runner("desktop_watch.py", [args["action"], "--watch", args["watch"]])
    if name == "start_observer":
        cli = [
            "start",
            "--scope", args.get("scope", "desktop"),
            "--interval", str(args.get("interval", 0.0167)),
            "--port", str(args.get("port", 0)),
            "--work-dir", args["work_dir"],
        ]
        if args.get("session"):
            cli.extend(["--session", args["session"]])
        else:
            cli.extend(["--desktop", args["desktop"], "--pid", str(args["pid"])])
        res = _runner("desktop_watch.py", cli)
        if auto_dash and auto_dash.get("url") and isinstance(res, dict):
            res["dashboard_url"] = auto_dash["url"]
        return res
    if name == "run_command":
        argv = [str(x) for x in (args.get("argv") or [])]
        if not argv:
            return {"ok": False, "error": "argv must be a non-empty list."}
        timeout = max(1.0, min(float(args.get("timeout", 30)), 300.0))
        proc = subprocess.run(
            argv,
            cwd=args.get("cwd") or None,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return {"ok": proc.returncode == 0, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    if name == "start_tunnel":
        res = ensure_remote_hub()
        _record_usage_event(name, True, 0, res.get("portal_url", ""))
        return res
    return {"ok": False, "error": f"Unknown tool: {name}"}


PORTAL_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Stealth Commander · Control Center</title>
<style>
:root{color-scheme:dark;font-family:Inter,system-ui,-apple-system,sans-serif;background:#080a0f;color:#e6edf3}
*{box-sizing:border-box}body{margin:0;padding:12px 14px;max-width:1360px;margin-inline:auto}
header{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between;padding:11px 14px;background:#10141d;border:1px solid #222a3d;border-radius:11px;margin-bottom:10px}
.brand{display:flex;align-items:center;gap:9px;font-weight:700;font-size:15px}
.dot{width:9px;height:9px;border-radius:99px;background:#2ea043;box-shadow:0 0 8px #2ea043}
.dot.warn{background:#d29922;box-shadow:0 0 8px #d29922}
.nav{display:flex;gap:6px;flex-wrap:wrap}
.tab{background:#161c28;color:#9fb0c7;border:1px solid #263044;padding:6px 12px;border-radius:7px;font-size:12px;font-weight:600;cursor:pointer}
.tab.active{background:#1f6feb;color:#fff;border-color:#388bfd}
.pills{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:12px}
.pill{padding:4px 8px;border-radius:6px;background:#171d2b;border:1px solid #283248;color:#9fb0c7;font-size:11px;font-weight:600}
.pill.ok{background:#0f291a;border-color:#238636;color:#3fb950}
.pill.warn{background:#2d210f;border-color:#9e6a03;color:#d29922}
.view{display:none}.view.active{display:block}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;margin-bottom:10px}
.kpi{background:#10141d;border:1px solid #222a3d;border-radius:10px;padding:10px 12px}
.kpi .lbl{font-size:11px;color:#8b9bb4;text-transform:uppercase;letter-spacing:.04em}
.kpi .val{font-size:20px;font-weight:700;margin-top:3px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:10px;margin-bottom:10px}
.panel{background:#10141d;border:1px solid #222a3d;border-radius:10px;padding:12px;display:flex;flex-direction:column;gap:8px}
.panel h3{margin:0;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:#8b9bb4;display:flex;justify-content:space-between;align-items:center}
.row{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
input,select{flex:1;min-width:130px;background:#080a0f;border:1px solid #283248;color:#e6edf3;padding:7px 9px;border-radius:6px;font-size:12px}
button,a.btn{background:#1f6feb;color:#fff;border:0;padding:6px 11px;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:5px}
button.sec,a.btn.sec{background:#1c2333;border:1px solid #2e3850;color:#c9d1d9}
button.danger{background:#3d161b;border:1px solid #8b2635;color:#ff7b72}
button.ok{background:#196c2e;color:#fff}
pre,code.blk{margin:0;padding:8px 10px;background:#06080c;border:1px solid #1d2433;border-radius:6px;font-size:11px;overflow:auto;white-space:pre-wrap;word-break:break-all;color:#a5d6ff;font-family:ui-monospace,Consolas,monospace}
.card{background:#10141d;border:1px solid #222a3d;border-radius:10px;overflow:hidden}
.card-hd{display:flex;justify-content:space-between;align-items:center;padding:8px 10px;font-size:13px;font-weight:600;border-bottom:1px solid #1d2433}
.card img{width:100%;display:block;background:#05070a;aspect-ratio:16/10;object-fit:contain}
.meta{padding:7px 10px;font-size:11px;color:#8b9bb4;display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid #1c2333}
th{color:#8b9bb4;font-size:11px;text-transform:uppercase}
#toast{position:fixed;bottom:14px;right:14px;background:#1f6feb;color:#fff;padding:8px 13px;border-radius:8px;font-size:12px;font-weight:600;display:none;z-index:99}
</style></head><body>
<header>
  <div class="brand"><span class="dot" id="hdot"></span><span id="host">Stealth Commander</span></div>
  <nav class="nav">
    <button class="tab active" onclick="showTab('devices',this)">Devices</button>
    <button class="tab" onclick="showTab('connect',this)">Connect AI / MCP</button>
    <button class="tab" onclick="showTab('live',this)">Live Desktops (<span id="dcount">0</span>)</button>
    <button class="tab" onclick="showTab('usage',this)">Usage & Logs</button>
    <button class="tab" onclick="showTab('settings',this)">Settings</button>
    <button class="tab" id="acctTabBtn" onclick="showTab('account',this)">Account (<span id="acctBadge">Sign In</span>)</button>
  </nav>
  <div class="pills">
    <select id="acctDeviceSelect" style="display:none;max-width:210px;padding:4px 8px;font-size:11px" onchange="switchAccountDevice(this.value)"></select>
    <button class="sec" id="acctBtn" onclick="openAuthModal()">Sign In / Account</button>
    <span class="pill ok" id="tstatus">ONLINE</span>
    <a class="btn sec" id="openDash" href="#" target="_blank">Open 60 FPS Dash</a>
    <button class="danger" id="killTunBtn" style="display:none" onclick="act('stop_tunnel')">Kill Public Tunnel (Lock Local)</button>
    <button class="sec" onclick="copyText(document.getElementById('mcpUrlCode').textContent,'Copied /mcp URL!')">Copy /mcp URL</button>
  </div>
</header>

<section id="view-devices" class="view active">
  <div class="kpis">
    <div class="kpi"><div class="lbl">Host Device</div><div class="val" id="k-host">—</div></div>
    <div class="kpi"><div class="lbl">Security Mode</div><div class="val" id="k-tun">Local Only (Safe)</div></div>
    <div class="kpi"><div class="lbl">Active Hidden Desktops</div><div class="val" id="k-desk">0</div></div>
    <div class="kpi"><div class="lbl">Connected Local AI Clients</div><div class="val" id="k-cli">0 / 7</div></div>
  </div>
  <div class="panel" style="margin-bottom:10px">
    <h3><span>Managed Devices & Hidden Desktop Nodes</span><div class="row"><button class="danger" onclick="act('stop_tunnel')">Lock Local-Only (Stop Tunnel)</button><button class="sec" onclick="act('reconnect_tunnel')">Enable Temp Tunnel</button><button onclick="act('launch_chrome')">+ New Chrome Desktop</button><button class="sec" onclick="act('launch_notepad')">+ New Native Desktop</button></div></h3>
    <table>
      <thead><tr><th>Device / Node</th><th>Type</th><th>Status</th><th>Telemetry / Endpoint</th><th>Actions</th></tr></thead>
      <tbody id="devTable"></tbody>
    </table>
  </div>
  <div class="panel">
    <h3><span>Install on a Windows PC</span><button class="sec" onclick="copyText('npx -y --registry=https://stealthcommander.pages.dev stealth-commander@1.1.3-private.4 setup','Copied NPX setup command!')">Copy NPX Command</button></h3>
    <code class="blk">npx -y --registry=https://stealthcommander.pages.dev stealth-commander@1.1.3-private.4 setup</code>
  </div>
</section>

<section id="view-connect" class="view">
  <div class="panel" style="margin-bottom:10px">
    <h3><span>Local AI Clients on This PC</span><button class="ok" onclick="act('sync_clients')">Auto-Connect / Repair All Local Clients</button></h3>
    <table>
      <thead><tr><th>AI Client / IDE</th><th>Status</th><th>Config Path</th><th>Action</th></tr></thead>
      <tbody id="cliTable"></tbody>
    </table>
  </div>
  <div class="grid">
    <div class="panel">
      <h3><span>1-Click IDE Auto-Install</span></h3>
      <p style="margin:0;font-size:12px;color:#8b9bb4">Click to auto-register Stealth Commander in Cursor or VS Code, or copy the Remote MCP URL for ChatGPT, Claude Web, and mobile agents.</p>
      <div class="row">
        <a class="btn" id="cursorLink" href="#">Open in Cursor</a>
        <a class="btn sec" id="vscodeLink" href="#">Open in VS Code</a>
        <button class="sec" onclick="act('sync_clients')">Auto-Connect Local GPT / Claude / Droid</button>
      </div>
      <h3 style="margin-top:6px"><span>Remote HTTPS MCP Endpoint (ChatGPT / Claude / Mobile)</span><button class="sec" onclick="copyText(document.getElementById('mcpUrlCode').textContent,'Copied Remote MCP URL!')">Copy URL</button></h3>
      <code class="blk" id="mcpUrlCode"></code>
    </div>
    <div class="panel">
      <h3><span>Universal MCP JSON (Claude Desktop / Cursor / Windsurf / Droid)</span><button class="sec" onclick="copyText(document.getElementById('jsonSnippet').textContent,'Copied JSON config!')">Copy JSON</button></h3>
      <pre id="jsonSnippet">{
  "mcpServers": {
    "stealth-commander": {
      "command": "npx",
      "args": ["-y", "--registry=https://stealthcommander.pages.dev", "stealth-commander@1.1.3-private.4", "serve"]
    }
  }
}</pre>
      <h3><span>ChatGPT / Codex CLI & Claude Code CLI</span><button class="sec" onclick="copyText('claude mcp add stealth-commander -- npx -y --registry=https://stealthcommander.pages.dev stealth-commander@1.1.3-private.4 serve','Copied Claude CLI command!')">Copy CLI</button></h3>
      <code class="blk">claude mcp add stealth-commander -- npx -y --registry=https://stealthcommander.pages.dev stealth-commander@1.1.3-private.4 serve</code>
    </div>
  </div>
</section>

<section id="view-live" class="view">
  <div class="grid" style="margin-bottom:10px">
    <div class="panel">
      <h3>Quick Launch Hidden Desktop</h3>
      <div class="row">
        <button onclick="act('launch_chrome')">+ Chrome</button>
        <button class="sec" onclick="act('launch_notepad')">+ Notepad</button>
        <input id="appPath" placeholder="Executable or PyCharm project path">
        <input id="browserPath" placeholder="Chrome executable path (or configured default)">
        <input id="reaCliPath" placeholder="Installed REA CLI path (or configured default)">
        <button class="sec" onclick="act('launch_app')">Launch App</button>
        <button class="sec" onclick="act('launch_pycharm')">PyCharm</button>
      </div>
    </div>
    <div class="panel">
      <h3>Background Command</h3>
      <p>Runs with this Windows account. Console hiding does not isolate GUI programs; use an owned desktop launch for GUI work.</p>
      <div class="row">
        <input id="cmdIn" placeholder="e.g. Get-Process | Select-Object -First 5">
        <button onclick="act('run_cmd')">Run</button>
      </div>
    </div>
  </div>
  <pre id="out" style="display:none;margin-bottom:10px;max-height:150px"></pre>
  <div class="grid" id="grid"></div>
</section>

<section id="view-usage" class="view">
  <div class="kpis">
    <div class="kpi"><div class="lbl">Total MCP / Hub Calls</div><div class="val" id="u-calls">0</div></div>
    <div class="kpi"><div class="lbl">Exposed MCP Tools</div><div class="val" id="u-tools">10</div></div>
    <div class="kpi"><div class="lbl">Capture Engine</div><div class="val">D3D11 · 60 FPS</div></div>
    <div class="kpi"><div class="lbl">JPEG Subsampling</div><div class="val">4:4:4 Crisp</div></div>
  </div>
  <div class="panel" style="margin-bottom:10px">
    <h3><span>Recent Activity & Tool Call Audit Log</span><button class="sec" onclick="act('clear_usage')">Clear Log</button></h3>
    <table>
      <thead><tr><th>Time</th><th>Tool / Action</th><th>Status</th><th>Latency</th><th>Details</th></tr></thead>
      <tbody id="usageTable"></tbody>
    </table>
  </div>
</section>

<section id="view-settings" class="view">
  <div class="grid">
    <div class="panel">
      <h3>Stream & Hub Settings</h3>
      <div class="row"><label style="width:180px;font-size:12px">Target Frame Rate</label><select id="setFps"><option value="60">60 FPS (Direct3D 11 GPU)</option><option value="30">30 FPS (Balanced)</option></select></div>
      <div class="row"><label style="width:180px;font-size:12px">Auto-Start Dashboard</label><select id="setAutoDash"><option value="true">Enabled (On any MCP call)</option><option value="false">Manual only</option></select></div>
      <div class="row" style="margin-top:6px"><button onclick="saveSettings()">Save Settings</button><button class="sec" onclick="act('restart_dashboard')">Restart 60 FPS Dashboard</button><button class="sec" onclick="act('reconnect_tunnel')">Reconnect Cloudflare Tunnel</button></div>
    </div>
    <div class="panel">
      <h3>System Diagnostics & Endpoints</h3>
      <div class="row"><button class="sec" onclick="act('doctor')">Run Environment Doctor</button><button class="sec" onclick="act('sync_clients')">Sync All AI Clients</button></div>
      <pre id="diagOut">Click "Run Environment Doctor" to inspect Python, Chrome, PyCharm, and Direct3D 11 readiness.</pre>
    </div>
  </div>
</section>

<section id="view-account" class="view">
  <div id="acctSignedOutPanel" class="grid">
    <div class="panel">
      <h3><span>Sign In or Create Your Stealth Commander Account</span></h3>
      <p style="margin:0;font-size:12px;color:#9fb0c7">Sign in to link your Windows PCs and VDI boxes to your personal account. Your devices auto-discover over HTTPS without manual tunnel URLs and remain strictly private to your login.</p>
      <div class="row" style="margin-top:4px">
        <button id="modeSignInBtn" onclick="setAuthMode('login')">Sign In</button>
        <button class="sec" id="modeRegBtn" onclick="setAuthMode('register')">Create Account</button>
        <button class="sec" id="modeKeyBtn" onclick="setAuthMode('key')">API Key Login</button>
      </div>
      <div id="authEmailFields" style="display:flex;flex-direction:column;gap:8px;margin-top:4px">
        <input id="acctEmailIn" type="email" placeholder="Email address (e.g. you@example.com)">
        <input id="acctPassIn" type="password" placeholder="Password (min 6 characters)">
        <input id="acctNameIn" type="text" placeholder="Display Name (optional)" style="display:none">
      </div>
      <div id="authKeyFields" style="display:none;flex-direction:column;gap:8px;margin-top:4px">
        <input id="acctKeyIn" type="password" placeholder="Paste your sc_live_... Personal Account API Key">
      </div>
      <div class="row" style="margin-top:6px">
        <button onclick="submitAuth('inline')" id="acctSubmitBtn">Sign In to Account</button>
        <span id="acctAuthMsg" style="font-size:12px;color:#ff7b72"></span>
      </div>
    </div>
    <div class="panel">
      <h3><span>CLI Account Login & Setup</span><button class="sec" onclick="copyText('npx -y --registry=https://stealthcommander.pages.dev stealth-commander@1.1.3-private.4 login','Copied CLI login command!')">Copy CLI Login</button></h3>
      <p style="margin:0;font-size:12px;color:#8b9bb4">Run this command in PowerShell and enter the account key at the hidden prompt:</p>
      <code class="blk">npx -y --registry=https://stealthcommander.pages.dev stealth-commander@1.1.3-private.4 login</code>
      <h3><span>Check Signed-In Account in CLI</span></h3>
      <code class="blk">npx -y --registry=https://stealthcommander.pages.dev stealth-commander@1.1.3-private.4 whoami</code>
    </div>
  </div>

  <div id="acctSignedInPanel" style="display:none">
    <div class="kpis">
      <div class="kpi"><div class="lbl">Signed-In Account</div><div class="val" id="acctEmailVal" style="font-size:15px">—</div></div>
      <div class="kpi"><div class="lbl">Display Name</div><div class="val" id="acctNameVal" style="font-size:15px">—</div></div>
      <div class="kpi"><div class="lbl">Linked Windows PCs</div><div class="val" id="acctDevCount">0</div></div>
      <div class="kpi"><div class="lbl">Session Status</div><div class="val" style="font-size:15px;color:#3fb950">Authenticated</div></div>
    </div>
    <div class="grid">
      <div class="panel">
        <h3><span>Account Security & Personal API Key</span><div class="row"><button class="sec" onclick="copyText(document.getElementById('acctKeyCode').textContent,'Copied Account API Key!')">Copy API Key</button><button class="sec" onclick="rotateApiKey()">Rotate Key</button><button class="danger" onclick="logoutAccount()">Sign Out</button></div></h3>
        <code class="blk" id="acctKeyCode">—</code>
        <h3 style="margin-top:6px"><span>Link this PC and enable remote access</span><button class="sec" onclick="copyText(document.getElementById('acctSetupCmd').textContent,'Copied Account Setup Command!')">Copy Setup Command</button></h3>
        <p>Running this command links the PC to your account and enables a token-protected HTTPS tunnel. Remote tools can run commands and control owned desktop sessions.</p>
        <code class="blk" id="acctSetupCmd">npx -y --registry=https://stealthcommander.pages.dev stealth-commander@1.1.3-private.4 setup --enable-remote</code>
      </div>
      <div class="panel">
        <h3><span>Account-Linked Windows PCs & VDI Nodes</span><button class="sec" onclick="loadAccountMe(true)">Refresh Devices</button></h3>
        <table>
          <thead><tr><th>PC / Hostname</th><th>User</th><th>Status</th><th>Action</th></tr></thead>
          <tbody id="acctDevsTable"></tbody>
        </table>
      </div>
    </div>
  </div>
</section>

<div id="authModal" style="display:none;position:fixed;inset:0;background:rgba(4,6,10,0.78);z-index:90;align-items:center;justify-content:center;padding:14px">
  <div class="panel" style="width:100%;max-width:440px;border-color:#388bfd;box-shadow:0 16px 48px rgba(0,0,0,0.6)">
    <h3><span>Stealth Commander Account</span><button class="sec" onclick="closeAuthModal()">Close</button></h3>
    <div class="row">
      <button id="mModeSignInBtn" onclick="setAuthMode('login')">Sign In</button>
      <button class="sec" id="mModeRegBtn" onclick="setAuthMode('register')">Create Account</button>
      <button class="sec" id="mModeKeyBtn" onclick="setAuthMode('key')">API Key</button>
    </div>
    <div id="mAuthEmailFields" style="display:flex;flex-direction:column;gap:8px;margin-top:4px">
      <input id="mAcctEmailIn" type="email" placeholder="Email address (e.g. user@example.com)">
      <input id="mAcctPassIn" type="password" placeholder="Password (min 6 characters)">
      <input id="mAcctNameIn" type="text" placeholder="Display Name (optional)" style="display:none">
    </div>
    <div id="mAuthKeyFields" style="display:none;flex-direction:column;gap:8px;margin-top:4px">
      <input id="mAcctKeyIn" type="password" placeholder="Paste your sc_live_... Personal Account API Key">
    </div>
    <div class="row" style="margin-top:6px;justify-content:space-between">
      <button onclick="submitAuth('modal')" id="mAcctSubmitBtn">Sign In</button>
      <span id="mAcctAuthMsg" style="font-size:12px;color:#ff7b72"></span>
    </div>
  </div>
</div>

<div id="setupBanner" class="panel" style="display:none;margin-bottom:10px;border-color:#388bfd;background:#0d1829">
  <h3><span>Set Up Stealth Commander on Your PC (or Sign In to Your Account)</span><div class="row"><button onclick="openAuthModal()">Sign In / Create Account</button><button class="sec" onclick="copyText(document.getElementById('bannerSetupCmd').textContent,'Copied NPX setup command!')">Copy Setup Command</button></div></h3>
  <p style="margin:0;font-size:12px;color:#9fb0c7">Sign in to your Stealth Commander account to auto-connect your linked Windows PCs, or run the command below in PowerShell on your Windows PC:</p>
  <code class="blk" id="bannerSetupCmd">npx -y --registry=https://stealthcommander.pages.dev stealth-commander@1.1.3-private.4 setup</code>
  <div class="row">
    <input id="remotePairIn" placeholder="Or paste your personal #remote tunnel URL (https://....trycloudflare.com/<token>)">
    <button class="sec" onclick="pairRemote()">Connect Personal Tunnel</button>
  </div>
</div>
<div id="toast"></div>
<script>
let apiBase='',authMode='login',currentAccount=null;
const AUTH_ORIGIN='https://stealthcommander.pages.dev';
const rawPath=location.pathname.replace(/\\/+$/,'');
const out=document.getElementById('out'),grid=document.getElementById('grid'),cards=new Map();
function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.style.display='block';setTimeout(()=>t.style.display='none',2200);}
function copyText(txt,msg){navigator.clipboard.writeText(txt);toast(msg||'Copied!');}
function showTab(name,btn){
  document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
  document.getElementById('view-'+name).classList.add('active');
  if(btn)btn.classList.add('active');
}
function authBaseUrl(){
  if(location.hostname.endsWith('stealthcommander.pages.dev'))return '';
  return AUTH_ORIGIN;
}
function getAuthToken(){
  try{return localStorage.getItem('sc_auth_token')||'';}catch(_){return '';}
}
function setAuthToken(tok){
  try{if(tok)localStorage.setItem('sc_auth_token',tok);else localStorage.removeItem('sc_auth_token');}catch(_){}
}
function escapeHtml(value){
  return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function encodedArg(value){
  return encodeURIComponent(String(value??'')).replace(/'/g,'%27');
}
function openAuthModal(){
  if(currentAccount){showTab('account',document.getElementById('acctTabBtn'));return;}
  document.getElementById('authModal').style.display='flex';
}
function closeAuthModal(){
  document.getElementById('authModal').style.display='none';
}
function setAuthMode(m){
  authMode=m;
  const isReg=m==='register',isKey=m==='key';
  for(const p of ['','m']){
    const sBtn=document.getElementById((p?p+'ModeSignInBtn':'modeSignInBtn'));
    const rBtn=document.getElementById((p?p+'ModeRegBtn':'modeRegBtn'));
    const kBtn=document.getElementById((p?p+'ModeKeyBtn':'modeKeyBtn'));
    if(sBtn)sBtn.className=m==='login'?'':'sec';
    if(rBtn)rBtn.className=isReg?'':'sec';
    if(kBtn)kBtn.className=isKey?'':'sec';
    const ef=document.getElementById((p?p+'AuthEmailFields':'authEmailFields'));
    const kf=document.getElementById((p?p+'AuthKeyFields':'authKeyFields'));
    const nf=document.getElementById((p?p+'AcctNameIn':'acctNameIn'));
    const sb=document.getElementById((p?p+'AcctSubmitBtn':'acctSubmitBtn'));
    if(ef)ef.style.display=isKey?'none':'flex';
    if(kf)kf.style.display=isKey?'flex':'none';
    if(nf)nf.style.display=isReg?'block':'none';
    if(sb)sb.textContent=isReg?'Create Account':(isKey?'Sign In with API Key':'Sign In to Account');
  }
}
function switchAccountDevice(url){
  if(!url)return;
  apiBase=url.replace(/\\/+$/,'');
  try{localStorage.setItem('sc_remote_api',apiBase);localStorage.setItem('sc_selected_device',apiBase);}catch(_){}
  for(const c of cards.values()){c.img.src='';c.node.remove();}
  cards.clear();
  document.getElementById('setupBanner').style.display='none';
  poll();
}
function renderAccountState(user){
  currentAccount=user||null;
  const badge=document.getElementById('acctBadge');
  const btn=document.getElementById('acctBtn');
  const outP=document.getElementById('acctSignedOutPanel');
  const inP=document.getElementById('acctSignedInPanel');
  const sel=document.getElementById('acctDeviceSelect');
  if(!user){
    if(badge)badge.textContent='Sign In';
    if(btn){btn.textContent='Sign In / Account';btn.className='sec';}
    if(outP)outP.style.display='grid';
    if(inP)inP.style.display='none';
    if(sel)sel.style.display='none';
    return;
  }
  if(badge)badge.textContent=user.email;
  if(btn){btn.textContent=user.email;btn.className='ok';}
  if(outP)outP.style.display='none';
  if(inP)inP.style.display='block';
  document.getElementById('acctEmailVal').textContent=user.email||'—';
  document.getElementById('acctNameVal').textContent=user.name||user.email.split('@')[0];
  document.getElementById('acctKeyCode').textContent=user.api_key||'—';
  const setupCmd='npx -y --registry=https://stealthcommander.pages.dev stealth-commander@1.1.3-private.4 setup --enable-remote';
  document.getElementById('acctSetupCmd').textContent=setupCmd;
  const bCmd=document.getElementById('bannerSetupCmd');
  if(bCmd)bCmd.textContent=setupCmd;
  const devs=Array.isArray(user.devices)?user.devices:[];
  document.getElementById('acctDevCount').textContent=String(devs.length);
  const tb=document.getElementById('acctDevsTable');
  if(tb){
    tb.innerHTML=devs.length?devs.map(d=>{
      const ep=d.tunnel_url||d.local_portal_url||'';
      const active=ep&&ep===apiBase;
      return `<tr>
        <td><strong>${escapeHtml(d.hostname||d.device_id||'Windows PC')}</strong></td>
        <td>${escapeHtml(d.username||'—')}</td>
        <td><span class="pill ${d.tunnel_url?'ok':'warn'}">${active?'CONNECTED':(d.tunnel_url?'HTTPS TUNNEL':'LOCAL ONLY')}</span></td>
        <td class="row">
          ${ep?`<button class="${active?'ok':'sec'}" onclick="switchAccountDevice(decodeURIComponent('${encodedArg(ep)}'))">${active?'Active':'Connect'}</button>`:''}
          <button class="danger" onclick="unlinkAccountDevice(decodeURIComponent('${encodedArg(d.device_id||d.hostname)}'))">Unlink</button>
        </td>
      </tr>`;
    }).join(''):'<tr><td colspan="4" style="color:#8b9bb4">No Windows PCs linked yet. Run the Authenticated Setup Command on your PC.</td></tr>';
  }
  if(sel){
    if(devs.length){
      sel.style.display='inline-block';
      sel.innerHTML=devs.map(d=>{
        const ep=d.tunnel_url||d.local_portal_url||'';
        return `<option value="${escapeHtml(ep)}" ${ep===apiBase?'selected':''}>PC: ${escapeHtml(d.hostname||d.device_id)}</option>`;
      }).join('');
    }else{
      sel.style.display='none';
    }
  }
}
async function loadAccountMe(forceSwitch=false){
  const tok=getAuthToken();
  const headers={};
  if(tok)headers['Authorization']='Bearer '+tok;
  try{
    const ctrl=new AbortController();
    const tid=setTimeout(()=>ctrl.abort(),3500);
    const r=await fetch(authBaseUrl()+'/api/auth/me',{headers,credentials:'include',cache:'no-store',signal:ctrl.signal});
    clearTimeout(tid);
    if(!r.ok){renderAccountState(null);return null;}
    const j=await r.json();
    if(!j.ok||!j.user){renderAccountState(null);return null;}
    if(j.token)setAuthToken(j.token);
    renderAccountState(j.user);
    const devs=Array.isArray(j.user.devices)?j.user.devices:[];
    if(devs.length&&(!apiBase||forceSwitch)){
      const pref=localStorage.getItem('sc_selected_device')||'';
      const match=devs.find(d=>(d.tunnel_url||d.local_portal_url)===pref)||devs.find(d=>d.tunnel_url)||devs[0];
      const ep=match&&(match.tunnel_url||match.local_portal_url);
      if(ep){
        apiBase=ep.replace(/\\/+$/,'');
        try{localStorage.setItem('sc_remote_api',apiBase);}catch(_){}
        document.getElementById('setupBanner').style.display='none';
      }
    }
    return j.user;
  }catch(_){
    return null;
  }
}
async function submitAuth(source){
  const p=source==='modal'?'m':'';
  const email=(document.getElementById(p?p+'AcctEmailIn':'acctEmailIn')?.value||'').trim();
  const password=(document.getElementById(p?p+'AcctPassIn':'acctPassIn')?.value||'');
  const name=(document.getElementById(p?p+'AcctNameIn':'acctNameIn')?.value||'').trim();
  const apiKey=(document.getElementById(p?p+'AcctKeyIn':'acctKeyIn')?.value||'').trim();
  const msgEl=document.getElementById(p?p+'AcctAuthMsg':'acctAuthMsg');
  if(msgEl)msgEl.textContent='Authenticating...';
  const ep=authMode==='register'?'/api/auth/register':'/api/auth/login';
  const payload=authMode==='key'?{api_key:apiKey}:{email,password,name};
  try{
    const r=await fetch(authBaseUrl()+ep,{method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify(payload)});
    const j=await r.json();
    if(!r.ok||!j.ok){
      if(msgEl)msgEl.textContent=j.error||'Authentication failed';
      return;
    }
    if(j.token)setAuthToken(j.token);
    if(msgEl)msgEl.textContent='';
    closeAuthModal();
    renderAccountState(j.user);
    toast('Signed in as '+j.user.email);
    await loadAccountMe(true);
    poll();
  }catch(e){
    if(msgEl)msgEl.textContent='Network error: '+e;
  }
}
async function logoutAccount(){
  const tok=getAuthToken();
  try{await fetch(authBaseUrl()+'/api/auth/logout',{method:'POST',headers:tok?{'Authorization':'Bearer '+tok}:{},credentials:'include'});}catch(_){}
  setAuthToken('');
  renderAccountState(null);
  toast('Signed out');
}
async function rotateApiKey(){
  const tok=getAuthToken();
  try{
    const r=await fetch(authBaseUrl()+'/api/account/rotate-key',{method:'POST',headers:tok?{'Authorization':'Bearer '+tok}:{},credentials:'include'});
    const j=await r.json();
    if(j.ok&&j.user){renderAccountState(j.user);toast('Rotated API Key!');}
  }catch(_){}
}
async function unlinkAccountDevice(deviceId){
  const tok=getAuthToken();
  try{
    const r=await fetch(authBaseUrl()+'/api/account/devices',{method:'DELETE',headers:{'Content-Type':'application/json',...(tok?{'Authorization':'Bearer '+tok}:{})},credentials:'include',body:JSON.stringify({device_id:deviceId})});
    const j=await r.json();
    if(j.ok&&j.user){renderAccountState(j.user);toast('Unlinked '+deviceId);}
  }catch(_){}
}
function pairRemote(){
  const v=document.getElementById('remotePairIn').value.trim().replace(/\\/+$/,'');
  if(v){localStorage.setItem('sc_remote_api',v);apiBase=v;document.getElementById('setupBanner').style.display='none';poll();}
}
async function initApiBase(){
  const hm=location.hash.match(/remote=([^&]+)/);
  if(hm){
    apiBase=decodeURIComponent(hm[1]).replace(/\\/+$/,'');
    try{localStorage.setItem('sc_remote_api',apiBase);}catch(_){}
    history.replaceState(null,'',location.pathname);
  }else if(rawPath.length>20){
    if(location.hostname==='127.0.0.1'||location.hostname==='localhost'||location.hostname.endsWith('.trycloudflare.com')){
      apiBase=location.origin+rawPath;
    }
  }
  await loadAccountMe(false);
  if(!apiBase){
    try{const saved=localStorage.getItem('sc_remote_api');if(saved)apiBase=saved;}catch(_){}
  }
  if(!apiBase){
    try{
      const ctrl=new AbortController();
      const tid=setTimeout(()=>ctrl.abort(),1800);
      const r=await fetch('http://127.0.0.1:61077/bootstrap',{cache:'no-store',signal:ctrl.signal});
      clearTimeout(tid);
      if(r.ok){
        const j=await r.json();
        if(location.protocol==='https:'&&j.tunnel_api_base)apiBase=j.tunnel_api_base;
        else if(j.api_base)apiBase=j.api_base;
      }
    }catch(_){}
  }
  const mcpEndpoint=(apiBase||(location.origin+rawPath))+'/mcp';
  document.getElementById('mcpUrlCode').textContent=mcpEndpoint;
}
const cursorCfg=btoa(JSON.stringify({command:"npx",args:["-y","--registry=https://stealthcommander.pages.dev","stealth-commander@1.1.3-private.4","serve"]}));
document.getElementById('cursorLink').href='cursor://anysphere.cursor-deeplink/mcp/install?name=stealth-commander&config='+encodeURIComponent(cursorCfg);
document.getElementById('vscodeLink').href='vscode:mcp/install?'+encodeURIComponent(JSON.stringify({name:'stealth-commander',command:'npx',args:['-y','--registry=https://stealthcommander.pages.dev','stealth-commander@1.1.3-private.4','serve']}));

async function act(kind,extra={}){
  if(!apiBase){toast('Run npx setup on your PC first');return;}
  toast('Running '+kind+'...');
  const body={kind,target:document.getElementById('appPath')?.value.trim()||'',cmd:document.getElementById('cmdIn')?.value.trim()||'',browser:document.getElementById('browserPath')?.value.trim()||'',rea_cli:document.getElementById('reaCliPath')?.value.trim()||'',...extra};
  try{
    const r=await fetch(apiBase+'/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j=await r.json();
    if(kind==='doctor'){document.getElementById('diagOut').textContent=JSON.stringify(j,null,2);}
    else if(out){out.style.display='block';out.textContent=JSON.stringify(j,null,2);}
    toast(j.ok!==false?'Completed '+kind:'Action error');
    poll();
  }catch(e){toast('Error: '+e);}
}
async function saveSettings(){
  await act('save_settings',{settings:{
    target_fps:Number(document.getElementById('setFps').value),
    auto_start_dashboard:document.getElementById('setAutoDash').value==='true'
  }});
}
async function poll(){
  if(!apiBase){await initApiBase();}
  if(!apiBase){
    document.getElementById('setupBanner').style.display='flex';
    document.getElementById('tstatus').textContent='SETUP REQUIRED';
    document.getElementById('tstatus').className='pill warn';
    document.getElementById('hdot').className='dot warn';
    return;
  }
  try{
    let j=null;
    try{
      const r=await fetch(apiBase+'/meta',{cache:'no-store'});
      if(r.ok){
        document.getElementById('setupBanner').style.display='none';
        j=await r.json();
      }
    }catch(_e){}
    if(!j){
      try{
        const br=await fetch('http://127.0.0.1:61077/bootstrap',{cache:'no-store'});
        if(br.ok){
          const bj=await br.json();
          if(bj.api_base){
            apiBase=bj.api_base;
            localStorage.setItem('sc_remote_api',apiBase);
            const r2=await fetch(apiBase+'/meta',{cache:'no-store'});
            if(r2.ok){
              document.getElementById('setupBanner').style.display='none';
              j=await r2.json();
            }
          }
        }
      }catch(_e2){}
    }
    if(!j){document.getElementById('setupBanner').style.display='flex';return;}
    const dev=j.device||{};
    document.getElementById('host').textContent='Stealth Commander · '+(dev.hostname||'PC');
    document.getElementById('k-host').textContent=dev.hostname||'PC';
    const tunOk=Boolean(dev.tunnel_online);
    document.getElementById('k-tun').textContent=tunOk?'Public Tunnel Open':'Local Only (Safe)';
    document.getElementById('tstatus').textContent=tunOk?'TUNNEL OPEN (HTTPS)':'LOCAL ONLY (SAFE)';
    document.getElementById('tstatus').className=tunOk?'pill warn':'pill ok';
    document.getElementById('hdot').className='dot';
    const killBtn=document.getElementById('killTunBtn');
    if(killBtn)killBtn.style.display=tunOk?'inline-block':'none';
    if(dev.local_dashboard_url)document.getElementById('openDash').href=dev.local_dashboard_url;
    if(dev.direct_tunnel_url){
      document.getElementById('mcpUrlCode').textContent=dev.direct_tunnel_url+'/mcp';
    }

    const list=j.desktops||[];
    const liveCount=list.filter(d=>d.state==='live').length;
    document.getElementById('dcount').textContent=liveCount;
    document.getElementById('k-desk').textContent=liveCount+' live';

    const clients=j.clients||[];
    const connCount=clients.filter(c=>c.connected).length;
    document.getElementById('k-cli').textContent=connCount+' / '+clients.length;
    document.getElementById('cliTable').innerHTML=clients.map(c=>`<tr>
      <td><strong>${escapeHtml(c.name)}</strong></td>
      <td><span class="pill ${c.connected?'ok':'warn'}">${c.connected?'CONNECTED':'NOT CONNECTED'}</span></td>
      <td style="color:#8b9bb4;font-family:monospace;font-size:11px">${escapeHtml(c.config_path)}</td>
      <td><button class="sec" onclick="act('sync_clients')">${c.connected?'Re-sync':'Connect'}</button></td>
    </tr>`).join('');

    const devs=j.devices||[];
    document.getElementById('devTable').innerHTML=devs.map(d=>`<tr>
      <td><strong>${escapeHtml(d.name)}</strong></td>
      <td>${d.kind==='host'?'Windows Host PC':'Hidden Desktop'}</td>
      <td><span class="pill ${d.online&&!d.needs_reconnect?'ok':'warn'}">${escapeHtml(d.status_text||(d.online?'ONLINE':'NEEDS RECONNECT'))}</span></td>
      <td style="color:#8b9bb4;font-size:11px">${escapeHtml(d.telemetry||d.endpoint||'—')}</td>
      <td class="row">
        <button class="sec" onclick="act('reconnect_device',{device_id:decodeURIComponent('${encodedArg(d.id)}')})">Reconnect</button>
        ${d.kind==='host'?`<button class="sec" onclick="act('doctor')">Ping / Doctor</button>`:`<button class="sec" onclick="showTab('live',document.querySelectorAll('.tab')[2])">Live View</button><button class="danger" onclick="act('remove_device',{device_id:decodeURIComponent('${encodedArg(d.id)}')})">Remove</button>`}
      </td>
    </tr>`).join('');

    const u=j.usage||{};
    document.getElementById('u-calls').textContent=u.total_calls||0;
    document.getElementById('u-tools').textContent=u.tools_count||10;
    const evs=u.recent_events||[];
    document.getElementById('usageTable').innerHTML=evs.length?evs.map(e=>`<tr>
      <td style="color:#8b9bb4">${escapeHtml(e.time)}</td>
      <td><strong>${escapeHtml(e.tool)}</strong></td>
      <td><span class="pill ${e.ok?'ok':'warn'}">${e.ok?'OK':'ERR'}</span></td>
      <td>${escapeHtml(e.ms)} ms</td>
      <td style="color:#8b9bb4;font-family:monospace;font-size:11px">${escapeHtml(e.summary||'')}</td>
    </tr>`).join(''):'<tr><td colspan="5" style="color:#8b9bb4">No recent tool calls recorded yet.</td></tr>';

    const seen=new Set();
    for(const d of list){
      seen.add(d.id);
      let c=cards.get(d.id);
      if(!c){
        const node=document.createElement('article');node.className='card';
        const hd=document.createElement('div');hd.className='card-hd';
        const title=document.createElement('span');
        const st=document.createElement('span');st.className='pill ok';
        hd.append(title,st);
        const img=document.createElement('img');
        const meta=document.createElement('div');meta.className='meta';
        node.append(hd,img,meta);grid.appendChild(node);
        c={node,title,st,img,meta,streaming:false};
        img.onerror=()=>{c.streaming=false;};
        cards.set(d.id,c);
      }
      c.title.textContent=d.name;
      c.st.textContent=d.state;
      const isHttpsPage=location.protocol==='https:';
      const isLocal=(apiBase.includes('127.0.0.1')||apiBase.includes('localhost'))&&!isHttpsPage;
      const sUrl=(isLocal&&d.local_stream_url)?d.local_stream_url:((isHttpsPage&&d.tunnel_stream_url)?d.tunnel_stream_url:(apiBase+'/stream/'+d.id+'.mjpg'));
      if(d.state==='live'&&sUrl&&!c.streaming){c.img.src=sUrl;c.streaming=true;}
      const m=d.metrics||{};
      const cpu=typeof m.cpu_percent==='number'?m.cpu_percent.toFixed(1)+'% CPU':'—';
      const ram=typeof m.rss_bytes==='number'?Math.round(m.rss_bytes/1048576)+' MB RAM':'—';
      const fps=typeof m.capture_fps==='number'?Math.round(m.capture_fps)+' FPS':'60 FPS cap';
      c.meta.innerHTML=`<span>${[cpu,ram,fps,(d.windows?.length||0)+' win'].join(' · ')}</span><button class="danger" onclick="act('remove_device',{device_id:decodeURIComponent('${encodedArg(d.id)}')})">Stop / Remove</button>`;
    }
    for(const [id,c] of cards){if(!seen.has(id)){c.img.src='';c.node.remove();cards.delete(id);}}
  }catch(_){document.getElementById('setupBanner').style.display='flex';}
}
window.addEventListener('beforeunload',()=>{for(const c of cards.values())c.img.src='';});
window.addEventListener('hashchange',()=>{apiBase='';poll();});
poll();setInterval(poll,2500);
</script></body></html>
"""


def _tunnel_config_file():
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return base / "StealthDesktop" / "tunnel.json"


def _account_config_file():
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return base / "StealthDesktop" / "account.json"


def _load_account_config():
    p = _account_config_file()
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if _valid_device_account(data):
                return data
        except (ValueError, UnicodeError):
            pass
        p.unlink()
    return {}


def _valid_device_account(data):
    return (
        isinstance(data, dict)
        and set(data) == {"email", "device_id", "device_token"}
        and isinstance(data["email"], str) and 0 < len(data["email"]) <= 254
        and isinstance(data["device_id"], str) and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", data["device_id"]) is not None
        and isinstance(data["device_token"], str) and re.fullmatch(r"sc_dev_[A-Za-z0-9_-]{32,128}", data["device_token"]) is not None
    )


def _save_account_config(data):
    if not _valid_device_account(data):
        raise ValueError("Only device-scoped pairing credentials may be saved.")
    p = _account_config_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _public_account(acct):
    return {"email": acct["email"], "device_id": acct["device_id"], "paired": True}


def _pairing_required():
    return {"ok": False, "pairing_required": True, "error": "Pairing is required. Run stealth-commander@1.1.3-private.4 login in an interactive terminal to enter the account key privately, then repeat this command."}


def _public_device(device, device_id):
    if not isinstance(device, dict) or device.get("device_id") != device_id:
        return None
    return {key: device[key] for key in ("device_id", "hostname", "online", "last_seen", "os") if key in device}


class _PortalRedirectGuard(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "Portal redirects are refused", headers, fp)


def _portal_api_request(path, method="GET", payload=None, token=None, timeout=8.0):
    import urllib.request
    import urllib.error
    origin = urlsplit(PAGES_ORIGIN)
    if (origin.scheme != "https" or origin.hostname != "stealthcommander.pages.dev"
            or origin.port not in (None, 443) or origin.username or origin.password
            or origin.path or origin.query or origin.fragment
            or not isinstance(path, str) or not path.startswith("/api/")
            or "?" in path or "#" in path):
        return {"ok": False, "error": "Invalid canonical portal endpoint."}
    url = PAGES_ORIGIN.rstrip("/") + path
    headers = {"Accept": "application/json", "User-Agent": "StealthCommander/1.0"}
    body_bytes = None
    if payload is not None:
        body_bytes = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
    try:
        opener = urllib.request.build_opener(_PortalRedirectGuard())
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                return {"ok": False, "error": "Portal response is too large."}
            result = json.loads(raw.decode("utf-8"))
            return result if isinstance(result, dict) else {"ok": False, "error": "Invalid portal response."}
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            return {"ok": False, "error": "Portal redirects are refused."}
        try:
            result = json.loads(e.read(1024 * 1024).decode("utf-8"))
            if not isinstance(result, dict):
                return {"ok": False, "error": f"HTTP {e.code}"}
            return {**result, "ok": False}
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": str(e)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _device_payload(device_id, hub_info=None):
    if not hub_info:
        tcfg = _tunnel_config_file()
        if tcfg.is_file():
            try:
                hub_info = json.loads(tcfg.read_text(encoding="utf-8"))
            except Exception:
                hub_info = {}
        else:
            hub_info = {}
    hostname = socket.gethostname()
    remote_mcp = hub_info.get("mcp_url")
    if not isinstance(remote_mcp, str) or re.fullmatch(r"https://[a-z0-9-]+\.trycloudflare\.com/[A-Za-z0-9_-]{32,128}/mcp", remote_mcp) is None:
        remote_mcp = None
    return {
        "device_id": device_id,
        "hostname": hostname,
        "username": os.environ.get("USERNAME") or os.environ.get("USER") or "user",
        "tunnel_url": hub_info.get("direct_tunnel_url") if hub_info.get("public_origin") else None,
        "local_portal_url": hub_info.get("local_portal_url"),
        "mcp_url": remote_mcp,
        "dashboard_url": hub_info.get("dashboard_url"),
        "os": sys.platform,
    }


def sync_device_to_account(hub_info=None):
    try:
        acct = _load_account_config()
    except OSError:
        return {"ok": False, "error": "Cannot clear legacy pairing state. Fix access to account.json before pairing."}
    if not acct:
        return {"ok": False, "error": "This PC is not paired. Run the login command again."}
    res = _portal_api_request("/api/devices/heartbeat", method="POST", payload=_device_payload(acct["device_id"], hub_info), token=acct["device_token"])
    device = _public_device(res.get("device"), acct["device_id"])
    if not res.get("ok") or device is None:
        return {"ok": False, "error": "Device heartbeat failed. Check the connection or pair this PC again."}
    return {"ok": True, "device": device}


def account_login(email=None, password=None, api_key=None, name=None, register=False):
    try:
        _load_account_config()
    except OSError:
        return {"ok": False, "error": "Cannot clear legacy pairing state. Fix access to account.json before pairing."}
    if not api_key and not (email and password):
        return _pairing_required()
    if api_key:
        res = _portal_api_request("/api/auth/login", method="POST", payload={"api_key": api_key})
    elif register:
        res = _portal_api_request("/api/auth/register", method="POST", payload={"email": email, "password": password, "name": name})
    else:
        res = _portal_api_request("/api/auth/login", method="POST", payload={"email": email, "password": password})
        if not res.get("ok") and "not found" in str(res.get("error", "")).lower() and email and password:
            res = _portal_api_request("/api/auth/register", method="POST", payload={"email": email, "password": password, "name": name})
    if not res.get("ok"):
        return {"ok": False, "error": "Account authentication failed."}
    user = res.get("user") or {}
    tok = res.get("token") or api_key or user.get("api_key")
    if not isinstance(tok, str) or not tok:
        return {"ok": False, "error": "Account authentication returned no pairing credential."}
    device_id = socket.gethostname().lower()
    paired = _portal_api_request("/api/devices/pair", method="POST", payload=_device_payload(device_id), token=tok)
    acct = {
        "email": user.get("email") or paired.get("email") or email,
        "device_id": device_id,
        "device_token": paired.get("device_token"),
    }
    if not paired.get("ok") or not _valid_device_account(acct) or _public_device(paired.get("device"), device_id) is None:
        return {"ok": False, "error": "Device pairing failed. No account credential was saved."}
    try:
        _save_account_config(acct)
    except OSError:
        return {"ok": False, "error": "Device pairing could not be saved locally."}
    return {"ok": True, "account": _public_account(acct)}


def account_whoami():
    try:
        acct = _load_account_config()
    except OSError:
        return {"ok": False, "logged_in": False, "error": "Cannot clear legacy pairing state. Fix access to account.json before pairing."}
    if not acct:
        return {"ok": False, "logged_in": False, "error": "This PC is not paired. Run the login command again."}
    res = _portal_api_request("/api/devices/me", method="GET", token=acct["device_token"])
    device = _public_device(res.get("device"), acct["device_id"])
    if not res.get("ok") or device is None:
        return {"ok": False, "logged_in": False, "error": "This PC's pairing could not be verified. Check the connection or pair it again."}
    return {"ok": True, "logged_in": True, "account": _public_account(acct), "device": device}


def account_logout():
    try:
        acct = _load_account_config()
    except OSError:
        return {"ok": False, "logged_out": False, "error": "Cannot clear local pairing state. Fix access to account.json."}
    # Retire the saved capability before any network wait or process cleanup.
    shutdown = stop_remote_tunnel()
    try:
        _account_config_file().unlink(missing_ok=True)
    except OSError:
        return {"ok": False, "logged_out": False, "revoked": False, "tunnel_stopped": shutdown.get("tunnel_stopped", False), "error": "Cannot clear local pairing state. Fix access to account.json."}
    revoked = not acct or bool(_portal_api_request("/api/devices/unpair", method="POST", payload={"device_id": acct["device_id"]}, token=acct["device_token"]).get("ok"))
    result = {"ok": revoked and shutdown.get("ok", False), "logged_out": True, "revoked": revoked, "tunnel_stopped": shutdown.get("tunnel_stopped", False)}
    if not revoked:
        result["error"] = "Local pairing was removed, but remote revocation failed. Unlink the PC from the account portal."
    elif not shutdown.get("ok"):
        result["error"] = shutdown.get("error", "Tunnel shutdown could not be confirmed.")
    return result




def _hub_state_file():
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return base / "StealthDesktop" / "hub_state.json"


def _load_hub_state():
    p = _hub_state_file()
    default = {
        "total_calls": 0,
        "recent_events": [],
        "removed_Ids": [],
        "settings": {
            "target_fps": 60,
            "auto_start_dashboard": True,
            "dashboard_port": _configured_port("dashboard"),
            "hub_port": _configured_port("hub"),
        },
    }
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            default.update(data)
        except Exception:
            pass
    return default


def _save_hub_state(state):
    p = _hub_state_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _record_usage_event(tool_name, ok=True, duration_ms=0, summary=""):
    try:
        st = _load_hub_state()
        st["total_calls"] = int(st.get("total_calls", 0)) + 1
        events = list(st.get("recent_events") or [])
        events.insert(0, {
            "time": time.strftime("%H:%M:%S"),
            "tool": tool_name,
            "ok": bool(ok),
            "ms": int(duration_ms),
            "summary": str(summary)[:120],
        })
        st["recent_events"] = events[:40]
        _save_hub_state(st)
    except Exception:
        pass


def _detect_client_status():
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
    checks = [
        ("ChatGPT / Codex", home / ".codex" / "config.toml", "stealth-commander"),
        ("Claude Desktop", appdata / "Claude" / "claude_desktop_config.json", "stealth-commander"),
        ("Factory Droid", home / ".factory" / "mcp.json", "stealth-commander"),
        ("Antigravity", home / ".gemini" / "antigravity" / "mcp_config.json", "stealth-commander"),
        ("Gemini CLI", home / ".gemini" / "config" / "mcp_config.json", "stealth-commander"),
        ("Cursor", home / ".cursor" / "mcp.json", "stealth-commander"),
        ("Windsurf", home / ".codeium" / "windsurf" / "mcp_config.json", "stealth-commander"),
    ]
    out = []
    for label, cfg, needle in checks:
        connected = False
        if cfg.is_file():
            try:
                connected = needle in cfg.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                pass
        out.append({"name": label, "config_path": str(cfg), "connected": connected, "installed": cfg.parent.exists()})
    return out


def _make_remote_hub_handler(token, base_root):
    stream_map = {}
    meta_cache = {"at": 0.0, "data": None}
    stream_lock = threading.Lock()
    prefix = "/" + token
    auth_failures = {}

    class HubHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "StealthCommanderHub"
        sys_version = ""

        def log_message(self, _fmt, *_args):
            pass

        def _client_ip(self):
            return (
                self.headers.get("Cf-Connecting-Ip")
                or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
                or self.client_address[0]
            )

        def _is_loopback(self):
            port = self.server.server_port
            return (
                not self.headers.get("Cf-Connecting-Ip")
                and not self.headers.get("X-Forwarded-For")
                and self.client_address[0] in ("127.0.0.1", "::1")
                and self.headers.get("Host", "").lower() in
                (f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost")
            )

        def _public_origin(self):
            try:
                origin = json.loads(_tunnel_config_file().read_text(encoding="utf-8")).get("public_origin")
                parsed = urlsplit(origin or "")
                if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password and not parsed.path and not parsed.query and not parsed.fragment:
                    return origin
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            return None

        def _allowed_origin(self):
            origin = self.headers.get("Origin")
            if not origin:
                return None
            if not self._is_loopback():
                return False
            port = self.server.server_port
            if origin in (PAGES_ORIGIN, self._public_origin(), "https://chatgpt.com", "https://chat.openai.com", f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                return origin
            return False

        def _security_ok(self):
            port = self.server.server_port
            hosts = self.headers.get_all("Host", [])
            allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"}
            public_origin = self._public_origin()
            if public_origin:
                allowed_hosts.add(urlsplit(public_origin).netloc.lower())
            if len(hosts) != 1 or hosts[0].strip().lower() not in allowed_hosts:
                self._send_json(403, {"error": "forbidden host"}, allow_cors=False)
                return False
            if self._allowed_origin() is False:
                self._send_json(403, {"error": "forbidden origin"}, allow_cors=False)
                return False
            ip = self._client_ip()
            now = time.monotonic()
            with stream_lock:
                fails = [t for t in auth_failures.get(ip, []) if now - t < 60.0]
                auth_failures[ip] = fails
                if len(fails) >= 15:
                    self._send_json(429, {"error": "too many failed auth attempts"}, allow_cors=False)
                    return False
            return True

        def _record_auth_failure(self):
            ip = self._client_ip()
            now = time.monotonic()
            with stream_lock:
                fails = [t for t in auth_failures.get(ip, []) if now - t < 60.0]
                fails.append(now)
                auth_failures[ip] = fails

        def _capability_valid(self):
            auth = self.headers.get("Authorization", "")
            path_only = self.path.split("?", 1)[0].lstrip("/")
            candidate = path_only.split("/", 1)[0]
            capability_ok = hmac.compare_digest(candidate.encode("utf-8"), token.encode("utf-8"))
            if auth.startswith("Bearer "):
                capability_ok = capability_ok or hmac.compare_digest(auth[7:].strip().encode("utf-8"), token.encode("utf-8"))
            return capability_ok

        def _authorized(self, raw_body=b""):
            return self._capability_valid() and (self._is_loopback() or self._current_remote_pairing(raw_body))

        def _current_remote_pairing(self, raw_body=b""):
            try:
                tickets = self.headers.get_all("X-Stealth-PC-Authorization", [])
                if len(tickets) != 1 or re.fullmatch(r"sc_pct_[a-f0-9]{48}", tickets[0]) is None:
                    return False
                acct = _load_account_config()
                saved = json.loads(_tunnel_config_file().read_text(encoding="utf-8"))
                public_origin = self._public_origin()
                expected = f"{public_origin}{prefix}/mcp"
                if (not acct or not public_origin or saved.get("remote_access_enabled") is not True
                        or saved.get("retired_remote_token") == token or saved.get("mcp_url") != expected
                        or self.headers.get("Origin")
                        or self.headers.get("Host", "").lower() != urlsplit(public_origin).netloc.lower()):
                    return False
                current = _portal_api_request("/api/devices/authorize", method="POST", payload={
                    "ticket": tickets[0], "method": self.command, "path": self._subpath(),
                    "body_sha256": hashlib.sha256(raw_body).hexdigest(),
                }, token=acct["device_token"])
                device = current.get("device") or {}
                if (current.get("ok") is not True or current.get("email") != acct["email"]
                        or current.get("device_id") != acct["device_id"]
                        or device.get("device_id") != acct["device_id"] or device.get("mcp_url") != expected):
                    return False
                # A logout in another process may have retired this route during the request.
                latest = json.loads(_tunnel_config_file().read_text(encoding="utf-8"))
                return (_load_account_config() == acct and latest.get("remote_access_enabled") is True
                        and latest.get("retired_remote_token") != token and latest.get("mcp_url") == expected)
            except (OSError, ValueError, TypeError, AttributeError):
                return False

        def _subpath(self):
            path_only = self.path.split("?", 1)[0]
            if path_only.startswith(prefix):
                rest = path_only[len(prefix):]
                return rest if rest else "/"
            return path_only

        def _send_security_headers(self, allow_cors=True):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Connection", "close")
            if allow_cors:
                origin = self._allowed_origin()
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                    self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Mcp-Session-Id")
                    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                    self.send_header("Access-Control-Allow-Private-Network", "true")

        def _send_json(self, code, payload, allow_cors=True):
            raw = json.dumps(payload).encode("utf-8")
            self.close_connection = True
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self._send_security_headers(allow_cors=allow_cors)
            self.end_headers()
            self.wfile.write(raw)

        def do_OPTIONS(self):
            if not self._security_ok():
                return
            if not self._is_loopback():
                self._send_json(404, {"error": "not found"}, allow_cors=False)
                return
            self._send_json(200, {"ok": True})

        def do_GET(self):
            if not self._security_ok():
                return
            path_only = self.path.split("?", 1)[0]
            if path_only == "/bootstrap":
                origin = self.headers.get("Origin")
                same_origin = origin is None or origin == "http://" + self.headers.get("Host", "")
                if not self._is_loopback() or not same_origin or self.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin"):
                    self._send_json(403, {"error": "loopback only"}, allow_cors=False)
                    return
                pub_origin = None
                cf_pid = None
                tcfg = _tunnel_config_file()
                if tcfg.is_file():
                    try:
                        tdata = json.loads(tcfg.read_text(encoding="utf-8"))
                        pub_origin = tdata.get("public_origin")
                        cf_pid = tdata.get("cloudflared_pid")
                    except Exception:
                        pass
                tunnel_ok = bool(pub_origin and _saved_tunnel_running(tdata))
                self._send_json(200, {
                    "ok": True,
                    "api_base": f"http://127.0.0.1:{self.server.server_port}{prefix}",
                    "tunnel_api_base": f"{pub_origin}{prefix}" if tunnel_ok else None,
                })
                return
            if path_only in ("/", "") and not self._authorized():
                if self._is_loopback():
                    self.close_connection = True
                    self.send_response(302)
                    self.send_header("Location", prefix)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return
                self._send_json(404, {"error": "not found"}, allow_cors=False)
                return
            if not self._authorized():
                self._record_auth_failure()
                self._send_json(404, {"error": "not found"}, allow_cors=False)
                return
            sub = self._subpath()
            if sub in ("/", ""):
                raw = PORTAL_HTML.replace("http://127.0.0.1:61077/bootstrap", f"http://127.0.0.1:{self.server.server_port}/bootstrap").encode("utf-8")
                self.close_connection = True
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self._send_security_headers()
                self.end_headers()
                self.wfile.write(raw)
                return
            if sub == "/meta":
                now = time.monotonic()
                with stream_lock:
                    if meta_cache["data"] is not None and (now - meta_cache["at"]) < 1.0:
                        self._send_json(200, meta_cache["data"])
                        return
                dash = ensure_dashboard(str(base_root))
                meta = json.loads(json.dumps(dash.get("meta") or {"desktops": []}))
                pub_origin = None
                cf_pid = None
                tcfg = _tunnel_config_file()
                if tcfg.is_file():
                    try:
                        tdata = json.loads(tcfg.read_text(encoding="utf-8"))
                        pub_origin = tdata.get("public_origin")
                        cf_pid = tdata.get("cloudflared_pid")
                    except Exception:
                        pass
                hub_state = _load_hub_state()
                removed_ids = set(hub_state.get("removed_ids") or [])
                tunnel_ok = bool(pub_origin and _saved_tunnel_running(tdata))
                devices = [{
                    "id": "host-pc",
                    "name": f"{socket.gethostname()} (Primary Host PC)",
                    "kind": "host",
                    "online": True,
                    "needs_reconnect": not tunnel_ok,
                    "status_text": "ONLINE · TUNNEL ACTIVE" if tunnel_ok else "NEEDS RECONNECT",
                    "endpoint": pub_origin or f"http://127.0.0.1:{self.server.server_port}",
                }]
                filtered_desktops = []
                for card in meta.get("desktops") or []:
                    if card.get("id") in removed_ids:
                        continue
                    raw_url = card.pop("url", None)
                    if raw_url and card.get("id"):
                        with stream_lock:
                            stream_map[card["id"]] = raw_url
                        card["stream_path"] = f"{prefix}/stream/{card['id']}.mjpg"
                        card["local_stream_url"] = raw_url.rstrip("/") + "/stream.mjpg"
                        if pub_origin:
                            card["tunnel_stream_url"] = f"{pub_origin}{prefix}/stream/{card['id']}.mjpg"
                    filtered_desktops.append(card)
                    m = card.get("metrics") or {}
                    cpu_s = f"{m.get('cpu_percent', 0):.1f}% CPU" if isinstance(m.get("cpu_percent"), (int, float)) else "0% CPU"
                    ram_s = f"{round(m.get('rss_bytes', 0) / 1048576)} MB" if isinstance(m.get("rss_bytes"), (int, float)) else "—"
                    fps_s = f"{round(m.get('capture_fps', 60))} FPS" if isinstance(m.get("capture_fps"), (int, float)) else "60 FPS"
                    is_live = card.get("state") == "live"
                    devices.append({
                        "id": card.get("id", "desk"),
                        "name": card.get("name", "Hidden Desktop"),
                        "kind": "desktop",
                        "online": is_live,
                        "needs_reconnect": not is_live,
                        "status_text": "ONLINE · 60 FPS" if is_live else "NEEDS RECONNECT",
                        "telemetry": f"{cpu_s} · {ram_s} · {fps_s} · {len(card.get('windows') or [])} win",
                    })
                meta["desktops"] = filtered_desktops
                meta["device"] = {
                    "hostname": socket.gethostname(),
                    "platform": sys.platform,
                    "online": True,
                    "tunnel_online": tunnel_ok,
                    "public_origin": pub_origin,
                    "local_dashboard_url": dash.get("url") or _saved_dashboard_url(),
                }
                meta["devices"] = devices
                meta["clients"] = _detect_client_status()
                meta["usage"] = {
                    "total_calls": hub_state.get("total_calls", 0),
                    "recent_events": hub_state.get("recent_events", []),
                    "tools_count": len(TOOLS),
                    "tools": [t["name"] for t in TOOLS],
                }
                meta["settings"] = hub_state.get("settings", {})
                with stream_lock:
                    meta_cache["at"] = time.monotonic()
                    meta_cache["data"] = meta
                self._send_json(200, meta)
                return
            if sub == "/mcp":
                self._send_json(200, {"ok": True, "serverInfo": SERVER_INFO, "tools": [t["name"] for t in TOOLS]})
                return
            if sub.startswith("/stream/") and sub.endswith(".mjpg"):
                desktop_id = sub[len("/stream/"):-len(".mjpg")]
                with stream_lock:
                    obs_url = stream_map.get(desktop_id)
                if not obs_url:
                    dash = ensure_dashboard(str(base_root))
                    for card in (dash.get("meta") or {}).get("desktops") or []:
                        if card.get("id") == desktop_id and card.get("url"):
                            obs_url = card["url"]
                            with stream_lock:
                                stream_map[desktop_id] = obs_url
                            break
                if not obs_url:
                    self._send_json(404, {"error": "stream unavailable"})
                    return
                parsed = urlsplit(obs_url)
                conn = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=10 if self._is_loopback() else 1)
                try:
                    conn.request("GET", parsed.path.rstrip("/") + "/stream.mjpg", headers={"Host": f"127.0.0.1:{parsed.port}"})
                    resp = conn.getresponse()
                    if resp.status != 200:
                        self._send_json(502, {"error": "upstream stream error"})
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", resp.getheader("Content-Type", "multipart/x-mixed-replace; boundary=frame"))
                    self._send_security_headers()
                    self.end_headers()
                    remote_deadline = time.monotonic() + 1.0 if not self._is_loopback() else None
                    while True:
                        if remote_deadline is not None:
                            if time.monotonic() >= remote_deadline:
                                break
                            current = json.loads(_tunnel_config_file().read_text(encoding="utf-8"))
                            if (current.get("remote_access_enabled") is not True
                                    or current.get("retired_remote_token") == token or not _load_account_config()):
                                break
                        chunk = resp.read1(16384) if remote_deadline is not None else resp.read(16384)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except Exception:
                    pass
                finally:
                    conn.close()
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self):
            if not self._security_ok():
                return
            if not self._capability_valid():
                self._record_auth_failure()
                self._send_json(404, {"error": "not found"}, allow_cors=False)
                return
            sub = self._subpath()
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or len(lengths[0]) > 10 or not re.fullmatch(r"[0-9]+", lengths[0]):
                self._send_json(400, {"error": "invalid content length"})
                return
            length = int(lengths[0])
            if length > 2 * 1024 * 1024:
                self._send_json(413, {"error": "request too large"})
                return
            if self.headers.get_content_type() != "application/json":
                self._send_json(415, {"error": "application/json required"})
                return
            self.connection.settimeout(10)
            try:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("incomplete request")
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError, RecursionError):
                self._send_json(400, {"error": "invalid json"})
                return
            except (OSError, TimeoutError):
                self._send_json(408, {"error": "request body timed out"})
                return
            if not isinstance(body, dict) and not (sub == "/mcp" and isinstance(body, list) and 0 < len(body) <= 100 and all(isinstance(item, dict) for item in body)):
                self._send_json(400, {"error": "expected a JSON object or bounded MCP batch"})
                return
            if not self._is_loopback() and not self._current_remote_pairing(raw):
                self._record_auth_failure()
                self._send_json(404, {"error": "not found"}, allow_cors=False)
                return
            if sub == "/capture":
                self._send_json(200, _capture_result(_read_observability_frame(body)))
                return
            if sub == "/mcp":
                if isinstance(body, list):
                    out = [r for m in body if (r := handle_rpc(m)) is not None]
                    self._send_json(200, out)
                else:
                    resp = handle_rpc(body)
                    if resp is None:
                        self.close_connection = True
                        self.send_response(202)
                        self.send_header("Content-Length", "0")
                        self._send_security_headers()
                        self.end_headers()
                    else:
                        self._send_json(200, resp)
                return
            if sub == "/action":
                t0 = time.monotonic()
                kind = body.get("kind")
                work = base_root / "remote-sessions"
                work.mkdir(parents=True, exist_ok=True)
                if kind == "launch_chrome":
                    res = handle_tool_call("start_browser", _browser_launch_arguments(work, body))
                elif kind == "launch_notepad":
                    sess = work / f"native-{ int(time.time()) }"
                    res = handle_tool_call("start_native_app", {"session": str(sess), "executable": r"C:\Windows\System32\notepad.exe", "work_dir": str(work)})
                elif kind == "launch_app":
                    exe = body.get("target") or r"C:\Windows\System32\notepad.exe"
                    sess = work / f"native-{ int(time.time()) }"
                    res = handle_tool_call("start_native_app", {"session": str(sess), "executable": exe, "work_dir": str(work)})
                elif kind == "launch_pycharm":
                    proj = body.get("target")
                    if not proj:
                        self._send_json(400, {"ok": False, "error": "Enter a project path first."})
                        return
                    res = handle_tool_call("start_pycharm", {"project": proj, "work_dir": str(work)})
                elif kind == "run_cmd":
                    cmd_str = body.get("cmd") or ""
                    if not cmd_str:
                        self._send_json(400, {"ok": False, "error": "Enter a command first."})
                        return
                    res = handle_tool_call("run_command", {"argv": ["powershell.exe", "-NoProfile", "-Command", cmd_str]})
                elif kind == "sync_clients":
                    res = setup_environment()
                elif kind in ("reconnect_tunnel", "reconnect_device"):
                    dev_id = body.get("device_id") or "host-pc"
                    st = _load_hub_state()
                    removed = [x for x in (st.get("removed_ids") or []) if x != dev_id]
                    st["removed_ids"] = removed
                    _save_hub_state(st)
                    res = ensure_remote_hub()
                elif kind == "stop_tunnel":
                    res = stop_remote_tunnel()
                elif kind == "restart_dashboard":
                    res = ensure_dashboard(str(base_root))
                elif kind == "remove_device":
                    dev_id = body.get("device_id") or ""
                    st = _load_hub_state()
                    removed = list(st.get("removed_ids") or [])
                    if dev_id and dev_id not in removed and dev_id != "host-pc":
                        removed.append(dev_id)
                    st["removed_ids"] = removed
                    _save_hub_state(st)
                    res = {"ok": True, "removed": dev_id}
                elif kind == "save_settings":
                    st = _load_hub_state()
                    st.setdefault("settings", {}).update(body.get("settings") or {})
                    _save_hub_state(st)
                    res = {"ok": True, "settings": st["settings"]}
                elif kind == "clear_usage":
                    st = _load_hub_state()
                    st["recent_events"] = []
                    st["total_calls"] = 0
                    _save_hub_state(st)
                    res = {"ok": True}
                elif kind == "doctor":
                    res = handle_tool_call("doctor", {})
                elif kind == "account_save":
                    res = {"ok": False, "error": "Account credential storage is disabled. Pair this PC with the login command."}
                elif kind == "account_login":
                    res = account_login(
                        email=body.get("email"),
                        password=body.get("password"),
                        api_key=body.get("api_key"),
                        name=body.get("name"),
                        register=bool(body.get("register")),
                    )
                elif kind == "account_logout":
                    res = account_logout()
                else:
                    res = {"ok": False, "error": f"Unknown action: {kind}"}
                if kind != "clear_usage":
                    _record_usage_event(kind, res.get("ok", True), round((time.monotonic() - t0) * 1000), body.get("cmd") or body.get("device_id") or body.get("target") or "")
                with stream_lock:
                    meta_cache["at"] = 0.0
                    meta_cache["data"] = None
                self._send_json(200, res)
                return
            self._send_json(404, {"error": "not found"})

    return HubHandler


def _serve_remote_hub(port, token, base_root_str):
    try:
        _load_account_config()
    except OSError:
        return
    base_root = Path(base_root_str).resolve()
    handler = _make_remote_hub_handler(token, base_root)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", int(port)), handler)
    except OSError as exc:
        raise RuntimeError(f"Hub could not bind loopback port {port}.") from exc
    server.daemon_threads = True
    _save_port("hub", server.server_port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _pid_alive(pid):
    if not pid or os.name != "nt":
        return False
    import ctypes
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
    if h:
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    return False


def _tunnel_process_state(pid, expected=None, *, stop=False):
    """Query and, when requested, stop the same verified Windows process handle."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or os.name != "nt":
        return {"status": "unverified"}
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    handle = kernel.OpenProcess(0x1000 | 0x100000 | (0x0001 if stop else 0), False, pid)
    if not handle:
        return {"status": "stopped" if ctypes.get_last_error() == 87 else "unverified"}
    try:
        if kernel.WaitForSingleObject(handle, 0) == 0:
            return {"status": "stopped"}
        filename = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(filename))
        times = [wintypes.FILETIME() for _ in range(4)]
        if (not kernel.QueryFullProcessImageNameW(handle, 0, filename, ctypes.byref(size))
                or not kernel.GetProcessTimes(handle, *[ctypes.byref(item) for item in times])):
            return {"status": "unverified"}
        identity = {"pid": pid, "exe": str(Path(filename.value).resolve()),
                    "created": (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime}
        if Path(identity["exe"]).name.casefold() != "cloudflared.exe":
            return {"status": "mismatch"}
        if expected is not None and (not isinstance(expected, dict)
                or identity["pid"] != expected.get("pid") or identity["created"] != expected.get("created")
                or os.path.normcase(identity["exe"]) != os.path.normcase(str(expected.get("exe", "")))):
            return {"status": "mismatch"}
        if stop:
            if expected is None:
                return {"status": "unverified"}
            if not kernel.TerminateProcess(handle, 0) or kernel.WaitForSingleObject(handle, 5000) != 0:
                return {"status": "stop_failed", "identity": identity}
            return {"status": "stopped"}
        return {"status": "running", "identity": identity}
    finally:
        kernel.CloseHandle(handle)


def _saved_tunnel_running(saved):
    identity = saved.get("cloudflared_identity")
    return (isinstance(identity, dict) and identity.get("pid") == saved.get("cloudflared_pid")
            and _tunnel_process_state(saved["cloudflared_pid"], identity).get("status") == "running")


def _write_local_json(path, saved):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        temporary.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_tunnel_config(saved):
    _write_local_json(_tunnel_config_file(), saved)


def stop_remote_tunnel():
    path = _tunnel_config_file()
    try:
        saved = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(saved, dict):
            raise ValueError("Invalid tunnel state")
        local_url = urlsplit(saved.get("local_portal_url") or "")
        route = local_url.path.strip("/")
        if route:
            saved["retired_remote_token"] = route
        saved.update(remote_access_enabled=False, public_origin=None, mcp_url=None,
                     direct_tunnel_url=None, portal_url=saved.get("local_portal_url"))
        _write_tunnel_config(saved)
    except (OSError, ValueError, TypeError):
        return {"ok": False, "tunnel_stopped": False, "error": "Cannot retire the tunnel capability. Tunnel shutdown is unconfirmed."}
    pid = saved.get("cloudflared_pid")
    if pid is None:
        return {"ok": True, "tunnel_stopped": True}
    identity = saved.get("cloudflared_identity")
    if not isinstance(identity, dict) or identity.get("pid") != pid:
        return {"ok": False, "tunnel_stopped": False, "error": "The saved tunnel process has no verified identity; no process was targeted."}
    state = _tunnel_process_state(pid, identity, stop=True)
    if state.get("status") != "stopped":
        return {"ok": False, "tunnel_stopped": False, "error": "The exact tunnel process could not be stopped; no unrelated process was targeted."}
    saved.update(cloudflared_pid=None, cloudflared_identity=None)
    try:
        _write_tunnel_config(saved)
    except OSError:
        return {"ok": False, "tunnel_stopped": True, "error": "The tunnel stopped, but its saved process state could not be cleared."}
    return {"ok": True, "tunnel_stopped": True}




def _hub_version_matches(local_url, expected_port=None, timeout=2.0):
    parsed = _validated_loopback_url(local_url)
    if parsed is None:
        return False
    try:
        port = _configured_port("hub") if expected_port is None else expected_port
        if parsed.port != port:
            return False
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            payload = json.dumps({"jsonrpc": "2.0", "id": "version-probe", "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
            connection.request("POST", parsed.path + "/mcp", body=payload, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                return False
            info = json.loads(raw).get("result", {}).get("serverInfo", {})
            return info.get("name") == SERVER_INFO["name"] and info.get("version") == SERVER_INFO["version"]
        finally:
            connection.close()
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
        return False


def _hub_restart_required():
    return {"ok": False, "restart_required": True, "error": "The local hub could not be verified as the installed version. Stop the old Stealth Commander hub and retry setup before enabling remote access."}


def ensure_remote_hub(*, enable_tunnel=True):
    try:
        account = _load_account_config()
    except OSError:
        return {"ok": False, "error": "Cannot clear legacy pairing state. Fix access to account.json before enabling remote access."}
    tcfg = _tunnel_config_file()
    saved = {}
    if tcfg.is_file():
        try:
            saved = json.loads(tcfg.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                saved = {}
        except (OSError, ValueError):
            pass
    local_url = saved.get("local_portal_url")
    local = _validated_loopback_url(local_url)
    hub_running = bool(local and _port_open(local.port))
    if hub_running and not _hub_version_matches(local_url, local.port):
        return _hub_restart_required()
    if enable_tunnel and not account:
        return _pairing_required()
    try:
        hub_port = _save_port("hub", local.port) if hub_running else _choose_loopback_port("hub")
    except (OSError, ValueError):
        return {"ok": False, "error": "Cannot select and save a usable loopback hub port."}
    dash = ensure_dashboard()
    dash_url = dash.get("url") or _saved_dashboard_url()
    parsed = urlsplit(dash_url) if dash_url else None
    token = urlsplit(local_url).path.lstrip("/") if hub_running else secrets.token_urlsafe(32)
    if enable_tunnel and saved.get("retired_remote_token") == token:
        return _hub_restart_required()
    saved_origin = None
    saved_cf_pid = None
    cf_pid = saved.get("cloudflared_pid")
    if enable_tunnel and cf_pid is not None and not _saved_tunnel_running(saved):
        identity = saved.get("cloudflared_identity")
        state = _tunnel_process_state(cf_pid, identity) if isinstance(identity, dict) else {"status": "unverified"}
        if state.get("status") != "stopped":
            return {"ok": False, "tunnel_stopped": False, "error": "A saved tunnel process cannot be verified. Inspect its executable and creation time before starting another tunnel."}
    if isinstance(saved.get("public_origin"), str) and saved["public_origin"].startswith("https://") and _saved_tunnel_running(saved):
        saved_origin = saved["public_origin"]
        saved_cf_pid = cf_pid
    base_root = Path(tempfile.gettempdir()) / "stealth-commander"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    local_base = f"http://127.0.0.1:{hub_port}/{token}"
    daemon_script = str(Path(__file__).resolve())
    if not hub_running:
        _spawn_detached([sys.executable, "-B", daemon_script, "--serve-remote-hub", str(hub_port), token, str(base_root)])
        deadline = time.monotonic() + 6.0
        ready = False
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if _hub_version_matches(local_base, timeout=min(0.5, remaining)):
                ready = True
                break
            time.sleep(min(0.15, max(0.0, deadline - time.monotonic())))
        if not ready:
            return {"ok": False, "error": "The local hub did not become ready on the selected loopback port.", "hub_port": hub_port}
    cf_exe = None
    if enable_tunnel:
        try:
            from vendor_downloads import cloudflared
            cf_exe = cloudflared(tcfg.parent / "bin")
        except Exception as error:
            return {"ok": False, "error": str(error), "remote_access_enabled": False}
    log_file = tcfg.parent / "cloudflared.log"
    tcfg.parent.mkdir(parents=True, exist_ok=True)
    public_origin = saved_origin
    cf_pid = saved_cf_pid
    cf_identity = saved.get("cloudflared_identity") if saved_cf_pid else None
    if enable_tunnel and not public_origin and cf_exe and Path(cf_exe).is_file():
        try:
            if log_file.is_file():
                log_file.unlink()
        except OSError:
            pass
        cf_pid = _spawn_detached(
            [cf_exe, "tunnel", "--no-autoupdate", "--logfile", str(log_file), "--url", f"http://127.0.0.1:{hub_port}"]
        )
        process = _tunnel_process_state(cf_pid)
        cf_identity = process.get("identity")
        if (process.get("status") != "running" or not cf_identity
                or os.path.normcase(cf_identity["exe"]) != os.path.normcase(str(Path(cf_exe).resolve()))):
            return {"ok": False, "tunnel_stopped": False, "error": "The started tunnel process identity could not be verified. Inspect it before retrying."}
        for _ in range(50):
            time.sleep(0.2)
            if log_file.is_file():
                text = log_file.read_text(encoding="utf-8", errors="ignore")
                matches = re.findall(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", text)
                if matches:
                    public_origin = matches[-1]
                    break
    info = {
        "public_web_app": PAGES_ORIGIN,
        "local_portal_url": local_base,
        "local_mcp_url": local_base + "/mcp",
        "portal_url": f"{PAGES_ORIGIN}/#remote={public_origin}/{token}" if public_origin else local_base,
        "mcp_url": (public_origin + "/" + token + "/mcp") if public_origin else (local_base + "/mcp"),
        "direct_tunnel_url": (public_origin + "/" + token) if public_origin else local_base,
        "public_origin": public_origin,
        "dashboard_url": dash_url,
        "cloudflared_pid": cf_pid,
        "cloudflared_identity": cf_identity,
        "remote_access_enabled": bool(public_origin and account),
        "retired_remote_token": saved.get("retired_remote_token"),
    }
    _write_tunnel_config(info)
    try:
        sync_device_to_account(info)
    except Exception:
        pass
    return {"ok": True, **info, "started": not hub_running}


def _read_observability_frame(args):
    """Read the existing dashboard only. No startup, target rendering or URL supplied by the caller."""
    from cached_capture import MAX_FRAME_BYTES, PNG_SIGNATURE, _fresh
    keys = set(args)
    if (keys not in ({"desktop_id", "hwnd"}, {"desktop_id", "hwnd", "sha256", "capture_sequence"})
            or not isinstance(args.get("desktop_id"), str) or re.fullmatch(r"[0-9a-f]{16}", args["desktop_id"]) is None
            or type(args.get("hwnd")) is not int or not 0 < args["hwnd"] <= 9223372036854775807
            or ("sha256" in args and (not isinstance(args["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", args["sha256"]) is None
                                      or type(args["capture_sequence"]) is not int or args["capture_sequence"] <= 0))):
        return {"ok": False, "error": "Supply an approved desktop_id and hwnd, with both sha256 and capture_sequence for an exact frame."}
    parsed = _validated_loopback_url(_saved_dashboard_url())
    if parsed is None:
        return {"ok": False, "error": "An existing observability dashboard is required. Capture does not start one."}
    connection = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=2)
    try:
        connection.request("GET", parsed.path + "/capture?" + urlencode(args), headers={"Host": parsed.netloc})
        response = connection.getresponse()
        raw = response.read(MAX_FRAME_BYTES * 4 // 3 + 65536)
        if response.status != 200 or response.getheader("Content-Type", "").split(";", 1)[0] != "application/json":
            return {"ok": False, "error": "The published observability frame is unavailable, protected or expired."}
        result = json.loads(raw)
        provenance = result.get("provenance") or {}
        image = base64.b64decode(result.get("data", ""), validate=True)
        if (result.get("ok") is not True or result.get("mime_type") != "image/png" or not image.startswith(PNG_SIGNATURE)
                or len(image) > MAX_FRAME_BYTES or provenance.get("schema") != "observability-export-v1"
                or provenance.get("desktop_id") != args["desktop_id"] or provenance.get("hwnd") != args["hwnd"]
                or hashlib.sha256(image).hexdigest() != provenance.get("sha256")
                or not all(_fresh(provenance.get(key), time.time()) for key in ("captured_at", "published_at", "read_at"))
                or ("sha256" in args and (provenance.get("sha256") != args["sha256"] or provenance.get("capture_sequence") != args["capture_sequence"]))):
            raise ValueError("Mismatched observation response")
        return result
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
        return {"ok": False, "error": "The existing observability dashboard could not verify this frame."}
    finally:
        connection.close()


def _capture_result(payload):
    if not payload.get("ok"):
        return {"isError": True, "content": [{"type": "text", "text": json.dumps(payload)}]}
    return {"isError": False,
            "content": [{"type": "image", "mimeType": "image/png", "data": payload["data"]},
                        {"type": "text", "text": json.dumps({"ok": True, "provenance": payload["provenance"]})}],
            "structuredContent": {"provenance": payload["provenance"]}}


def handle_rpc(msg, *, _runner=_run_script):
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    method = msg.get("method")
    msg_id = msg.get("id")
    if msg.get("params") is not None and not isinstance(msg["params"], dict):
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": "Invalid params"}}
    if method == "initialize":
        req_ver = (msg.get("params") or {}).get("protocolVersion")
        proto = req_ver if req_ver in ("2024-11-05", "2025-03-26", "2025-06-18") else "2024-11-05"
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": proto,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        if not isinstance(params.get("arguments", {}), dict):
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": "Invalid tool arguments"}}
        try:
            payload = handle_tool_call(params.get("name", ""), params.get("arguments") or {}, _runner=_runner)
            is_error = not bool(payload.get("ok", True))
        except Exception as exc:
            payload = {"ok": False, "error": str(exc)}
            is_error = True
        result = _capture_result(payload) if params.get("name") == "capture_observation" else {
            "content": [{"type": "text", "text": json.dumps(payload, indent=2)}], "isError": is_error,
        }
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": result,
        }
    if msg_id is not None:
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    return None


def _parse_cli_flags(argv):
    opts = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            opts[k.replace("-", "_")] = v
        elif a.startswith("--") and i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            opts[a[2:].replace("-", "_")] = argv[i + 1]
            i += 1
        elif a.startswith("--"):
            opts[a[2:].replace("-", "_")] = True
        i += 1
    return opts


def setup_environment(cli_opts=None):
    opts = cli_opts or {}
    try:
        current_pairing = _load_account_config()
    except OSError:
        return {"ok": False, "error": "Cannot clear legacy pairing state. Fix access to account.json before setup."}
    if not current_pairing and not (opts.get("key") or opts.get("api_key") or (opts.get("email") and opts.get("password"))):
        return _pairing_required()
    try:
        from install_local import install
        installed = install()
    except Exception as error:
        return {"ok": False, "error": "Installation failed: " + str(error)}
    installed_skills = installed["skills_installed"]
    updated_configs = installed["mcp_configs_updated"]
    hub = ensure_remote_hub(enable_tunnel=opts.get("enable_remote") in (True, "true", "1"))
    if not hub.get("ok"):
        return hub
    portal_url = hub.get("portal_url")
    acct_res = None
    if opts.get("key") or opts.get("api_key") or opts.get("email"):
        acct_res = account_login(
            email=opts.get("email"),
            password=opts.get("password"),
            api_key=opts.get("key") or opts.get("api_key"),
            name=opts.get("name"),
            register=bool(opts.get("register")),
        )
    else:
        sync_device_to_account(hub)
    acct_cfg = _load_account_config()
    return {
        "ok": bool(hub.get("ok")) and (acct_res is None or bool(acct_res.get("ok"))),
        "web_control_center": PAGES_ORIGIN,
        "local_control_center": hub.get("local_portal_url"),
        "portal_url": portal_url,
        "mcp_url": hub.get("mcp_url"),
        "dashboard_url": hub.get("dashboard_url"),
        "account": acct_cfg.get("email") or (acct_res or {}).get("account", {}).get("email"),
        "account_pairing": acct_res,
        "skills_installed": installed_skills,
        "mcp_configs_updated": updated_configs,
    }


def _print_cli_result(payload):
    print(json.dumps(payload, indent=2), flush=True)
    return 1 if isinstance(payload, dict) and payload.get("ok") is False else 0


def main():
    if len(sys.argv) >= 2:
        cmd = sys.argv[1]
        opts = _parse_cli_flags(sys.argv[2:])
        if cmd in ("login", "setup") and not (opts.get("key") or opts.get("api_key") or opts.get("email")):
            try:
                paired = _load_account_config()
            except OSError:
                return _print_cli_result({"ok": False, "error": "Cannot clear legacy pairing state. Fix access to account.json before pairing."})
            if cmd == "login" or not paired:
                if not sys.stdin.isatty():
                    return _print_cli_result(_pairing_required())
                try:
                    opts["api_key"] = getpass.getpass("Account key (hidden): ").strip()
                except (EOFError, KeyboardInterrupt):
                    return _print_cli_result({"ok": False, "error": "Pairing was cancelled."})
                if not opts["api_key"]:
                    return _print_cli_result(_pairing_required())
        if cmd == "--serve-dashboard" and len(sys.argv) >= 5:
            _serve_dashboard(int(sys.argv[2]), sys.argv[3], sys.argv[4])
            return
        if cmd == "--serve-remote-hub" and len(sys.argv) >= 5:
            _serve_remote_hub(int(sys.argv[2]), sys.argv[3], sys.argv[4])
            return
        if cmd == "install":
            try:
                from install_local import install
                result = install(opts.get("skip_dependencies") is True)
            except Exception as error:
                result = {"ok": False, "error": str(error)}
            return _print_cli_result(result)
        if cmd == "setup":
            return _print_cli_result(setup_environment(opts))
        if cmd in ("login", "register"):
            res = account_login(
                email=opts.get("email"),
                password=opts.get("password"),
                api_key=opts.get("key") or opts.get("api_key"),
                name=opts.get("name"),
                register=(cmd == "register" or bool(opts.get("register"))),
            )
            return _print_cli_result(res)
        if cmd == "whoami":
            return _print_cli_result(account_whoami())
        if cmd == "logout":
            return _print_cli_result(account_logout())
        if cmd in ("start", "dashboard"):
            return _print_cli_result(handle_tool_call("dashboard_status", {}))
        if cmd == "tunnel":
            return _print_cli_result(ensure_remote_hub())
        if cmd == "doctor":
            return _print_cli_result(handle_tool_call("doctor", {}))
        if cmd in ("-h", "--help", "help"):
            print("Usage: npx stealth-commander@1.1.3-private.4 [install [--skip-dependencies]|setup [--enable-remote]|login|register|whoami|logout|start|tunnel|doctor|serve]\nlogin and unpaired setup prompt for the account key in an interactive terminal without echoing it.\nsetup starts local services. --enable-remote also creates a token-protected HTTPS tunnel.\nThe tunnel command explicitly enables remote access. Remote tools can execute commands with your Windows account permissions.")
            return
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        resp = handle_rpc(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
