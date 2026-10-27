"""Restart an owned dashboard on its existing token-protected loopback URL."""

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

SCRIPTS = Path(__file__).resolve().parent
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def loopback_url(url):
    if not isinstance(url, str) or any(ord(char) <= 32 or ord(char) >= 127 for char in url):
        raise ValueError("Expected a token-protected loopback URL.")
    parsed = urlsplit(url)
    port = parsed.port
    if (parsed.scheme != "http" or not port or parsed.netloc != f"127.0.0.1:{port}"
            or parsed.query or parsed.fragment
            or not re.fullmatch(r"/[A-Za-z0-9_-]{32,128}", parsed.path)
            or url != f"http://127.0.0.1:{port}{parsed.path}"):
        raise ValueError("Expected a token-protected loopback URL.")
    return port, parsed.path[1:]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Viewer redirects are not allowed.")


def fetch(url, suffix="", *, text=False):
    loopback_url(url)
    with build_opener(ProxyHandler({}), NoRedirect()).open(url + suffix, timeout=3) as response:
        data = response.read()
    return data.decode("utf-8") if text else json.loads(data)


def server_process(h, pid, created, url, launchers, state_flag, state_path):
    if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            or not isinstance(created, (int, float)) or isinstance(created, bool)
            or not math.isfinite(created) or created <= 0):
        raise ValueError("Invalid server process identity.")
    port, _ = loopback_url(url)
    process = h.psutil.Process(pid)
    if process.create_time() != created or Path(process.exe()).resolve() != Path(sys.executable).resolve():
        raise ValueError("Server process identity changed.")
    working_directory = Path(process.cwd())
    if not working_directory.is_absolute() or not working_directory.is_dir():
        raise ValueError("Server working directory could not be verified.")
    working_directory = working_directory.resolve(strict=True)
    command = process.cmdline()
    index = 2 if len(command) > 1 and command[1] == "-B" else 1
    if (len(command) <= index or (working_directory / command[index]).resolve()
            not in {Path(path).resolve() for path in launchers}):
        raise ValueError("Unexpected server launcher.")
    arguments = command[index + 1:]
    flags = (state_flag,) if isinstance(state_flag, str) else state_flag
    if sum(arguments.count(flag) for flag in flags) != 1:
        raise ValueError("Server command has no unique state directory.")
    position = next(arguments.index(flag) + 1 for flag in flags if flag in arguments)
    if (position >= len(arguments)
            or (working_directory / arguments[position]).resolve() != Path(state_path).resolve()):
        raise ValueError("Server command targets another state directory.")
    connections = (getattr(process, "net_connections", None) or process.connections)(kind="tcp")
    listeners = [item for item in connections if item.status == h.psutil.CONN_LISTEN]
    if not listeners or any(item.laddr.ip != "127.0.0.1" or item.laddr.port != port for item in listeners):
        raise ValueError("Server does not exclusively own the expected loopback listener.")
    owners = {item.pid for item in h.psutil.net_connections(kind="tcp")
              if item.status == h.psutil.CONN_LISTEN and item.laddr.port == port}
    if owners != {pid}:
        raise ValueError("Listener ownership is ambiguous.")
    if process.create_time() != created:
        raise ValueError("Server process identity changed during validation.")
    return process


def observer_server(watch, folder, config, previous_launcher=None):
    if config.get("source_identity", [None, None])[1] == config.get("server_pid"):
        raise ValueError("Observer identity overlaps a source process.")
    launchers = [SCRIPTS / "desktop_watch.py", SCRIPTS / "restart_observer.py"]
    if previous_launcher:
        launchers.append(previous_launcher)
    return server_process(watch.h, config["server_pid"], config["server_created"],
                          config["url"], launchers, "--watch", folder)


def stop_observer(watch, folder, config, previous_launcher=None):
    if watch.config_at(folder) != config or (folder / "stop").exists():
        raise ValueError("Observer state changed or a stop is already pending.")
    source = watch.observer_source(config)
    if list(watch.identity(source)) != config["source_identity"]:
        raise ValueError("Observer source identity changed.")
    server = observer_server(watch, folder, config, previous_launcher)
    if watch.config_at(folder) != config:
        raise ValueError("Observer state changed during validation.")
    (folder / "stop").touch(exist_ok=False)
    try:
        server.wait(20)
    except watch.h.psutil.TimeoutExpired:
        raise RuntimeError("Observer did not stop; no replacement was launched.") from None
    health = watch.read(folder / "browser-session.json")
    if (health.get("status") != "stopped" or health.get("cleanup") != {
            "active_processes": 0, "worker_alive": False}):
        raise RuntimeError("Observer worker cleanup was not confirmed.")


def confirmed_stopped(watch, folder, config):
    health = watch.read(folder / "browser-session.json")
    if health.get("status") != "stopped":
        return False
    source = config.get("source_identity")
    created = config.get("server_created")
    cleanup = health.get("cleanup")
    server_pid, worker_pid = config.get("server_pid"), config.get("worker_pid")
    if (health.get("schema") != "rea-browser-v1"
            or Path(health.get("session", "")).resolve() != folder
            or not isinstance(source, list) or len(source) != 4
            or health.get("desktop") != source[0] or health.get("pid") != source[1]
            or cleanup != {"active_processes": 0, "worker_alive": False}
            or type(cleanup["active_processes"]) is not int or cleanup["worker_alive"] is not False
            or any(not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
                   for pid in (server_pid, worker_pid))
            or server_pid == worker_pid or health.get("worker_pid") != worker_pid
            or not isinstance(created, (int, float)) or isinstance(created, bool)
            or not math.isfinite(created) or created <= 0):
        raise ValueError("Stopped observer cleanup metadata is ambiguous.")
    try:
        server = watch.h.psutil.Process(server_pid)
        if server.create_time() == created:
            raise ValueError("Stopped observer still has its original server process.")
    except watch.h.psutil.NoSuchProcess:
        pass
    try:
        watch.h.psutil.Process(worker_pid)
    except watch.h.psutil.NoSuchProcess:
        return True
    raise ValueError("Stopped observer worker identity cannot be confirmed dead.")


def dashboard_command(args, port):
    command = ["--port", str(port), "--status-file", str(args.state)]
    if args.discover_root:
        command += ["--discover-root", str(args.discover_root)]
    for root in args.scan_root:
        command += ["--scan-root", str(root)]
    for path, name in zip(args.watch, args.name):
        command += ["--watch", str(path), "--name", name]
    return command


def restart_command(args):
    command = [sys.executable, "-B", str(SCRIPTS / "restart_dashboard.py"),
               "--state", str(args.state), "--runtime-dir", str(args.runtime_dir), "--serve"]
    if args.discover_root:
        command += ["--discover-root", str(args.discover_root)]
    for root in args.scan_root:
        command += ["--scan-root", str(root)]
    for path, name in zip(args.watch, args.name):
        command += ["--watch", str(path), "--name", name]
    return command


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--watch", required=True, action="append", type=Path)
    parser.add_argument("--name", action="append", default=[])
    parser.add_argument("--discover-root", type=Path)
    parser.add_argument("--scan-root", action="append", type=Path, default=[])
    parser.add_argument("--previous-launcher", type=Path,
                        help="Exact prior launcher when migrating from a different script directory.")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.state = args.state.resolve(strict=True)
    args.runtime_dir = args.runtime_dir.resolve(strict=True)
    args.watch = [path.resolve(strict=True) for path in args.watch]
    args.scan_root = [path.resolve(strict=True) for path in args.scan_root]
    if args.discover_root:
        args.discover_root = args.discover_root.resolve(strict=True)
    if args.previous_launcher:
        args.previous_launcher = args.previous_launcher.resolve(strict=True)
    if (not args.state.is_file() or not args.runtime_dir.is_dir()
            or any(not root.is_dir() for root in args.watch + args.scan_root)
            or (args.discover_root and not args.discover_root.is_dir())
            or len(args.watch) > 16 or len(set(args.watch)) != len(args.watch)
            or (args.name and len(args.name) != len(args.watch))
            or (args.scan_root and not args.discover_root)):
        parser.error("Use existing directories, unique watches, and one name per watch; scan roots need discovery.")
    args.name = args.name or [f"Desktop {index + 1}" for index in range(len(args.watch))]
    state = json.loads(args.state.read_text(encoding="utf-8"))
    if state.get("schema") != "desktop-dashboard-v1" or state.get("status") != "running":
        raise ValueError("Expected a running dashboard state file.")
    port, token = loopback_url(state.get("url"))
    import dashboard
    import desktop_watch as watch
    dashboard.h.dependencies(False)
    dashboard.h.native_modules()
    if args.serve:
        dashboard.secrets.token_urlsafe = lambda _size: token
        dashboard.main(dashboard_command(args, port))
        return
    sources = [dashboard.approve_watch(path, name) for path, name in zip(args.watch, args.name)]
    identities = [(source["config"]["source_identity"][1], source["config"]["source_identity"][2])
                  for source in sources]
    retired = []
    if args.discover_root:
        for metadata in args.discover_root.glob("registry-*/watch-*/watch.json"):
            folder = metadata.parent.resolve(strict=True)
            if not folder.is_relative_to(args.discover_root):
                raise ValueError("Automatic observer escaped its discovery directory.")
            if folder not in args.watch:
                config = watch.config_at(folder)
                if confirmed_stopped(watch, folder, config):
                    continue
                if (folder / "stop").exists():
                    raise ValueError("Automatic observer already has a pending stop.")
                source = watch.observer_source(config)
                if list(watch.identity(source)) != config["source_identity"]:
                    raise ValueError("Automatic observer source identity changed.")
                observer_server(watch, folder, config)
                retired.append((folder, config))
                identities.append((source["pid"], source["process_created"]))
    launchers = [SCRIPTS / "dashboard.py", SCRIPTS / "restart_dashboard.py"]
    if args.previous_launcher:
        launchers.append(args.previous_launcher)
    previous = server_process(dashboard.h, state["pid"], state["created"], state["url"],
                              launchers, ("--status-file", "--state"), args.state)
    if any(pid == previous.pid for pid, _created in identities):
        raise ValueError("Dashboard identity overlaps a source process.")
    input_before = dashboard.h.input_desktop()
    if json.loads(args.state.read_text(encoding="utf-8")) != state:
        raise ValueError("Dashboard state changed during validation.")
    previous = server_process(dashboard.h, state["pid"], state["created"], state["url"],
                              launchers, ("--status-file", "--state"), args.state)
    previous.terminate()
    previous.wait(10)
    for folder, config in retired:
        stop_observer(watch, folder, config)
    environment = dict(os.environ, TEMP=str(args.runtime_dir), TMP=str(args.runtime_dir))
    with (args.runtime_dir / "dashboard-restart.log").open("w", encoding="utf-8") as log:
        child = subprocess.Popen(restart_command(args), stdout=log, stderr=log, env=environment,
                                 creationflags=NO_WINDOW)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("Dashboard exited; inspect dashboard-restart.log.")
        try:
            current = json.loads(args.state.read_text(encoding="utf-8"))
            if current.get("pid") == child.pid and current.get("url") == state["url"]:
                server_process(dashboard.h, child.pid, current["created"], current["url"],
                               [SCRIPTS / "restart_dashboard.py"], "--state", args.state)
                result = fetch(state["url"], "/meta")
                if len(result["desktops"]) >= len(sources) and result.get("build"):
                    break
        except (OSError, ValueError, dashboard.h.psutil.Error):
            pass
        time.sleep(.2)
    else:
        raise RuntimeError("Dashboard did not become ready; inspect dashboard-restart.log.")
    proof = {"same_url": current["url"] == state["url"],
             "sources_preserved": all(dashboard.h.psutil.Process(pid).create_time() == created
                                      for pid, created in identities),
             "input_desktop_unchanged": dashboard.h.input_desktop() == input_before,
             "desktop_count": len(result["desktops"])}
    dashboard.h.write_json(args.runtime_dir / "dashboard-restart-result.json", proof)
    if not all(proof[key] for key in ("same_url", "sources_preserved", "input_desktop_unchanged")):
        raise RuntimeError("Dashboard restart verification failed; inspect its result file.")
    print(json.dumps(proof))


if __name__ == "__main__":
    main()
