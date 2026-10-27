"""Refresh hashes after editing the owned skill; use --check in verification."""
from pathlib import Path
import hashlib
import json
import sys

root = Path(__file__).resolve().parents[1] / "skills" / "stealth-ghost-delegation"
skip = {".runtime", "__pycache__", "node_modules", ".venv"}
files = [p for p in root.rglob("*") if p.is_file()
         and not skip.intersection(p.relative_to(root).parts)
         and p.suffix not in (".pyc", ".pyo")
         and p.name != "install-manifest.json"]
if any(p.is_symlink() for p in files):
    raise SystemExit("Source links cannot be release inputs.")
manifest = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(files)}
target = root / "install-manifest.json"
if "--check" in sys.argv:
    if not target.exists() or json.loads(target.read_text(encoding="utf-8")) != manifest:
        raise SystemExit("Install manifest is stale: run python tools/update-manifest.py")
else:
    target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
print(json.dumps({"ok": True, "verified_skill_files": len(manifest)}))
