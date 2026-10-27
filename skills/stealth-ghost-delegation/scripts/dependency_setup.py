"""Pinned Windows dependencies in a Python-build-specific user cache."""
import hashlib
import json
import os
from pathlib import Path
import platform
import site
import struct
import subprocess
import sys
import sysconfig

PINS = {'pywin32': '311', 'Pillow': '13.0.0', 'comtypes': '1.4.16', 'psutil': '5.9.8'}
DLL_HANDLES = []


def runtime_identity():
    return {
        'implementation': sys.implementation.name,
        'cache_tag': sys.implementation.cache_tag,
        'soabi': sysconfig.get_config_var('SOABI') or '',
        'platform': sysconfig.get_platform(),
        'machine': platform.machine().lower(),
        'bits': struct.calcsize('P') * 8,
    }


def validate_runtime():
    """Validate the supported PC runtime without installing or starting anything."""
    if sys.version_info < (3, 11):
        raise RuntimeError('Stealth Commander requires Python 3.11 or newer.')
    if sys.platform != 'win32':
        raise RuntimeError('Stealth Commander requires Windows AMD64 (x64).')
    identity = runtime_identity()
    if identity['bits'] != 64:
        raise RuntimeError('Stealth Commander requires a 64-bit Python interpreter.')
    if identity['machine'] not in ('amd64', 'x86_64'):
        raise RuntimeError('Stealth Commander supports Windows AMD64 (x64) only.')
    return identity


def dependency_directory():
    lock = Path(__file__).with_name('requirements.txt').read_bytes()
    identity = json.dumps(runtime_identity(), sort_keys=True, separators=(',', ':')).encode('utf-8')
    tag = 'v2-' + hashlib.sha256(identity + b'\0' + lock).hexdigest()[:32]
    return Path.home() / '.stealth-commander' / 'python-deps' / tag


def activate():
    target = dependency_directory()
    if target.is_dir():
        paths = [str(target), *(str(target / extra) for extra in ('win32', 'win32/lib', 'Pythonwin', 'pywin32_system32')
                                if (target / extra).is_dir())]
        identities = {os.path.normcase(os.path.abspath(path)) for path in paths}

        def prioritize_cache():
            sys.path[:] = paths + [path for path in sys.path if not isinstance(path, str)
                                   or os.path.normcase(os.path.abspath(path)) not in identities]

        # .pth bootstrap imports must also see the pinned packages first.
        prioritize_cache()
        site.addsitedir(str(target))
        prioritize_cache()
        if os.name == 'nt' and (target / 'pywin32_system32').is_dir():
            DLL_HANDLES.append(os.add_dll_directory(str(target / 'pywin32_system32')))
    return target


def install():
    identity = validate_runtime()
    target = dependency_directory()
    marker = target / 'stealth-dependencies.json'
    expected = {'schema': 2, 'pins': PINS, 'runtime': identity}
    if marker.is_file() and json.loads(marker.read_text(encoding='utf-8')) == expected:
        activate()
        return target
    if target.exists():
        raise RuntimeError('Dependency directory is incomplete. Preserve it for review before retrying.')
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + '.installing')
    if staging.exists():
        raise RuntimeError('An interrupted dependency installation needs review.')
    subprocess.run([sys.executable, '-m', 'pip', '--isolated', 'install', '--index-url', 'https://pypi.org/simple', '--no-cache-dir', '--disable-pip-version-check', '--only-binary=:all:', '--require-hashes', '--ignore-installed', '--target', str(staging), '-r', str(Path(__file__).with_name('requirements.txt'))], check=True, stdout=sys.stderr, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    (staging / marker.name).write_text(json.dumps(expected, sort_keys=True) + '\n', encoding='utf-8')
    os.replace(staging, target)
    activate()
    return target
