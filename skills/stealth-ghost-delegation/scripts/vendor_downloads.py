import hashlib
import os
from pathlib import Path
import platform
import shutil
import urllib.request
import uuid

CLOUDFLARED_VERSION = '2026.8.3'
CLOUDFLARED_SHA256 = '83e726ed18ea78c5ad5213c4c3a3a27051393950d2bc8ed4de69bec12d14eaae'
CLOUDFLARED_URL = 'https://github.com/cloudflare/cloudflared/releases/download/2026.8.3/cloudflared-windows-amd64.exe'

def verified_cloudflared(path):
    path = Path(path)
    return path.is_file() and not path.is_symlink() and path.stat().st_size <= 100 * 1024 * 1024 and hashlib.sha256(path.read_bytes()).hexdigest() == CLOUDFLARED_SHA256

def cloudflared(cache_directory):
    from dependency_setup import validate_runtime
    validate_runtime()
    destination = Path(cache_directory) / 'cloudflared.exe'
    candidates = [shutil.which('cloudflared'), r'C:\Program Files (x86)\cloudflared\cloudflared.exe', r'C:\Program Files\cloudflared\cloudflared.exe', destination]
    for candidate in candidates:
        if candidate and verified_cloudflared(candidate):
            return str(Path(candidate).resolve())
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name('cloudflared-' + uuid.uuid4().hex + '.download')
    size = 0
    try:
        with urllib.request.urlopen(CLOUDFLARED_URL, timeout=30) as response, temporary.open('xb') as output:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > 100 * 1024 * 1024:
                    raise RuntimeError('Tunnel download exceeds the size limit.')
                output.write(block)
        if not verified_cloudflared(temporary):
            raise RuntimeError('Tunnel download failed its published SHA-256 check.')
        if destination.exists():
            os.replace(destination, destination.with_name('cloudflared-unverified-' + uuid.uuid4().hex + '.exe'))
        os.replace(temporary, destination)
        return str(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
