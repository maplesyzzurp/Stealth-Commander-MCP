"""Restart an owned observer while preserving its URL and source applications."""

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

from restart_dashboard import NO_WINDOW, SCRIPTS, fetch, loopback_url, observer_server, stop_observer


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", required=True, type=Path)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--previous-launcher", type=Path,
                        help="Exact prior launcher when migrating from a different script directory.")
    parser.add_argument("--anchor-pid", type=int)
    parser.add_argument("--anchor-created", type=float)
    parser.add_argument("--interval", type=float)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    folder = args.watch.resolve(strict=True)
    runtime = args.runtime_dir.resolve(strict=True)
    if not folder.is_dir() or not runtime.is_dir():
        parser.error("Watch and runtime paths must be existing directories.")
    if (args.anchor_pid is None) != (args.anchor_created is None):
        parser.error("Supply --anchor-pid and --anchor-created together.")
    if args.anchor_pid is not None and (args.anchor_pid <= 0 or not math.isfinite(args.anchor_created)
                                        or args.anchor_created <= 0):
        parser.error("Supply a positive anchor PID and creation time.")
    if args.interval is not None and (not math.isfinite(args.interval) or not 0.015 <= args.interval <= 30):
        parser.error("Supply an interval from 0.015 to 30 seconds.")
    previous_launcher = args.previous_launcher.resolve(strict=True) if args.previous_launcher else None
    import desktop_watch as watch
    watch.h.dependencies(False)
    watch.h.native_modules()
    config = watch.config_at(folder)
    source = watch.observer_source(config)
    if list(watch.identity(source)) != config["source_identity"]:
        raise ValueError("Source identity mismatch.")
    url = config["url"]
    port, token = loopback_url(url)
    if config.get("port") not in (0, port):
        raise ValueError("Observer port does not match its URL.")
    if args.serve:
        watch.secrets.token_urlsafe = lambda _size: token
        watch.serve(folder)
        return
    server = observer_server(watch, folder, config, previous_launcher)
    replacement = dict(config)
    identities = {source["pid"]: source["process_created"]}
    if args.anchor_pid is not None:
        if config.get("scope") != "desktop":
            raise ValueError("An anchor update requires desktop observation scope.")
        process = watch.h.psutil.Process(args.anchor_pid)
        if process.create_time() != args.anchor_created:
            raise ValueError("Replacement anchor identity changed.")
        anchor = {"desktop": source["desktop"], "pid": process.pid,
                  "process_created": args.anchor_created, "ide_executable": process.exe()}
        verified = watch.observer_source({"anchor": anchor})
        replacement.update(anchor=anchor, source_identity=list(watch.identity(verified)))
        identities[process.pid] = args.anchor_created
    layout = fetch(url, "/workspace")
    for entry in layout["windows"]:
        process = watch.h.psutil.Process(entry["pid"])
        identities[process.pid] = process.create_time()
    if server.pid in identities:
        raise ValueError("Observer server overlaps a source application.")
    input_before = watch.h.input_desktop()
    stop_observer(watch, folder, config, previous_launcher)
    replacement["port"] = port
    if args.interval is not None:
        replacement["interval"] = args.interval
    watch.write_json(folder / "watch.json", replacement)
    (folder / "stop").unlink()
    watch.health(folder, status="starting", pid=replacement["source_identity"][1], error=None,
                 capture_error=None, heartbeat=time.time())
    environment = dict(os.environ, TEMP=str(runtime), TMP=str(runtime))
    with (runtime / "observer-restart.log").open("w", encoding="utf-8") as log:
        child = subprocess.Popen([sys.executable, "-B", str(SCRIPTS / "restart_observer.py"),
                                  "--watch", str(folder), "--runtime-dir", str(runtime), "--serve"],
                                 stdout=log, stderr=log, env=environment, creationflags=NO_WINDOW)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("Replacement observer exited; inspect observer-restart.log.")
        try:
            current = watch.config_at(folder)
            if current.get("server_pid") == child.pid and current.get("url") == url:
                observer_server(watch, folder, current)
                page = fetch(url, text=True)
                updated = fetch(url, "/workspace")
                if 'id="panelsView"' in page and 0 <= time.time() - updated["captured_at"] < 3:
                    break
        except (OSError, ValueError, watch.h.psutil.Error):
            pass
        time.sleep(.2)
    else:
        raise RuntimeError("Replacement observer did not become ready; inspect observer-restart.log.")
    result = {"same_url": current["url"] == url, "panels_available": 'id="panelsView"' in page,
              "source_processes_preserved": all(watch.h.psutil.Process(pid).create_time() == created
                                                for pid, created in identities.items()),
              "input_desktop_unchanged": watch.h.input_desktop() == input_before,
              "window_count": len(updated["windows"])}
    watch.write_json(runtime / "observer-restart-result.json", result)
    if not all(result[key] for key in result if key != "window_count"):
        raise RuntimeError("Observer restart verification failed; inspect its result file.")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
