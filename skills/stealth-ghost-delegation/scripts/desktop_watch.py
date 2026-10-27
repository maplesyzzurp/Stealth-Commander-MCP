"""Watch an existing isolated session from a read-only localhost browser page."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import ctypes
import hashlib
import io
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac

import harness as h
import observe
from rea_bridge import write_json

ROOT = Path(__file__).resolve().parent
SCHEMA = 'desktop-watch-v1'
MAX_WINDOWS = 32
MAX_PIXELS = 16_000_000
DEFAULT_INTERVAL = round(1 / 60, 4)
MIN_INTERVAL = 0.015
_CAPTURE_API = None
_LAST_NS = 0
_LAST_MONO = 0.0
_NS_LOCK = threading.Lock()
_HEALTH_LOCK = threading.Lock()
_TLS = threading.local()
_OVERLAY_CLASSES = frozenset({
    'UAC_InputIndicatorOverlayWnd',
    'UAC Input Indicator',
    'MSCTFIME UI',
    'IME',
})


def _unique_ns():
    global _LAST_NS
    with _NS_LOCK:
        now = time.time_ns()
        if now <= _LAST_NS:
            now = _LAST_NS + 1
        _LAST_NS = now
        return now


def _ensure_thread_dpi():
    if getattr(_TLS, 'dpi_ready', False):
        return
    set_dpi = ctypes.windll.user32.SetThreadDpiAwarenessContext
    set_dpi.argtypes, set_dpi.restype = [ctypes.c_void_p], ctypes.c_void_p
    if not set_dpi(ctypes.c_void_p(-4)):
        raise RuntimeError('Could not use native display coordinates for capture.')
    _TLS.dpi_ready = True


def _is_overlay_window(hwnd):
    get_class = getattr(h.win32gui, 'GetClassName', None)
    if get_class is None:
        return False
    with contextlib.suppress(Exception):
        return get_class(hwnd) in _OVERLAY_CLASSES
    return False


def _capture_api():
    global _CAPTURE_API
    if _CAPTURE_API is not None:
        return _CAPTURE_API
    from ctypes import wintypes as wt

    class BitmapInfo(ctypes.Structure):
        _fields_ = [('size', wt.DWORD), ('width', wt.LONG), ('height', wt.LONG),
                    ('planes', wt.WORD), ('bits', wt.WORD), ('compression', wt.DWORD),
                    ('image_size', wt.DWORD), ('xppm', wt.LONG), ('yppm', wt.LONG),
                    ('colors', wt.DWORD), ('important', wt.DWORD)]

    gdi = ctypes.WinDLL('gdi32', use_last_error=True)
    user = ctypes.WinDLL('user32', use_last_error=True)
    handle = ctypes.c_void_p
    for fn, result, args in (
        (gdi.CreateCompatibleDC, handle, [handle]),
        (gdi.CreateDIBSection, handle, [handle, ctypes.POINTER(BitmapInfo), wt.UINT,
                                     ctypes.POINTER(handle), handle, wt.DWORD]),
        (gdi.SelectObject, handle, [handle, handle]),
        (gdi.DeleteObject, wt.BOOL, [handle]),
        (gdi.DeleteDC, wt.BOOL, [handle]),
        (user.PrintWindow, wt.BOOL, [wt.HWND, handle, wt.UINT]),
    ):
        fn.restype, fn.argtypes = result, args
    _CAPTURE_API = (BitmapInfo, gdi, user, handle)
    return _CAPTURE_API


_DX11_API = None


def _dx11_api():
    global _DX11_API
    if _DX11_API is not None:
        return _DX11_API
    from ctypes import wintypes as wt

    class GUID(ctypes.Structure):
        _fields_ = [
            ('Data1', ctypes.c_uint32),
            ('Data2', ctypes.c_uint16),
            ('Data3', ctypes.c_uint16),
            ('Data4', ctypes.c_uint8 * 8),
        ]

        def __init__(self, d1, d2, d3, *d4):
            super().__init__(d1, d2, d3, (ctypes.c_uint8 * 8)(*d4))

    class Texture2DDesc(ctypes.Structure):
        _fields_ = [
            ('Width', ctypes.c_uint),
            ('Height', ctypes.c_uint),
            ('MipLevels', ctypes.c_uint),
            ('ArraySize', ctypes.c_uint),
            ('Format', ctypes.c_uint),
            ('SampleDesc_Count', ctypes.c_uint),
            ('SampleDesc_Quality', ctypes.c_uint),
            ('Usage', ctypes.c_uint),
            ('BindFlags', ctypes.c_uint),
            ('CPUAccessFlags', ctypes.c_uint),
            ('MiscFlags', ctypes.c_uint),
        ]

    class MappedSubresource(ctypes.Structure):
        _fields_ = [
            ('pData', ctypes.c_void_p),
            ('RowPitch', ctypes.c_uint),
            ('DepthPitch', ctypes.c_uint),
        ]

    user = ctypes.WinDLL('user32', use_last_error=True)
    d3d11 = ctypes.WinDLL('d3d11', use_last_error=True)
    user.DwmGetDxSharedSurface.argtypes = [
        wt.HWND,
        ctypes.POINTER(wt.HANDLE),
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint64),
    ]
    user.DwmGetDxSharedSurface.restype = wt.BOOL
    iid_tex2d = GUID(0x6F15AAF2, 0xD208, 0x4E89, 0x9A, 0xB4, 0x48, 0x95, 0x35, 0xD3, 0x4F, 0x9C)
    _DX11_API = (wt, GUID, Texture2DDesc, MappedSubresource, user, d3d11, iid_tex2d)
    return _DX11_API


def _thread_dx11():
    bundle = getattr(_TLS, 'dx11_bundle', None)
    if bundle is not None:
        return bundle
    wt, GUID, Texture2DDesc, MappedSubresource, _user, d3d11, _iid_tex2d = _dx11_api()
    dev = ctypes.c_void_p()
    ctx = ctypes.c_void_p()
    fl = ctypes.c_uint()
    hr = d3d11.D3D11CreateDevice(
        None, 1, None, 0x20, None, 0, 7,
        ctypes.byref(dev), ctypes.byref(fl), ctypes.byref(ctx),
    )
    if hr != 0 or not dev.value or not ctx.value:
        _TLS.dx11_bundle = False
        return False
    dev_vtbl = ctypes.cast(ctypes.cast(dev, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    ctx_vtbl = ctypes.cast(ctypes.cast(ctx, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    open_shared = ctypes.CFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, wt.HANDLE, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p),
    )(dev_vtbl[28])
    create_tex = ctypes.CFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(Texture2DDesc), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    )(dev_vtbl[5])
    copy_res = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(ctx_vtbl[47])
    map_res = ctypes.CFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
        ctypes.POINTER(MappedSubresource),
    )(ctx_vtbl[14])
    unmap_res = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)(ctx_vtbl[15])
    bundle = (dev, ctx, open_shared, create_tex, copy_res, map_res, unmap_res, {})
    _TLS.dx11_bundle = bundle
    return bundle


def _grab_window_dx11(hwnd, width, height):
    with contextlib.suppress(Exception):
        bundle = _thread_dx11()
        if not bundle:
            return None
        wt, _GUID, Texture2DDesc, MappedSubresource, user, _d3d11, iid_tex2d = _dx11_api()
        dev, ctx, open_shared, create_tex, copy_res, map_res, unmap_res, staging_cache = bundle
        ph = wt.HANDLE()
        luid = ctypes.c_uint64()
        fmt = ctypes.c_uint()
        flags = ctypes.c_uint()
        upd = ctypes.c_uint64()
        if not user.DwmGetDxSharedSurface(
            hwnd, ctypes.byref(ph), ctypes.byref(luid),
            ctypes.byref(fmt), ctypes.byref(flags), ctypes.byref(upd),
        ) or not ph.value:
            return None
        tex = ctypes.c_void_p()
        if open_shared(dev, ph, ctypes.byref(iid_tex2d), ctypes.byref(tex)) != 0 or not tex.value:
            return None
        tex_vtbl = ctypes.cast(ctypes.cast(tex, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        get_desc = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.POINTER(Texture2DDesc))(tex_vtbl[10])
        release_tex = ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(tex_vtbl[2])
        try:
            desc = Texture2DDesc()
            get_desc(tex, ctypes.byref(desc))
            w, h_px, dx_fmt = int(desc.Width), int(desc.Height), int(desc.Format)
            if (w <= 0 or h_px <= 0 or w * h_px > MAX_PIXELS or dx_fmt not in (87, 88)
                    or abs(w - width) > 32 or abs(h_px - height) > 32):
                return None
            key = (w, h_px, dx_fmt)
            staging_entry = staging_cache.get(key)
            if staging_entry is None:
                if len(staging_cache) >= 6:
                    old_key = next(iter(staging_cache))
                    old_st, old_rel = staging_cache.pop(old_key)
                    old_rel(old_st)
                st_desc = Texture2DDesc(w, h_px, 1, 1, dx_fmt, 1, 0, 3, 0, 0x20000, 0)
                staging = ctypes.c_void_p()
                if create_tex(dev, ctypes.byref(st_desc), None, ctypes.byref(staging)) != 0 or not staging.value:
                    return None
                st_vtbl = ctypes.cast(ctypes.cast(staging, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
                st_release = ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(st_vtbl[2])
                staging_entry = (staging, st_release)
                staging_cache[key] = staging_entry
            staging, _ = staging_entry
            copy_res(ctx, staging, tex)
            mapped = MappedSubresource()
            if map_res(ctx, staging, 0, 1, 0, ctypes.byref(mapped)) != 0 or not mapped.pData:
                return None
            try:
                pitch = int(mapped.RowPitch)
                if pitch < w * 4:
                    return None
                data = ctypes.string_at(mapped.pData, pitch * h_px)
            finally:
                unmap_res(ctx, staging, 0)
            if not any(data[::4096]):
                return None
            return w, h_px, data
        finally:
            release_tex(tex)
    return None


def read(path):
    for attempt in range(100):
        try:
            return json.loads(Path(path).read_text(encoding='utf-8'))
        except PermissionError:
            if attempt == 99:
                raise
            time.sleep(.02)


def _native_source_state(folder):
    import native_desktop as native
    _, state = native.read_state(folder)
    if state.get('status') not in ('running', 'ready'):
        raise RuntimeError('Start the source session before watching it.')
    if not native.guardian_alive(state):
        raise RuntimeError('The exact native session guardian is unavailable.')
    launch = state.get('launch')
    if (not isinstance(launch, dict) or type(launch.get('pid')) is not int or launch['pid'] <= 0
            or not isinstance(launch.get('created'), (int, float)) or isinstance(launch['created'], bool)
            or not math.isfinite(launch['created']) or launch['created'] <= 0
            or not isinstance(launch.get('exe'), str) or not Path(launch['exe']).is_absolute()
            or Path(launch['exe']).resolve() != Path(state['executable']).resolve()):
        raise ValueError('Invalid native session launch identity.')
    if not native.same_process(launch, native.process_identity(launch['pid'])):
        raise RuntimeError('The native launch process identity changed.')
    process = h.psutil.Process(launch['pid'])
    parent = process.parent()
    if parent is None or not native.same_process(state['guardian'], native.process_identity(parent.pid)):
        raise RuntimeError('The native launch process does not belong to its guardian.')
    source = {**state, 'pid': launch['pid'], 'process_created': launch['created'],
              'ide_executable': launch['exe']}
    expected = {launch['pid']: launch}
    for child in process.children(recursive=True):
        expected[child.pid] = native.process_identity(child.pid)
    desktop = h.win32service.OpenDesktop(
        state['desktop'], 0, False, h.win32con.DESKTOP_READOBJECTS | h.win32con.DESKTOP_ENUMERATE)
    attached = []
    try:
        native.inactive(desktop, state['desktop'])
        def visit(hwnd, _):
            _, pid = h.win32process.GetWindowThreadProcessId(hwnd)
            if (pid in expected and h.win32gui.IsWindowVisible(hwnd)
                    and native.same_process(expected[pid], native.process_identity(pid))):
                attached.append(hwnd)
            return True
        h.win32gui.EnumDesktopWindows(desktop, visit, None)
        native.inactive(desktop, state['desktop'])
    finally:
        desktop.CloseDesktop()
    if not attached:
        raise RuntimeError('The native session has no owned window on its inactive desktop.')
    if not native.guardian_alive(state) or not native.same_process(launch, native.process_identity(launch['pid'])):
        raise RuntimeError('The native source identity changed during verification.')
    return source


def source_state(folder):
    folder = Path(folder).resolve()
    if (folder / 'browser-session.json').is_file():
        state = read(folder / 'browser-session.json')
        if state.get('schema') != 'rea-browser-v1' or not state.get('desktop', '').startswith('StealthREA_'):
            raise ValueError('Expected an owned REA browser session.')
        state['ide_executable'] = state['browser']
    elif (folder / 'native-session.json').is_file():
        state = _native_source_state(folder)
    else:
        state = h.session_state(folder)
    if Path(state['session']).resolve() != folder:
        raise ValueError('Session directory does not match its metadata.')
    if state.get('status') not in ('running', 'ready'):
        raise RuntimeError('Start the source session before watching it.')
    if state['desktop'] == h.input_desktop():
        raise RuntimeError('Refusing to observe the input desktop.')
    h.owned_process(state)
    return state


def identity(state):
    return (state['desktop'], state['pid'], state['process_created'], state['ide_executable'])


def observer_source(config):
    if not config.get('anchor'):
        return source_state(config['source'])
    state = config['anchor']
    if not state['desktop'].startswith(('PyCharmHarness_', 'StealthREA_')):
        raise ValueError('Expected an existing harness desktop.')
    if state['desktop'] == h.input_desktop():
        raise RuntimeError('Refusing to observe the input desktop.')
    process = h.owned_process(state)
    desktop = h.win32service.OpenDesktop(
        state['desktop'], 0, False, h.win32con.DESKTOP_READOBJECTS | h.win32con.DESKTOP_ENUMERATE)
    attached = []
    try:
        def visit(hwnd, _):
            if h.win32process.GetWindowThreadProcessId(hwnd)[1] == process.pid:
                attached.append(hwnd)
            return True
        h.win32gui.EnumDesktopWindows(desktop, visit, None)
    finally:
        desktop.CloseDesktop()
    if not attached:
        raise RuntimeError('The anchor process has no window on the selected desktop.')
    return state


def desktop_windows(source, allowed_pids=None):
    import pywintypes
    h.assert_isolation(source)
    result = []
    root_pid = source.get('pid')
    def visit(hwnd, _):
        try:
            if h.win32gui.IsWindowVisible(hwnd) and not _is_overlay_window(hwnd):
                tid, pid = h.win32process.GetWindowThreadProcessId(hwnd)
                if allowed_pids is not None and pid not in allowed_pids:
                    return True
                if h.desktop_name(h.win32service.GetThreadDesktop(tid)) != source['desktop']:
                    raise RuntimeError('A window belongs to a different desktop.')
                kind = 'ide' if allowed_pids is not None and pid == root_pid else 'application'
                result.append({'hwnd': hwnd, 'pid': pid, 'kind': kind,
                               'title': h.win32gui.GetWindowText(hwnd), 'rect': h.win32gui.GetWindowRect(hwnd)})
        except pywintypes.error:
            # A closing window can lose its thread between enumeration and inspection.
            # Skip that unverified target; the next pass discovers remaining windows.
            return True
        return True
    h.win32gui.EnumWindows(visit, None)
    return result


def desktop_target(source, original):
    h.assert_isolation(source)
    hwnd = original['hwnd']
    tid, pid = h.win32process.GetWindowThreadProcessId(hwnd)
    if pid != original['pid'] or h.desktop_name(h.win32service.GetThreadDesktop(tid)) != source['desktop']:
        raise RuntimeError('Window identity or desktop changed.')
    if not h.win32gui.IsWindowVisible(hwnd):
        raise RuntimeError('Window is no longer visible.')
    return {**original, 'rect': h.win32gui.GetWindowRect(hwnd)}


def config_at(folder):
    folder = Path(folder).resolve()
    config = read(folder / 'watch.json')
    if config.get('schema') != SCHEMA or Path(config['watch']).resolve() != folder:
        raise ValueError('Invalid watcher directory.')
    return config


def health(folder, **updates):
    with _HEALTH_LOCK:
        state = read(folder / 'browser-session.json')
        state.update(updates)
        write_json(folder / 'browser-session.json', state)


def rect_ok(rect):
    return (len(rect) == 4 and all(isinstance(v, int) and not isinstance(v, bool) and abs(v) <= 100000 for v in rect)
            and 0 < rect[2] - rect[0] <= 32768 and 0 < rect[3] - rect[1] <= 32768)


def _thread_dib(BitmapInfo, gdi, handle, width, height):
    surfaces = getattr(_TLS, 'surfaces', None)
    if surfaces is None:
        surfaces = {}
        _TLS.surfaces = surfaces
    key = (width, height)
    cached = surfaces.get(key)
    if cached is not None:
        return cached
    if len(surfaces) >= 6:
        old_key = next(iter(surfaces))
        old_dc, old_bmp, old_prev, _ = surfaces.pop(old_key)
        if old_prev and old_prev != ctypes.c_void_p(-1).value:
            gdi.SelectObject(old_dc, old_prev)
        if old_bmp:
            gdi.DeleteObject(old_bmp)
        if old_dc:
            gdi.DeleteDC(old_dc)
    memory_dc = gdi.CreateCompatibleDC(None)
    if not memory_dc:
        raise ctypes.WinError(ctypes.get_last_error())
    info = BitmapInfo()
    info.size, info.width, info.height = ctypes.sizeof(info), width, -height
    info.planes, info.bits = 1, 32
    pixels = handle()
    bitmap = gdi.CreateDIBSection(memory_dc, ctypes.byref(info), 0, ctypes.byref(pixels), None, 0)
    if not bitmap or not pixels.value:
        gdi.DeleteDC(memory_dc)
        raise ctypes.WinError(ctypes.get_last_error())
    previous = gdi.SelectObject(memory_dc, bitmap)
    if not previous or previous == ctypes.c_void_p(-1).value:
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(memory_dc)
        raise RuntimeError('Could not select the capture bitmap.')
    entry = (memory_dc, bitmap, previous, pixels)
    surfaces[key] = entry
    return entry


def _grab_window_raw(target):
    _ensure_thread_dpi()
    BitmapInfo, gdi, user, handle = _capture_api()
    hwnd = target['hwnd']
    rect = h.win32gui.GetWindowRect(hwnd)
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    if not rect_ok(rect) or width * height > MAX_PIXELS or h.win32gui.IsIconic(hwnd):
        raise RuntimeError('Window has no capturable surface within the size limit.')
    dx_frame = _grab_window_dx11(hwnd, width, height)
    if dx_frame is not None:
        return list(rect), dx_frame[0], dx_frame[1], dx_frame[2], 'D3D11'
    memory_dc, _bitmap, _previous, pixels = _thread_dib(BitmapInfo, gdi, handle, width, height)
    if not user.PrintWindow(hwnd, memory_dc, 2):
        raise RuntimeError('PrintWindow failed for this application.')
    gdi.GdiFlush()
    data = ctypes.string_at(pixels, width * height * 4)
    return list(rect), width, height, data, 'PrintWindow'


def _encode_window_png(path, width, height, data, meta):
    from PIL import Image
    if path.exists() or path.with_suffix('.capture.json').exists():
        raise ValueError('Existing captures are preserved.')
    if meta.get('method') not in observe.CAPTURE_METHODS:
        raise ValueError('The capture backend must be recorded.')
    buf = io.BytesIO()
    Image.frombytes('RGB', (width, height), data, 'raw', 'BGRX', len(data) // height, 1).save(
        buf, format='PNG', compress_level=1, compress_type=0
    )
    png_bytes = buf.getvalue()
    if len(png_bytes) > observe.MAX_CAPTURE_BYTES:
        raise RuntimeError('PNG exceeds the viewer size limit.')
    path.write_bytes(png_bytes)
    result = {'path': str(path), 'sha256': hashlib.sha256(png_bytes).hexdigest(),
              'width': width, 'height': height, 'pixel_edits': False,
              **meta}
    path.with_suffix('.capture.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


def capture_window(source, target, path):
    if path.exists() or path.with_suffix('.capture.json').exists():
        raise ValueError('Existing captures are preserved.')
    rect, width, height, data, method = _grab_window_raw(target)
    meta = {'desktop': source['desktop'], 'input_desktop': h.input_desktop(),
            **target, 'rect': rect, 'method': method}
    return _encode_window_png(path, width, height, data, meta)


def _prune_old_captures(folder, keep):
    for old in sorted((folder / 'captures').glob('*.png'))[:-MAX_WINDOWS * 10]:
        if old not in keep:
            with contextlib.suppress(PermissionError, OSError):
                old.unlink(missing_ok=True)
                old.with_suffix('.capture.json').unlink(missing_ok=True)


def _stage_frame(folder, source, scope='session', capture_sequence=None, encode_pool=None, cache=None, cache_lock=None, session_pids=None):
    from cached_capture import display_affinity_allowed
    _ensure_thread_dpi()
    h.assert_isolation(source)
    if scope == 'desktop':
        targets = desktop_windows(source)
    elif session_pids is not None:
        targets = desktop_windows(source, allowed_pids=session_pids)
    else:
        targets = [w for w in h.windows(source) if not _is_overlay_window(w['hwnd'])]
    # GetWindow supplies top-to-bottom order on this thread's private desktop.
    order, current = {}, h.win32gui.GetTopWindow(0)
    while current and current not in order and len(order) < 4096:
        order[current] = len(order)
        current = h.win32gui.GetWindow(current, h.win32con.GW_HWNDNEXT)
    targets.sort(key=lambda item: order.get(item['hwnd'], 4096))
    complete = len(targets) <= MAX_WINDOWS
    staged_entries, bounds = [], [0, 0, 1000, 760]
    changed_count = 0
    stage_started = time.monotonic()
    for index, target in enumerate(targets[:MAX_WINDOWS]):
        if time.monotonic() - stage_started > 1.0:
            health(folder, heartbeat=time.time())
            stage_started = time.monotonic()
        entry = {key: target[key] for key in ('hwnd', 'pid', 'title', 'rect')}
        entry.update(z_index=index, status='unavailable')
        pending_capture = None
        try:
            # Revalidate ownership and desktop immediately before each capture.
            target = desktop_target(source, target) if (scope == 'desktop' or session_pids is not None) else h.target_window(source, target['hwnd'])
            entry['rect'] = list(target['rect'])
            if h.win32gui.IsIconic(target['hwnd']):
                entry['status'] = 'minimized'
                entry['rect'] = list(h.win32gui.GetWindowPlacement(target['hwnd'])[4])
            else:
                if not rect_ok(entry['rect']):
                    raise RuntimeError('Window bounds exceed the capture limit.')
                width, height = entry['rect'][2] - entry['rect'][0], entry['rect'][3] - entry['rect'][1]
                if width * height > MAX_PIXELS:
                    raise RuntimeError('Window exceeds the capture pixel limit.')
                process = h.psutil.Process(target['pid'])
                process_identity = (process.create_time(), process.exe())
                if not display_affinity_allowed(target['hwnd']):
                    raise RuntimeError('The window disallows observation or its protection could not be queried.')
                rect, width, height, data, method = _grab_window_raw(target)
                if (not process.is_running() or (process.create_time(), process.exe()) != process_identity
                        or not display_affinity_allowed(target['hwnd'])):
                    raise RuntimeError('Window process identity or capture permission changed.')
                entry['rect'] = rect
                if scope == 'desktop' or session_pids is not None:
                    desktop_target(source, target)
                input_name = h.input_desktop()
                if input_name == source['desktop']:
                    raise RuntimeError('The source became the input desktop.')
                captured_at = time.time()
                meta = {'desktop': source['desktop'], 'input_desktop': input_name,
                        **target, 'rect': rect, 'captured_at': captured_at,
                        'source_identity': list(identity(source)), 'capture_scope': scope,
                        'process_created': process_identity[0], 'process_executable': process_identity[1],
                        'display_affinity': 0, 'method': method}
                hwnd = target['hwnd']
                raw_digest = hashlib.blake2b(data, digest_size=16).digest() if cache is not None else None
                if cache is not None:
                    with (cache_lock or contextlib.nullcontext()):
                        cached = cache.get(hwnd)
                        if (cached and cached[0] == raw_digest and cached[1] == width
                                and cached[2] == height and cached[3].is_file() and cached[5] == method):
                            pending_capture = ('reuse', cached[4], meta)
                        else:
                            path = folder / 'captures' / f'{_unique_ns()}-{hwnd}.png'
                            fut = encode_pool.submit(_encode_window_png, path, width, height, data, meta) if encode_pool is not None else _encode_window_png(path, width, height, data, meta)
                            cache[hwnd] = (raw_digest, width, height, path, fut, method)
                            pending_capture = ('new', fut, meta)
                            changed_count += 1
                else:
                    path = folder / 'captures' / f'{_unique_ns()}-{hwnd}.png'
                    fut = encode_pool.submit(_encode_window_png, path, width, height, data, meta) if encode_pool is not None else _encode_window_png(path, width, height, data, meta)
                    pending_capture = ('new', fut, meta)
                    changed_count += 1
                entry['status'] = 'captured'
        except Exception as error:
            entry['error'] = str(error)
        if not rect_ok(entry['rect']):
            entry['rect'] = [0, 0, 320, 80]
            entry['status'] = 'unavailable'
            pending_capture = None
        if entry['status'] != 'captured':
            complete = False
        rect = entry['rect']
        bounds = [min(bounds[0], rect[0]), min(bounds[1], rect[1]), max(bounds[2], rect[2]), max(bounds[3], rect[3])]
        staged_entries.append((entry, pending_capture))
    if bounds[2] - bounds[0] > 32768 or bounds[3] - bounds[1] > 32768:
        raise RuntimeError('Workspace bounds exceed the layout limit.')
    return {
        'captured_at': time.time(),
        'capture_monotonic': time.monotonic(),
        'capture_sequence': capture_sequence,
        'bounds': bounds,
        'staged_entries': staged_entries,
        'complete': complete,
        'omitted_count': max(0, len(targets) - MAX_WINDOWS),
        'changed_count': changed_count,
    }


def _publish_frame(folder, staged, cache=None, cache_lock=None):
    global _LAST_MONO
    entries, images = [], []
    complete = staged['complete']
    for entry, pending_capture in staged['staged_entries']:
        if pending_capture is not None:
            mode, fut_or_val, meta = pending_capture
            try:
                base = fut_or_val.result() if hasattr(fut_or_val, 'result') else fut_or_val
                if mode == 'reuse' and not Path(base['path']).is_file():
                    raise RuntimeError('Cached capture file was removed.')
                captured = {**base, **meta}
                entry['capture'] = captured
                images.append(captured)
            except Exception as error:
                entry['status'] = 'unavailable'
                entry['error'] = str(error)
                entry.pop('capture', None)
                if cache is not None:
                    with (cache_lock or contextlib.nullcontext()):
                        cache.pop(entry['hwnd'], None)
                complete = False
        entries.append(entry)
    now_wall = max(staged['captured_at'], time.time())
    now_mono = max(staged['capture_monotonic'], time.monotonic())
    if now_mono <= _LAST_MONO:
        now_mono = _LAST_MONO + 1e-6
    _LAST_MONO = now_mono
    manifest = {
        'schema': 'desktop-layout-v1',
        'captured_at': now_wall,
        'bounds': staged['bounds'],
        'windows': entries,
        'complete': complete,
        'omitted_count': staged['omitted_count'],
    }
    seq = staged['capture_sequence']
    if seq is not None:
        manifest.update(capture_sequence=seq, capture_monotonic=now_mono)
    changed = staged.get('changed_count', 1) > 0
    if changed or seq is None or seq <= 2 or seq % 15 == 0:
        write_json(folder / 'captures' / 'desktop.json', manifest)
    if images and (changed or seq is None or seq <= 2 or seq % 15 == 0):
        write_json(folder / 'captures' / 'latest.json', max(images, key=lambda item: item['width'] * item['height']))
    if (changed and (seq is None or seq % 20 == 0)) or seq is None or seq <= 2:
        keep = {Path(item['path']) for item in images}
        _prune_old_captures(folder, keep)
    return manifest


def collect(folder, source, scope='session', capture_sequence=None):
    staged = _stage_frame(folder, source, scope, capture_sequence)
    return _publish_frame(folder, staged)


def _make_export_verifier(source, token, scope):
    """Read-only same-desktop authorization endpoint; does not capture or export pixels."""
    import cached_capture

    class ExportVerifier(BaseHTTPRequestHandler):
        server_version = "StealthObserverVerifier"
        sys_version = ""
        protocol_version = "HTTP/1.0"

        def log_message(self, *_args):
            pass

        def _reply(self, code):
            payload = b'{"ok":true}' if code == 200 else b'{"ok":false}'
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            try:
                length = self.headers.get_all("Content-Length", [])
                auth = self.headers.get_all("X-Stealth-Observer-Token", [])
                if (self.path != "/verify" or self.client_address[0] != "127.0.0.1"
                        or self.headers.get_all("Host", []) != [f"127.0.0.1:{self.server.server_port}"]
                        or self.headers.get("Origin") or self.headers.get("X-Forwarded-For")
                        or self.headers.get("Cf-Connecting-Ip")
                        or len(auth) != 1 or not hmac.compare_digest(auth[0], token)
                        or len(length) != 1 or not length[0].isdigit()
                        or not 1 <= int(length[0]) <= 1024
                        or self.headers.get_content_type() != "application/json"):
                    self._reply(404)
                    return
                raw = self.rfile.read(int(length[0]))
                if len(raw) != int(length[0]):
                    self._reply(409)
                    return
                data = json.loads(raw.decode("utf-8"))
                if (type(data) is not dict or set(data) !=
                        {"hwnd", "pid", "process_created", "process_executable", "desktop", "scope"}
                        or type(data["hwnd"]) is not int or data["hwnd"] <= 0
                        or type(data["pid"]) is not int or data["pid"] <= 0
                        or type(data["process_created"]) not in (int, float)
                        or not math.isfinite(data["process_created"])
                        or type(data["process_executable"]) is not str
                        or len(data["process_executable"]) > 512
                        or not Path(data["process_executable"]).is_absolute()
                        or data["desktop"] != source["desktop"] or data["scope"] != scope):
                    self._reply(409)
                    return
                h.assert_isolation(source)
                owned = h.owned_process(source)
                original = {"hwnd": data["hwnd"], "pid": data["pid"]}
                desktop_target(source, original)
                process = h.psutil.Process(data["pid"])
                if (not process.is_running()
                        or abs(process.create_time() - data["process_created"]) > .01
                        or os.path.normcase(os.path.abspath(process.exe())) !=
                           os.path.normcase(os.path.abspath(data["process_executable"]))):
                    self._reply(409)
                    return
                if scope == "session" and data["pid"] != owned.pid:
                    if owned.pid not in {p.pid for p in process.parents()}:
                        self._reply(409)
                        return
                if not cached_capture.display_affinity_allowed(data["hwnd"]):
                    self._reply(409)
                    return
                desktop_target(source, original)
                if (not process.is_running()
                        or abs(process.create_time() - data["process_created"]) > .01
                        or h.input_desktop() == source["desktop"]):
                    self._reply(409)
                    return
                self._reply(200)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError):
                self._reply(409)

    return ExportVerifier


def worker(folder):
    config = config_at(folder)
    cpu_count = os.cpu_count() or 4
    stage_workers = max(2, min(6, cpu_count // 2))
    encode_workers = max(4, min(12, cpu_count))
    stage_pool = ThreadPoolExecutor(max_workers=stage_workers)
    encode_pool = ThreadPoolExecutor(max_workers=encode_workers)
    publish_pool = ThreadPoolExecutor(max_workers=1)
    pending_stage = []
    pending_publish = []
    cache = {}
    cache_lock = threading.Lock()
    timer_period_set = False
    verifier = None
    verifier_thread = None
    try:
        with contextlib.suppress(Exception):
            if ctypes.windll.winmm.timeBeginPeriod(1) == 0:
                timer_period_set = True
        _ensure_thread_dpi()
        source = observer_source(config)
        if list(identity(source)) != config['source_identity']:
            raise RuntimeError('Source process identity changed.')
        h.assert_isolation(source)
        verifier_token = secrets.token_urlsafe(32)
        verifier = ThreadingHTTPServer(("127.0.0.1", 0),
                                     _make_export_verifier(source, verifier_token, config.get("scope", "session")))
        verifier.daemon_threads = True
        verifier_thread = threading.Thread(target=verifier.serve_forever, daemon=True)
        verifier_thread.start()
        health(folder, worker_pid=os.getpid(), worker_created=h.psutil.Process().create_time(),
               verifier_port=verifier.server_port, verifier_token=verifier_token,
               heartbeat=time.time())
        capture_sequence = 0
        last_source_check = 0.0
        last_health_write = 0.0
        session_pids = None
        base_interval = float(config['interval'])
        next_deadline = time.monotonic()

        def drain_ready(block_oldest=False):
            nonlocal last_health_write
            while pending_stage and (pending_stage[0].done() or (block_oldest and len(pending_stage) >= stage_workers)):
                staged = pending_stage.pop(0).result()
                pending_publish.append(publish_pool.submit(_publish_frame, folder, staged, cache, cache_lock))
            while pending_publish and (pending_publish[0].done() or len(pending_publish) >= 4):
                pending_publish.pop(0).result()
            if time.monotonic() - last_health_write >= 0.5:
                health(folder, status='running', heartbeat=time.time(), capture_error=None)
                last_health_write = time.monotonic()

        while not (folder / 'stop').exists():
            now = time.monotonic()
            if now - last_source_check >= 2.0 or capture_sequence == 0:
                fresh = observer_source(config)
                if identity(fresh) != identity(source):
                    raise RuntimeError('Source process identity changed.')
                if config.get('scope', 'session') != 'desktop':
                    proc = h.owned_process(source)
                    session_pids = {proc.pid, *(p.pid for p in proc.children(recursive=True))}
                last_source_check = now
            drain_ready(block_oldest=True)
            capture_sequence += 1
            stage_fut = stage_pool.submit(
                _stage_frame, folder, source, config.get('scope', 'session'),
                capture_sequence, encode_pool, cache, cache_lock, session_pids
            )
            if capture_sequence == 1:
                staged = stage_fut.result()
                publish_pool.submit(_publish_frame, folder, staged, cache, cache_lock).result()
                health(folder, status='running', heartbeat=time.time(), capture_error=None)
                last_health_write = time.monotonic()
                next_deadline = time.monotonic() + base_interval
            else:
                pending_stage.append(stage_fut)
                drain_ready(block_oldest=False)
                backlog = len(pending_stage) + len(pending_publish)
                step_scale = 1.35 if backlog >= 4 else (1.12 if backlog >= 3 else 1.0)
                effective_interval = base_interval * step_scale
                next_deadline += effective_interval
                if next_deadline < time.monotonic() - effective_interval:
                    next_deadline = time.monotonic()
            while time.monotonic() < next_deadline and not (folder / 'stop').exists():
                drain_ready(block_oldest=False)
                rem = next_deadline - time.monotonic()
                if rem > 0:
                    time.sleep(min(.003, max(.001, rem)))
        for fut in pending_stage:
            staged = fut.result()
            pending_publish.append(publish_pool.submit(_publish_frame, folder, staged, cache, cache_lock))
        for fut in pending_publish:
            fut.result()
    except Exception as error:
        health(folder, error=str(error), capture_error=str(error), heartbeat=time.time())
        raise
    finally:
        if verifier is not None:
            with contextlib.suppress(Exception):
                verifier.shutdown()
                verifier.server_close()
                if verifier_thread is not None:
                    verifier_thread.join(2)
            with contextlib.suppress(Exception):
                health(folder, verifier_port=None, verifier_token=None)
        if timer_period_set:
            with contextlib.suppress(Exception):
                ctypes.windll.winmm.timeEndPeriod(1)
        stage_pool.shutdown(wait=False, cancel_futures=True)
        publish_pool.shutdown(wait=False, cancel_futures=True)
        encode_pool.shutdown(wait=False, cancel_futures=True)


def serve(folder):
    import win32job
    config = config_at(folder)
    source = observer_source(config)
    if list(identity(source)) != config['source_identity']:
        raise RuntimeError('Source process identity changed.')
    process = thread = job = server = None
    failure = None
    active = -1
    worker_alive = False
    try:
        job = win32job.CreateJobObject(None, '')
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits['BasicLimitInformation']['LimitFlags'] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        startup = h.win32process.STARTUPINFO()
        startup.lpDesktop = 'WinSta0\\' + source['desktop']
        args = [sys.executable, '-B', str(Path(__file__).resolve()), '_worker', '--watch', str(folder)]
        process, thread, pid, _ = h.win32process.CreateProcess(sys.executable, subprocess.list2cmdline(args),
            None, None, False, h.win32con.CREATE_NO_WINDOW | h.win32con.CREATE_SUSPENDED,
            None, str(folder), startup)
        win32job.AssignProcessToJobObject(job, process)
        h.win32process.ResumeThread(thread)
        token = secrets.token_urlsafe(32)
        server = ThreadingHTTPServer(('127.0.0.1', config.get('port', 0)), observe.make_handler(folder, token))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        config.update(url=f'http://127.0.0.1:{server.server_port}/{token}', server_pid=os.getpid(),
                      server_created=h.psutil.Process().create_time(), worker_pid=pid,
                      worker_created=h.psutil.Process(pid).create_time())
        write_json(folder / 'watch.json', config)
        started = time.monotonic()
        while not (folder / 'stop').exists():
            if h.win32process.GetExitCodeProcess(process) != h.win32con.STILL_ACTIVE:
                failure = read(folder / 'browser-session.json').get('error')
                break
            if h.input_desktop() == source['desktop']:
                failure = 'Source became the input desktop; observation stopped.'
                break
            state = read(folder / 'browser-session.json')
            if time.monotonic() - started > 30 and time.time() - state.get('heartbeat', 0) > 25:
                failure = 'Capture stalled; observation stopped without terminating the application.'
                break
            time.sleep(.1)
    except Exception as error:
        failure = str(error)
    finally:
        try:
            if job:
                win32job.TerminateJobObject(job, 0)
            if process and h.win32process.GetExitCodeProcess(process) == h.win32con.STILL_ACTIVE:
                h.win32process.TerminateProcess(process, 1)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                active = (win32job.QueryInformationJobObject(job,
                    win32job.JobObjectBasicAccountingInformation)['ActiveProcesses'] if job else 0)
                worker_alive = bool(process and h.win32process.GetExitCodeProcess(process) == h.win32con.STILL_ACTIVE)
                if not active and not worker_alive:
                    break
                time.sleep(.1)
        except Exception as error:
            failure = 'Observer cleanup could not be confirmed: ' + str(error)
            active = -1
        finally:
            for handle in (thread, process, job):
                if handle:
                    handle.Close()
        if server:
            server.shutdown()
            server.server_close()
        health(folder, status='stopped' if active == 0 and not worker_alive else 'cleanup_failed',
               error=failure, stopped_at=time.time(), cleanup={'active_processes': active, 'worker_alive': worker_alive})


def start(args):
    anchor = None
    if args.desktop:
        if args.pid is None or args.scope != 'desktop':
            raise ValueError('An existing desktop needs --pid and --scope desktop.')
        process = h.psutil.Process(args.pid)
        anchor = {'desktop': args.desktop, 'pid': args.pid, 'process_created': process.create_time(),
                  'ide_executable': process.exe()}
        source = observer_source({'anchor': anchor})
    else:
        if args.pid is not None:
            raise ValueError('--pid is only used with --desktop.')
        source = source_state(args.session)
    if not math.isfinite(args.interval) or not MIN_INTERVAL <= args.interval <= 30:
        raise ValueError(f'Use an interval from {MIN_INTERVAL} to 30 seconds.')
    if not 0 <= args.port <= 65535:
        raise ValueError('Use a port between 0 and 65535.')
    folder = Path(args.work_dir).resolve() / ('watch-' + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True)
    (folder / 'captures').mkdir()
    config = {'schema': SCHEMA, 'watch': str(folder), 'source': source.get('session'), 'anchor': anchor,
              'source_identity': list(identity(source)), 'interval': args.interval,
              'scope': args.scope, 'port': args.port}
    write_json(folder / 'watch.json', config)
    write_json(folder / 'browser-session.json', {'schema': 'rea-browser-v1', 'session': str(folder),
               'desktop': source['desktop'], 'pid': source['pid'], 'status': 'starting', 'heartbeat': time.time()})
    with (folder / 'observer.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '_serve', '--watch', str(folder)],
                                   stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        current = config_at(folder)
        state = read(folder / 'browser-session.json')
        if current.get('url') and state['status'] == 'running':
            return {**current, 'status': state['status']}
        if process.poll() is not None or state['status'] in ('stopped', 'cleanup_failed'):
            raise RuntimeError(f'Observer failed; inspect {folder}: ' + str(state.get('error')))
        time.sleep(.1)
    (folder / 'stop').touch()
    raise TimeoutError(f'Observer did not start; stop requested. Inspect {folder}.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    command = sub.add_parser('start')
    target = command.add_mutually_exclusive_group(required=True)
    target.add_argument('--session', type=Path)
    target.add_argument('--desktop')
    command.add_argument('--pid', type=int)
    command.add_argument('--scope', choices=('session', 'desktop'), default='session')
    command.add_argument('--work-dir', required=True, type=Path)
    command.add_argument('--interval', type=float, default=DEFAULT_INTERVAL)
    command.add_argument('--port', type=int, default=0)
    for name in ('status', 'stop', '_serve', '_worker'):
        sub.add_parser(name).add_argument('--watch', required=True, type=Path)
    args = parser.parse_args()
    h.dependencies(False)
    h.native_modules()
    _orig_input_desktop = h.input_desktop
    def _safe_input_desktop():
        try:
            return _orig_input_desktop()
        except Exception as exc:
            if getattr(exc, 'winerror', None) == 5 or 'OpenInputDesktop' in str(exc):
                return 'Winlogon'
            raise
    h.input_desktop = _safe_input_desktop
    if args.command == 'start':
        result = start(args)
    else:
        folder = args.watch.resolve()
        config = config_at(folder)
        if args.command == '_worker':
            worker(folder)
            return
        if args.command == '_serve':
            serve(folder)
            return
        if args.command == 'stop':
            (folder / 'stop').touch()
            deadline = time.monotonic() + 15
            while read(folder / 'browser-session.json')['status'] not in ('stopped', 'cleanup_failed'):
                if time.monotonic() > deadline:
                    raise TimeoutError('Observer stop not confirmed; inspect its state and log.')
                time.sleep(.1)
        result = {**config, 'health': read(folder / 'browser-session.json')}
    print(json.dumps({'ok': True, 'result': result}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}), flush=True)
        sys.exit(1)
