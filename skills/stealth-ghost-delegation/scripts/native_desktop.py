"""Launch a new native application on a private Windows desktop.

Stop requests WM_CLOSE on verified job-owned windows. An application that stays
open retains its guardian; --force-owned-job explicitly terminates that job.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

import harness as h
from rea_bridge import write_json


SCHEMA = 'stealth-native-v1'
STATE = 'native-session.json'
SCRIPT = Path(__file__).resolve()


def native():
    global job_api, pywintypes
    if os.name != 'nt':
        raise OSError('Native desktop sessions require Windows.')
    h.dependencies(False)
    import psutil
    import win32api, win32con, win32gui, win32process, win32service
    for module in (psutil, win32api, win32con, win32gui, win32process, win32service):
        setattr(h, module.__name__, module)
    import win32job as job_api
    import pywintypes


def error_code(error):
    return getattr(error, 'winerror', error.args[0] if error.args else None)


def process_identity(pid):
    process = h.psutil.Process(pid)
    identity = {'pid': pid, 'created': process.create_time(), 'exe': process.exe()}
    if not process.is_running() or process.create_time() != identity['created']:
        raise h.psutil.NoSuchProcess(pid)
    return identity


def same_process(expected, actual):
    return (expected['pid'] == actual['pid'] and expected['created'] == actual['created']
            and os.path.normcase(expected['exe']) == os.path.normcase(actual['exe']))


def job_owns(job, expected):
    try:
        handle = h.win32api.OpenProcess(0x1000, False, expected['pid'])
        try:
            return (job_api.IsProcessInJob(handle, job)
                    and same_process(expected, process_identity(expected['pid'])))
        finally:
            handle.Close()
    except (h.psutil.NoSuchProcess, pywintypes.error):
        return False


def job_processes(job):
    result = {}
    for pid in job_api.QueryInformationJobObject(job, job_api.JobObjectBasicProcessIdList):
        try:
            identity = process_identity(pid)
            if job_owns(job, identity):
                result[pid] = identity
        except h.psutil.NoSuchProcess:
            pass
    return result


def inactive(desktop, name):
    if h.desktop_name(desktop) != name or h.input_desktop() == name:
        raise RuntimeError('The owned desktop is not verified inactive.')


def new_desktop(name):
    if not re.fullmatch(r'(?:PyCharmHarness|StealthREA)_Native_[a-f0-9]{32}', name):
        raise ValueError('Invalid new desktop name.')
    station = h.win32service.GetProcessWindowStation()
    if h.desktop_name(station) != 'WinSta0':
        raise RuntimeError('The process must run in its interactive WinSta0 station.')
    access = (h.win32con.DESKTOP_CREATEWINDOW | h.win32con.DESKTOP_READOBJECTS
              | h.win32con.DESKTOP_WRITEOBJECTS | h.win32con.DESKTOP_ENUMERATE)
    try:
        existing = h.win32service.OpenDesktop(name, 0, False, access)
    except pywintypes.error as error:
        if error_code(error) != 2:
            raise
    else:
        existing.CloseDesktop()
        raise RuntimeError('The requested desktop already exists; it was not reused.')
    desktop = h.win32service.CreateDesktop(name, 0, access, None)
    try:
        inactive(desktop, name)
    except Exception:
        desktop.CloseDesktop()
        raise
    return desktop


def owned_windows(desktop, name, job, identities):
    inactive(desktop, name)
    result = []

    def visit(hwnd, _):
        try:
            if not h.win32gui.IsWindowVisible(hwnd):
                return True
            tid, pid = h.win32process.GetWindowThreadProcessId(hwnd)
            expected = identities.get(pid)
            if expected is None or not job_owns(job, expected):
                return True
            # EnumDesktopWindows proves membership without opening protected app threads.
            result.append({'hwnd': int(hwnd), 'tid': tid, **expected,
                           'title': h.win32gui.GetWindowText(hwnd),
                           'rect': list(h.win32gui.GetWindowRect(hwnd))})
        except pywintypes.error as error:
            if error_code(error) not in (0, 6, 1400):
                raise
        return True

    try:
        h.win32gui.EnumDesktopWindows(desktop, visit, None)
    except pywintypes.error as error:
        if error_code(error) not in (0, 6):
            raise
    inactive(desktop, name)
    return result


def snapshot(state, desktop, job):
    identities = job_processes(job)
    active = h.input_desktop()
    return {**state, 'processes': list(identities.values()),
            'windows': owned_windows(desktop, state['desktop'], job, identities),
            'input_desktop': active, 'inactive': active != state['desktop'],
            'input_desktop_unchanged': active == state['input_desktop_at_start'],
            'heartbeat': time.time()}


def active_processes(job):
    return job_api.QueryInformationJobObject(
        job, job_api.JobObjectBasicAccountingInformation)['ActiveProcesses']


def close_owned(state, desktop, job, force):
    if force:
        job_api.TerminateJobObject(job, 0)
    else:
        current = snapshot(state, desktop, job)
        for window in current['windows']:
            try:
                inactive(desktop, state['desktop'])
                tid, pid = h.win32process.GetWindowThreadProcessId(window['hwnd'])
                if pid == window['pid'] and tid == window['tid'] and job_owns(job, window):
                    h.win32gui.PostMessage(window['hwnd'], h.win32con.WM_CLOSE, 0, 0)
            except pywintypes.error as error:
                if error_code(error) not in (0, 6, 1400):
                    raise
    deadline = time.monotonic() + 5
    while active_processes(job) and time.monotonic() < deadline:
        time.sleep(.1)
    remaining = active_processes(job)
    return {'stopped': remaining == 0, 'active_processes': remaining,
            'forced_owned_job': bool(force), 'desktop_released': False}


def save(folder, state):
    write_json(folder / STATE, state)


def read_state(folder):
    folder = Path(folder).resolve(strict=True)
    path = folder / STATE
    if path.stat().st_size > 262144:
        raise ValueError('Native session metadata is too large.')
    state = h.read_json(path)
    if (state.get('schema') != SCHEMA or Path(state.get('session', '')).resolve() != folder
            or not re.fullmatch(r'(?:PyCharmHarness|StealthREA)_Native_[a-f0-9]{32}',
                                state.get('desktop', ''))):
        raise ValueError('Invalid native session metadata.')
    return folder, state


def guardian_alive(state):
    expected = state.get('guardian')
    if not isinstance(expected, dict):
        return False
    try:
        process = h.psutil.Process(expected['pid'])
        return (same_process(expected, process_identity(expected['pid']))
                and process.cmdline()[1:] == [str(SCRIPT), '_guardian', '--session', state['session']])
    except (h.psutil.NoSuchProcess, h.psutil.AccessDenied, KeyError):
        return False


def keeper(folder):
    _, state = read_state(folder)
    desktop = job = process = thread = None
    original_thread_desktop = h.win32service.GetThreadDesktop(h.win32api.GetCurrentThreadId())
    pending_stop = None
    cleanup = None
    state['guardian'] = process_identity(os.getpid())
    save(folder, state)
    try:
        state['input_desktop_at_start'] = h.input_desktop()
        desktop = new_desktop(state['desktop'])
        desktop.SetThreadDesktop()
        state['guardian_thread_desktop'] = h.desktop_name(
            h.win32service.GetThreadDesktop(h.win32api.GetCurrentThreadId()))
        if state['guardian_thread_desktop'] != state['desktop']:
            raise RuntimeError('The guardian thread did not attach to its new desktop.')
        inactive(desktop, state['desktop'])
        job = job_api.CreateJobObject(None, '')
        limits = job_api.QueryInformationJobObject(job, job_api.JobObjectExtendedLimitInformation)
        limits['BasicLimitInformation']['LimitFlags'] = job_api.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        job_api.SetInformationJobObject(job, job_api.JobObjectExtendedLimitInformation, limits)
        startup = h.win32process.STARTUPINFO()
        startup.lpDesktop = 'WinSta0\\' + state['desktop']
        process, thread, pid, _ = h.win32process.CreateProcess(
            state['executable'], subprocess.list2cmdline([state['executable'], *state['args']]),
            None, None, False, h.win32con.CREATE_NO_WINDOW | h.win32con.CREATE_UNICODE_ENVIRONMENT
            | h.win32con.CREATE_SUSPENDED, dict(os.environ), state['work_dir'], startup)
        state['launch'] = process_identity(pid)
        job_api.AssignProcessToJobObject(job, process)
        inactive(desktop, state['desktop'])
        h.win32process.ResumeThread(thread)
        deadline = time.monotonic() + state['startup_timeout']
        while True:
            state = snapshot(state, desktop, job)
            if state['windows']:
                state['status'] = 'ready'
                break
            if time.monotonic() >= deadline or active_processes(job) == 0:
                raise RuntimeError('No visible job-owned window appeared on the new desktop. '
                                   'The application may have exited or redirected to an existing instance.')
            time.sleep(.1)
        save(folder, state)
        while True:
            if active_processes(job) == 0:
                cleanup = close_owned(state, desktop, job, False)
                break
            for request_path in sorted((folder / 'requests').glob('*.request.json')):
                response_path = request_path.with_name(request_path.name.replace('.request.', '.response.'))
                try:
                    request = h.read_json(request_path)
                    if request.get('action') == 'status':
                        state = snapshot(state, desktop, job)
                        response = {'ok': True, 'result': state}
                    elif request.get('action') == 'stop' and isinstance(request.get('force_owned_job'), bool):
                        cleanup = close_owned(state, desktop, job, request['force_owned_job'])
                        state['cleanup'] = cleanup
                        if cleanup['stopped']:
                            pending_stop = response_path
                            request_path.unlink()
                            break
                        state['status'] = 'close_pending'
                        state = snapshot(state, desktop, job)
                        response = {'ok': False, 'error': 'The application remains open. '
                                    'The guardian and owned job are still running. '
                                    'Use --force-owned-job only if unsaved changes may be discarded.',
                                    'result': state}
                    else:
                        raise ValueError('Invalid native session request.')
                except Exception as error:
                    response = {'ok': False, 'error': str(error)}
                write_json(response_path, response)
                request_path.unlink()
                save(folder, state)
            if pending_stop:
                break
            time.sleep(.1)
    except Exception as error:
        state.update(status='failed', error=str(error))
    finally:
        try:
            if job and (not cleanup or not cleanup['stopped']):
                cleanup = close_owned(state, desktop, job, True)
            if process and h.win32process.GetExitCodeProcess(process) == h.win32con.STILL_ACTIVE:
                h.win32process.TerminateProcess(process, 1)
            deadline = time.monotonic() + 5
            while (process and h.win32process.GetExitCodeProcess(process) == h.win32con.STILL_ACTIVE
                   and time.monotonic() < deadline):
                time.sleep(.1)
            if process and h.win32process.GetExitCodeProcess(process) == h.win32con.STILL_ACTIVE:
                raise RuntimeError('The exact launched process remains alive.')
        except Exception as error:
            state.update(status='cleanup_failed', error='Cleanup could not be confirmed: ' + str(error))
        for handle in (thread, process, job):
            if handle:
                handle.Close()
        try:
            if desktop:
                original_thread_desktop.SetThreadDesktop()
                desktop.CloseDesktop()
            if cleanup:
                cleanup['desktop_released'] = True
        except Exception as error:
            state.update(status='cleanup_failed', error='Desktop release failed: ' + str(error))
        if state['status'] not in ('failed', 'cleanup_failed'):
            state['status'] = 'stopped' if cleanup and cleanup['stopped'] else 'cleanup_failed'
        if cleanup and cleanup['stopped']:
            state.update(windows=[], processes=[])
        state.update(cleanup=cleanup, stopped_at=time.time())
        save(folder, state)
        if pending_stop:
            write_json(pending_stop, {'ok': state['status'] == 'stopped', 'result': state})


def start(args):
    executable = Path(args.executable).expanduser()
    work_dir = Path(args.work_dir).expanduser()
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError('--executable must be an existing absolute executable path.')
    if executable.name.casefold() in ('chrome.exe', 'msedge.exe', 'firefox.exe', 'brave.exe', 'opera.exe'):
        raise ValueError('Use the browser adapter for browser sessions.')
    if not work_dir.is_absolute() or not work_dir.is_dir():
        raise ValueError('--work-dir must be an existing absolute directory.')
    if not 1 <= args.timeout <= 120:
        raise ValueError('--timeout must be between 1 and 120 seconds.')
    folder = Path(args.session).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=False)
    (folder / 'requests').mkdir()
    state = {'schema': SCHEMA, 'session': str(folder), 'status': 'starting',
             'desktop': args.desktop_prefix + 'Native_' + uuid.uuid4().hex,
             'executable': str(executable.resolve()), 'args': args.args,
             'work_dir': str(work_dir.resolve()), 'startup_timeout': args.timeout}
    save(folder, state)
    with (folder / 'guardian.log').open('w', encoding='utf-8') as log:
        guardian = subprocess.Popen([sys.executable, str(SCRIPT), '_guardian', '--session', str(folder)],
                                    stdout=log, stderr=log, creationflags=h.HIDDEN)
    deadline = time.monotonic() + args.timeout + 12
    while time.monotonic() < deadline:
        state = h.read_json(folder / STATE)
        if state['status'] != 'starting':
            return {'ok': state['status'] == 'ready', 'result': state,
                    **({'error': state.get('error', 'Native session did not start.')} if state['status'] != 'ready' else {})}
        if guardian.poll() is not None:
            raise RuntimeError('The guardian exited during startup. Inspect ' + str(folder / 'guardian.log'))
        time.sleep(.1)
    raise TimeoutError('Startup is still unresolved. Inspect the session before retrying: ' + str(folder))


def command(args):
    folder, state = read_state(args.session)
    if state['status'] in ('stopped', 'failed', 'cleanup_failed'):
        return {'ok': state['status'] == 'stopped', 'result': {**state, 'guardian_alive': guardian_alive(state)}}
    if not guardian_alive(state):
        _, state = read_state(folder)
        if state['status'] in ('stopped', 'failed', 'cleanup_failed'):
            return {'ok': state['status'] == 'stopped', 'result': {**state, 'guardian_alive': guardian_alive(state)}}
        raise RuntimeError('The exact guardian process is unavailable. Cleanup is unconfirmed; no process was targeted.')
    request_id = uuid.uuid4().hex
    request_path = folder / 'requests' / (request_id + '.request.json')
    response_path = request_path.with_name(request_id + '.response.json')
    write_json(request_path, {'action': args.action, 'force_owned_job': getattr(args, 'force_owned_job', False)})
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if response_path.is_file():
            result = h.read_json(response_path)
            response_path.unlink()
            return result
        _, latest = read_state(folder)
        if latest['status'] in ('stopped', 'failed', 'cleanup_failed'):
            return {'ok': latest['status'] == 'stopped', 'result': latest}
        time.sleep(.1)
    raise TimeoutError('The guardian did not reply. Inspect status before retrying.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    begin = commands.add_parser('start', help='Create a new inactive desktop and launch an explicit application.')
    begin.add_argument('--session', required=True, help='New directory for private session state.')
    begin.add_argument('--executable', required=True)
    begin.add_argument('--work-dir', required=True)
    begin.add_argument('--desktop-prefix', choices=('PyCharmHarness_', 'StealthREA_'), default='PyCharmHarness_')
    begin.add_argument('--timeout', type=float, default=20)
    begin.add_argument('args', nargs=argparse.REMAINDER, help='Application arguments after --.')
    commands.add_parser('status').add_argument('--session', required=True)
    end = commands.add_parser('stop', help='Request graceful close; keep the guardian if applications remain open.')
    end.add_argument('--session', required=True)
    end.add_argument('--force-owned-job', action='store_true', help='Terminate only this session job; unsaved changes may be lost.')
    internal = commands.add_parser('_guardian', help=argparse.SUPPRESS)
    internal.add_argument('--session', required=True)
    args = parser.parse_args(argv)
    try:
        native()
        if args.action == '_guardian':
            keeper(Path(args.session).resolve())
            return 0
        if args.action == 'start':
            if args.args[:1] == ['--']:
                args.args = args.args[1:]
            result = start(args)
        else:
            result = command(args)
    except Exception as error:
        result = {'ok': False, 'error': str(error)}
    print(json.dumps(result))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
