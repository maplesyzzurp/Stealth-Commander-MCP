"""Run REA browser scenarios on an owned, non-input Windows desktop."""

import argparse
import contextlib
import ctypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import traceback
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener
import uuid

import harness as h

ROOT = Path(__file__).resolve().parent
SCHEMA = 'rea-browser-v1'


def write_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, indent=2), encoding='utf-8')
    try:
        for attempt in range(100):
            try:
                os.replace(temp, path)
                return
            except PermissionError:
                if attempt == 99:
                    raise
                time.sleep(.02)
    finally:
        temp.unlink(missing_ok=True)


def state_at(session):
    session = Path(session).resolve()
    for attempt in range(100):
        try:
            state = h.read_json(session / 'browser-session.json')
            break
        except PermissionError:
            if attempt == 99:
                raise
            time.sleep(.02)
    if state.get('schema') != SCHEMA or Path(state['session']).resolve() != session:
        raise ValueError('Invalid browser session.')
    if not state['desktop'].startswith('StealthREA_'):
        raise ValueError('Unexpected desktop.')
    return state


def save(state):
    write_json(Path(state['session']) / 'browser-session.json', state)


def event(state, name, **details):
    with (Path(state['session']) / 'events.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'time': time.time(), 'event': name, **details}) + '\n')


def verify_identity(pid, created, executable):
    process = h.psutil.Process(pid)
    if abs(process.create_time() - created) > .01 or Path(process.exe()).resolve() != Path(executable).resolve():
        raise RuntimeError('Process identity changed; refusing to use it.')
    return process


def assert_desktop(state):
    current = h.desktop_name(h.win32service.GetThreadDesktop(h.win32api.GetCurrentThreadId()))
    if current != state['desktop'] or h.input_desktop() == state['desktop']:
        raise RuntimeError('Input isolation failed; no action was performed.')


def browser_state(state):
    return {**state, 'ide_executable': state['browser']}


def owned_windows(state):
    return h.windows(browser_state(state))


def owned_endpoint(state):
    process = verify_identity(state['pid'], state['process_created'], state['browser'])
    profile_argument = '--user-data-dir=' + str(Path(state['session']) / 'profile')
    if profile_argument not in process.cmdline():
        raise RuntimeError('The browser does not have this session\'s private profile.')
    port = state['cdp_port']
    listeners = [c for c in h.psutil.net_connections(kind='tcp')
                 if c.status == 'LISTEN' and c.laddr.port == port]
    if not listeners or any(c.pid != state['pid'] or c.laddr.ip not in ('127.0.0.1', '::1') for c in listeners):
        raise RuntimeError('The debugging endpoint is not an owned loopback listener.')
    return f'http://127.0.0.1:{port}'


def prepare_scenario(source, state):
    if not isinstance(source, dict) or 'browser' in source:
        raise ValueError('Supply a scenario without browser; the adapter selects its owned browser.')
    if not isinstance(source.get('actions'), list) or not source['actions']:
        raise ValueError('The scenario needs at least one action.')
    request = dict(source)
    if request.get('start_url') == {'url': 'demo'}:
        request['start_url'] = {'url': state['demo_url']}
    url = request.get('start_url', {}).get('url', '')
    if urlsplit(url).scheme not in ('http', 'https'):
        raise ValueError('start_url must be HTTP(S), or use {"url":"demo"}.')
    endpoint = owned_endpoint(state)
    with build_opener(ProxyHandler({})).open(endpoint + '/json/list', timeout=5) as response:
        targets = json.load(response)
    if not any(t.get('id') == state['target_id'] and t.get('type') == 'page' for t in targets):
        raise RuntimeError('The owned browser page has closed; no replacement target was selected.')
    request['browser'] = {'mode': 'connect', 'cdp_endpoint': endpoint, 'target_id': state['target_id']}
    return request


def demo_server():
    content = (ROOT.parent / 'examples' / 'browser_demo.html').read_bytes()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}' or self.path != '/':
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(content)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def capture_frame(state):
    windows = owned_windows(state)
    candidates = [w for w in windows if w['pid'] == state['pid'] and w['rect'][2] - w['rect'][0] > 300]
    if not candidates:
        raise RuntimeError('No owned browser window is ready for capture.')
    target = max(candidates, key=lambda w: (w['rect'][2]-w['rect'][0]) * (w['rect'][3]-w['rect'][1]))
    folder = Path(state['session']) / 'captures'
    destination = folder / (f'{time.time_ns()}.png')
    result = h.capture(browser_state(state), target, destination)
    result['captured_at'] = time.time()
    write_json(folder / 'latest.json', result)
    for old in sorted(folder.glob('*.png'))[:-60]:
        old.unlink(missing_ok=True)
        old.with_suffix('.capture.json').unlink(missing_ok=True)
    return result


def reply(path, ok, **details):
    write_json(path.with_name(path.name.replace('.request.json', '.response.json')), {'ok': ok, **details})
    path.unlink(missing_ok=True)


def worker(session):
    state = state_at(session)
    assert_desktop(state)
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    demo = demo_server()
    state['demo_url'] = f'http://127.0.0.1:{demo.server_port}/'
    state['worker_pid'] = os.getpid()
    state['worker_created'] = h.psutil.Process().create_time()
    profile = session / 'profile'
    (profile / 'DevToolsActivePort').unlink(missing_ok=True)
    arguments = [state['browser'], '--user-data-dir=' + str(profile), '--remote-debugging-port=0',
                 '--remote-debugging-address=127.0.0.1', '--no-first-run', '--no-default-browser-check',
                 '--hide-crash-restore-bubble', '--disable-background-networking', '--disable-component-update',
                 '--disable-sync', '--disable-extensions', '--mute-audio', '--autoplay-policy=no-user-gesture-required',
                 '--disable-gpu', '--disable-gpu-compositing', '--disable-direct-composition',
                 '--force-device-scale-factor=1', '--disable-background-timer-throttling',
                 '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows',
                 '--disable-features=CalculateNativeWinOcclusion,msEdgeSidebarV2',
                 '--window-size=1000,760', state['demo_url']]
    browser = h.NativeProcess(arguments, state['desktop'], cwd=session)
    state.update(pid=browser.pid, process_created=h.psutil.Process(browser.pid).create_time())
    running = None
    try:
        deadline = time.monotonic() + 35
        marker = profile / 'DevToolsActivePort'
        while True:
            assert_desktop(state)
            if browser.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('Browser did not expose its owned debugging endpoint.')
            if (session / 'stop').exists():
                return
            try:
                lines = marker.read_text().splitlines()
                if lines:
                    state['cdp_port'] = int(lines[0])
                    if 1 <= state['cdp_port'] <= 65535:
                        break
            except (FileNotFoundError, PermissionError, ValueError):
                pass
            time.sleep(.2)
        endpoint = owned_endpoint(state)
        with build_opener(ProxyHandler({})).open(endpoint + '/json/version', timeout=5) as response:
            state['browser_version'] = json.load(response)['Browser']
        while time.monotonic() < deadline:
            with build_opener(ProxyHandler({})).open(endpoint + '/json/list', timeout=5) as response:
                targets = [t for t in json.load(response) if t.get('type') == 'page' and t.get('url') == state['demo_url']]
            if len(targets) == 1:
                state['target_id'] = targets[0]['id']
                break
            time.sleep(.2)
        if 'target_id' not in state:
            raise RuntimeError('The dedicated browser did not open the expected demo page.')
        state.update(status='running', heartbeat=time.time(), input_desktop_at_start=h.input_desktop())
        save(state)
        event(state, 'ready', pid=state['pid'], desktop=state['desktop'])
        next_capture = 0
        while browser.poll() is None and not (session / 'stop').exists():
            assert_desktop(state)
            state['heartbeat'] = time.time()
            if time.monotonic() >= next_capture:
                try:
                    frame = capture_frame(state)
                    state['last_capture'] = frame['captured_at']
                    if 'capture_error' in state:
                        event(state, 'capture_recovered')
                    state.pop('capture_error', None)
                except Exception as error:
                    if state.get('capture_error') != str(error):
                        event(state, 'capture_error', error=str(error))
                    state['capture_error'] = str(error)
                next_capture = time.monotonic() + state['capture_interval']
            if running is not None and running['process'].poll() is not None:
                running['stdout'].close()
                running['stderr'].close()
                try:
                    if running['process'].returncode != 0:
                        raise RuntimeError('REA failed: ' + running['error'].read_text(encoding='utf-8')[-3000:])
                    result = json.loads(running['output'].read_text(encoding='utf-8'))
                    reply(running['request'], True, result=result)
                    event(state, 'scenario_finished', request=running['request'].stem, exit_code=0)
                except Exception as error:
                    reply(running['request'], False, error=str(error))
                    event(state, 'scenario_failed', error=str(error))
                running = None
                state['busy'] = False
            if running is not None and time.monotonic() > running['deadline']:
                raise TimeoutError('REA exceeded its session deadline; stopping the entire owned job.')
            if running is None:
                requests = sorted((session / 'requests').glob('*.request.json'))
                if requests:
                    path = requests[0]
                    try:
                        command = h.read_json(path)
                        request = prepare_scenario(command, state)
                        input_path = path.with_name(path.stem + '.rea.json')
                        write_json(input_path, request)
                        output = path.with_name(path.stem + '.stdout.json')
                        error_path = path.with_name(path.stem + '.stderr.txt')
                        out = output.open('w', encoding='utf-8')
                        err = error_path.open('w', encoding='utf-8')
                        process = subprocess.Popen([state['node'], state['rea_cli'], 'capture-browser-scenario',
                                                    str(input_path), '--format', 'json'], cwd=session, stdout=out, stderr=err,
                                                   creationflags=h.HIDDEN, close_fds=True)
                        running = {'process': process, 'stdout': out, 'stderr': err, 'output': output,
                                   'error': error_path, 'request': path, 'deadline': time.monotonic() + 180}
                        state['busy'] = True
                        event(state, 'scenario_started', request=path.stem, pid=process.pid)
                    except Exception as error:
                        reply(path, False, error=str(error))
            save(state)
            time.sleep(.1)
    except Exception as error:
        state['error'] = str(error)
        event(state, 'worker_error', error=str(error))
        save(state)
        raise
    finally:
        if running:
            running['stdout'].close()
            running['stderr'].close()
        demo.shutdown()
        demo.server_close()
        browser.close()


def keeper(session):
    import win32job
    state = state_at(session)
    desktop = job = process = thread = None
    active = -1
    worker_alive = False
    try:
        desktop = h.win32service.CreateDesktop(state['desktop'], 0,
            h.win32con.DESKTOP_CREATEWINDOW | h.win32con.DESKTOP_READOBJECTS |
            h.win32con.DESKTOP_WRITEOBJECTS | h.win32con.DESKTOP_ENUMERATE, None)
        job = win32job.CreateJobObject(None, '')
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits['BasicLimitInformation']['LimitFlags'] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        if h.input_desktop() == state['desktop']:
            raise RuntimeError('Refusing to run on the input desktop.')
        startup = h.win32process.STARTUPINFO()
        startup.lpDesktop = 'WinSta0\\' + state['desktop']
        args = [sys.executable, '-B', str(Path(__file__).resolve()), '_worker', '--session', str(session)]
        process, thread, pid, _ = h.win32process.CreateProcess(sys.executable, subprocess.list2cmdline(args),
            None, None, False, h.win32con.CREATE_NO_WINDOW | h.win32con.CREATE_SUSPENDED,
            None, str(session), startup)
        win32job.AssignProcessToJobObject(job, process)
        h.win32process.ResumeThread(thread)
        started = time.monotonic()
        while h.win32process.GetExitCodeProcess(process) == h.win32con.STILL_ACTIVE:
            if (session / 'stop').exists():
                break
            state = state_at(session)
            if h.input_desktop() == state['desktop']:
                state['error'] = 'The owned desktop became the input desktop; session terminated.'
                break
            if time.monotonic() - started > 50 and time.time() - state.get('heartbeat', 0) > 25:
                state['error'] = 'Worker heartbeat expired; session terminated.'
                break
            time.sleep(.1)
    except Exception as error:
        state['error'] = str(error)
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
                if active == 0 and not worker_alive:
                    break
                time.sleep(.1)
        except Exception as error:
            state['error'] = 'Cleanup could not be confirmed: ' + str(error)
            active = -1
        finally:
            if thread:
                thread.Close()
            if process:
                process.Close()
            if job:
                job.Close()
            if desktop:
                desktop.CloseDesktop()
        # The job owns descendants even if a browser or worker exits first.
        latest = state_at(session)
        latest.update({k: v for k, v in state.items() if k == 'error'})
        stopped = active == 0 and not worker_alive
        latest.update(status='stopped' if stopped else 'cleanup_failed', stopped_at=time.time(),
                      cleanup={'active_processes': active, 'job_terminated': stopped})
        save(latest)
        event(latest, 'stopped', active_processes=active)


def start(args):
    browser = Path(args.browser).resolve()
    node = Path(args.node or shutil.which('node') or '').resolve()
    rea = Path(args.rea_cli).resolve()
    if not browser.is_file() or browser.name.lower() not in ('chrome.exe', 'msedge.exe', 'chromium.exe'):
        raise ValueError('Supply an installed Chrome, Edge, or Chromium executable.')
    if not node.is_file() or not rea.is_file():
        raise ValueError('Node.js and the REA CLI must exist before starting.')
    if not .5 <= args.capture_interval <= 30:
        raise ValueError('Capture interval must be between 0.5 and 30 seconds.')
    base = Path(args.work_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(base).free < 1_000_000_000:
        raise RuntimeError('The session drive needs at least 1 GB free.')
    session = base / ('browser-' + uuid.uuid4().hex[:12])
    session.mkdir()
    for name in ('profile', 'captures', 'requests', 'temp'):
        (session / name).mkdir()
    state = {'schema': SCHEMA, 'session': str(session), 'desktop': 'StealthREA_' + uuid.uuid4().hex[:16],
             'browser': str(browser), 'node': str(node), 'rea_cli': str(rea), 'status': 'starting',
             'capture_interval': args.capture_interval, 'created_at': time.time()}
    save(state)
    env = os.environ.copy()
    env.update(TEMP=str(session / 'temp'), TMP=str(session / 'temp'), PYTHONDONTWRITEBYTECODE='1')
    with (session / 'keeper.log').open('w', encoding='utf-8') as log:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '_keeper', '--session', str(session)],
                                stdout=log, stderr=log, env=env, creationflags=h.HIDDEN, close_fds=True)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        state = state_at(session)
        if state['status'] == 'running':
            return state
        if state['status'] in ('stopped', 'cleanup_failed') or proc.poll() is not None:
            raise RuntimeError('Browser startup failed: ' + state.get('error', str(session / 'keeper.log')))
        time.sleep(.2)
    (session / 'stop').touch()
    raise TimeoutError('Browser startup timed out; stop requested. Inspect ' + str(session))


def wait_stopped(session):
    (session / 'stop').touch()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = state_at(session)
        if state['status'] == 'stopped':
            return state
        time.sleep(.1)
    raise RuntimeError('Cleanup is not confirmed. Inspect the session state and keeper log.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    sub = commands.add_parser('start')
    sub.add_argument('--browser', required=True)
    sub.add_argument('--rea-cli', required=True)
    sub.add_argument('--node')
    sub.add_argument('--work-dir', required=True)
    sub.add_argument('--capture-interval', type=float, default=1)
    for name in ('status', 'scenario', 'stop', '_worker', '_keeper'):
        sub = commands.add_parser(name)
        sub.add_argument('--session', required=True)
        if name == 'scenario':
            sub.add_argument('--file', required=True)
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('The REA desktop adapter requires Windows.')
    missing = h.dependencies(False)
    if missing:
        raise RuntimeError('Install the existing harness dependencies first: ' + ', '.join(missing))
    h.native_modules()
    if args.command == 'start':
        return start(args)
    session = Path(args.session).resolve()
    state = state_at(session)
    if args.command == '_keeper':
        return keeper(session)
    if args.command == '_worker':
        with (session / 'worker.log').open('a', encoding='utf-8', buffering=1) as log:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                try:
                    return worker(session)
                except Exception:
                    traceback.print_exc()
                    raise
    if args.command == 'stop':
        return wait_stopped(session)
    if args.command == 'status':
        return state
    if state['status'] != 'running':
        raise RuntimeError('The browser session is not running.')
    verify_identity(state['worker_pid'], state['worker_created'], sys.executable)
    if h.input_desktop() == state['desktop']:
        raise RuntimeError('Refusing to send a scenario to the active input desktop.')
    source = h.read_json(args.file)
    if not isinstance(source, dict) or 'browser' in source:
        raise ValueError('The adapter owns browser selection; omit browser from the scenario.')
    path = session / 'requests' / (uuid.uuid4().hex + '.request.json')
    write_json(path, source)
    response = path.with_name(path.name.replace('.request.json', '.response.json'))
    deadline = time.monotonic() + 190
    while time.monotonic() < deadline:
        if response.exists():
            result = h.read_json(response)
            if not result['ok']:
                raise RuntimeError(result['error'])
            return result['result']
        if state_at(session)['status'] != 'running':
            raise RuntimeError('Session ended before the scenario completed; inspect its events and logs.')
        time.sleep(.2)
    raise TimeoutError('Scenario outcome unknown; inspect before retrying a mutation.')


if __name__ == '__main__':
    try:
        result = main()
        if result is not None:
            print(json.dumps({'ok': True, 'result': result}, indent=2))
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        sys.exit(1)
