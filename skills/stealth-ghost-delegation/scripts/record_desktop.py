"""Record an active Stealth Commander hidden desktop directly from its local observer stream to H.264 MP4.

Reads frames directly from the owned local desktop_watch.py MJPEG stream (never the website or user's monitor),
pipes them into ffmpeg (via imageio-ffmpeg or PATH), and cleans up cleanly on exit.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request


def _find_ffmpeg():
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _discover_stream_url(desktop_id=None, desktop_name=None):
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "StealthDesktop"
    plugin = base / "plugin.json"
    if not plugin.is_file():
        raise RuntimeError("Stealth Commander local dashboard config not found.")
    dash_url = (json.loads(plugin.read_text(encoding="utf-8")).get("dashboard_url") or "").rstrip("/")
    if not dash_url:
        raise RuntimeError("Dashboard URL missing in plugin.json.")
    with urllib.request.urlopen(dash_url + "/meta", timeout=5) as resp:
        meta = json.loads(resp.read().decode("utf-8"))
    desktops = meta.get("desktops") or []
    if not desktops:
        raise RuntimeError("No active hidden desktops found on local dashboard.")
    chosen = None
    for d in desktops:
        if desktop_id and d.get("id") == desktop_id:
            chosen = d
            break
        if desktop_name and (d.get("name") == desktop_name or desktop_name in str(d.get("name") or "")):
            chosen = d
            break
    if chosen is None and not desktop_id and not desktop_name:
        chosen = desktops[0]
    if not chosen or not chosen.get("url"):
        raise RuntimeError("Matching active hidden desktop stream URL is unavailable.")
    return chosen, chosen["url"].rstrip("/") + "/stream.mjpg"


def record_hidden_desktop(output_path, *, duration=8.0, desktop_id=None, desktop_name=None, fps=15):
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found (install ffmpeg or imageio-ffmpeg).")
    chosen, stream_url = _discover_stream_url(desktop_id=desktop_id, desktop_name=desktop_name)
    out = Path(output_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "mpjpeg",
        "-i", stream_url,
        "-t", f"{max(1.0, float(duration)):.2f}",
        "-vf", f"fps={int(max(2, min(30, fps)))},scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=max(20, int(duration) + 25),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg recording failed: {(proc.stderr or '').strip()[:300]}")
    return {
        "ok": True,
        "output": str(out),
        "bytes": out.stat().st_size,
        "desktop_id": chosen.get("id"),
        "desktop_name": chosen.get("name"),
        "duration_s": float(duration),
    }


def main():
    parser = argparse.ArgumentParser(description="Record an active Stealth Commander hidden desktop directly to MP4.")
    parser.add_argument("--output", required=True, help="Destination .mp4 path.")
    parser.add_argument("--duration", type=float, default=8.0, help="Recording length in seconds.")
    parser.add_argument("--id", dest="desktop_id", default=None, help="Optional dashboard desktop id.")
    parser.add_argument("--desktop", dest="desktop_name", default=None, help="Optional desktop name substring.")
    parser.add_argument("--fps", type=int, default=15, help="Output framerate (default 15).")
    args = parser.parse_args()
    try:
        res = record_hidden_desktop(
            args.output,
            duration=args.duration,
            desktop_id=args.desktop_id,
            desktop_name=args.desktop_name,
            fps=args.fps,
        )
        print(json.dumps(res, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
