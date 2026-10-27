"""Exercise the real REA/private-desktop/viewer path and its cleanup boundaries."""

import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from unittest.mock import patch
import uuid

import rea_bridge as bridge
import harness as h

ROOT = Path(__file__).resolve().parent


def call(*args, success=True):
    process = subprocess.run([sys.executable, '-B', str(ROOT / 'rea_bridge.py'), *map(str, args)],
                             capture_output=True, text=True, timeout=210, creationflags=h.HIDDEN)
    result = json.loads(process.stdout)
    if success:
        assert process.returncode == 0 and result['ok'], result
        return result['result']
    assert process.returncode != 0 and not result['ok'], result
    return result


def wait_for(fn, seconds=15):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            value = fn()
            if value:
                return value
        except (OSError, ValueError, KeyError) as error:
            last = error
        time.sleep(.1)
    raise AssertionError(f'Timed out waiting for evidence: {last}')


def process_gone(pid, created):
    try:
        process = h.psutil.Process(pid)
        if abs(process.create_time() - created) > .01:
            return True
        try:
            process.wait(timeout=.1)
            return True
        except h.psutil.TimeoutExpired:
            return False
    except h.psutil.NoSuchProcess:
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', required=True)
    parser.add_argument('--rea-cli', required=True)
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    h.dependencies(False)
    h.native_modules()
    args.output.mkdir(parents=True, exist_ok=True)
    active_before = h.input_desktop()
    checks = {}
    sessions = []
    viewer = None
    opener = build_opener(ProxyHandler({}))
    def start():
        state = call('start', '--browser', args.browser, '--rea-cli', args.rea_cli,
                     '--work-dir', args.work_dir)
        sessions.append(Path(state['session']))
        return state
    try:
        state = start()
        session = Path(state['session'])
        assert state['desktop'] != active_before and state['input_desktop_at_start'] == active_before
        checks['private_desktop'] = True
        frame = wait_for(lambda: h.read_json(session / 'captures/latest.json'))
        assert frame['desktop'] == state['desktop'] and frame['input_desktop'] == active_before
        assert frame['pid'] == state['pid']
        checks['owned_window_capture'] = True

        invalid = session / 'invalid.json'
        bridge.write_json(invalid, {'browser': {'mode': 'launch'}, 'actions': []})
        rejected = call('scenario', '--session', session, '--file', invalid, success=False)
        assert 'omit browser' in rejected['error']
        checks['caller_browser_override_rejected'] = True
        tampered = {**state, 'cdp_port': int(state['demo_url'].split(':')[-1].strip('/'))}
        try:
            bridge.owned_endpoint(tampered)
        except RuntimeError as error:
            assert 'owned loopback' in str(error)
        else:
            raise AssertionError('Unowned listener accepted')
        checks['unowned_endpoint_rejected'] = True
        try:
            bridge.assert_desktop(state)
        except RuntimeError as error:
            assert 'isolation' in str(error)
        else:
            raise AssertionError('Action was allowed from the input desktop')
        checks['input_desktop_action_rejected'] = True

        viewer = subprocess.Popen([sys.executable, '-B', str(ROOT / 'observe.py'), '--session', str(session)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  creationflags=h.HIDDEN)
        url = json.loads(viewer.stdout.readline())['url']
        def get(suffix):
            with opener.open(url + suffix, timeout=5) as response:
                return response.read()
        assert b'Hidden desktop observer' in get('')
        def observer_frame():
            meta = json.loads(get('/meta'))
            if meta.get('available') and not meta['stale']:
                try:
                    pixels = get('/frame?sha256=' + meta['sha256'])
                except HTTPError as error:
                    if error.code == 409:
                        return None
                    raise
                assert hashlib.sha256(pixels).hexdigest() == meta['sha256']
                return meta, pixels
        before, _ = wait_for(observer_frame)
        assert h.psutil.Process(state['pid']).is_running()
        checks['viewer_serves_verified_live_frame_without_killing_browser'] = True
        request = Request(url + '/meta', headers={'Host': 'other.invalid'})
        try:
            opener.open(request, timeout=5)
        except HTTPError as error:
            assert error.code == 403
        else:
            raise AssertionError('Untrusted viewer Host accepted')
        checks['viewer_host_rejected'] = True

        evidence = call('scenario', '--session', session, '--file', ROOT.parent / 'examples/browser_scenario.json')
        result = evidence['normalized_result']
        last = result['steps'][-1]['artifacts']
        assert '3 × Notebook: $37.50</output>' in last['dom']['value']['text']
        assert last['accessibility']['state'] == 'captured'
        assert result['browser']['cleanup'] == 'disconnected-external'
        assert h.psutil.Process(state['pid']).is_running()
        page = base64.b64decode(last['screenshot']['value']['data_base64'])
        assert hashlib.sha256(page).hexdigest() == last['screenshot']['value']['sha256']
        (args.output / 'edge-page.png').write_bytes(page)
        checks['rea_fill_click_and_expected_result'] = True
        checks['rea_capture_hash_and_disconnect'] = True
        def changed():
            pair = observer_frame()
            return pair if pair and pair[0]['sha256'] != before['sha256'] else None
        after, pixels = wait_for(changed)
        (args.output / 'edge-native.png').write_bytes(pixels)
        checks['viewer_updates_after_action'] = True
        viewer_state = start()
        viewer_scenario = session / 'view-observer.json'
        bridge.write_json(viewer_scenario, {
            'start_url': {'url': url},
            'actions': [{'step_id': 'image-loaded', 'action': 'wait_for',
                         'locator': {'kind': 'css', 'selector': '#frame:not([hidden])'}, 'state': 'visible'}],
            'capture': {'at_end': ['dom', 'screenshot']}})
        view = call('scenario', '--session', viewer_state['session'], '--file', viewer_scenario)
        rendered = view['normalized_result']['steps'][-1]['artifacts']
        assert 'Capture is current.' in rendered['dom']['value']['text']
        (args.output / 'observer-browser.png').write_bytes(base64.b64decode(rendered['screenshot']['value']['data_base64']))
        checks['viewer_rendered_in_second_isolated_browser'] = True
        call('stop', '--session', viewer_state['session'])
        stopped = call('stop', '--session', session)
        assert stopped['cleanup'] == {'active_processes': 0, 'job_terminated': True}
        assert wait_for(lambda: process_gone(state['pid'], state['process_created']))
        assert wait_for(lambda: process_gone(state['worker_pid'], state['worker_created']))
        assert json.loads(get('/meta'))['status'] == 'stopped'
        assert not any(c.status == 'LISTEN' and c.laddr.port == state['cdp_port']
                       for c in h.psutil.net_connections(kind='tcp'))
        checks['normal_cleanup_and_listener_closed'] = True
        viewer.terminate()
        viewer.wait(timeout=5)
        viewer = None

        for victim in ('worker', 'browser'):
            state = start()
            session = Path(state['session'])
            pid = state['worker_pid'] if victim == 'worker' else state['pid']
            created = state['worker_created'] if victim == 'worker' else state['process_created']
            exe = sys.executable if victim == 'worker' else state['browser']
            owned = bridge.verify_identity(pid, created, exe)
            owned.terminate()
            stopped = wait_for(lambda: (s if (s := bridge.state_at(session))['status'] == 'stopped' else None))
            assert stopped['cleanup']['active_processes'] == 0
            assert wait_for(lambda: process_gone(state['pid'], state['process_created']))
            checks[victim + '_exit_cleans_entire_job'] = True

        import win32job
        session = args.work_dir.resolve() / ('assignment-failure-' + uuid.uuid4().hex[:10])
        session.mkdir()
        state = {'schema': bridge.SCHEMA, 'session': str(session), 'desktop': 'StealthREA_' + uuid.uuid4().hex,
                 'status': 'starting'}
        bridge.save(state)
        spawned = []
        real_create = h.win32process.CreateProcess
        def create(*a, **kw):
            values = real_create(*a, **kw)
            spawned.append((values[2], h.psutil.Process(values[2]).create_time()))
            return values
        with patch.object(h.win32process, 'CreateProcess', side_effect=create), \
             patch.object(win32job, 'AssignProcessToJobObject', side_effect=RuntimeError('injected assignment failure')):
            bridge.keeper(session)
        assert len(spawned) == 1 and wait_for(lambda: process_gone(*spawned[0]))
        assert bridge.state_at(session)['status'] == 'stopped'
        checks['job_assignment_failure_leaves_no_suspended_worker'] = True
        assert h.input_desktop() == active_before
        checks['input_desktop_preserved'] = True
        report = {'passed': True, 'checks': checks, 'browser_version': evidence['normalized_result']['browser']['version'],
                  'expected_result': '3 × Notebook: $37.50', 'rea_completeness': result['completeness'],
                  'capture_method': 'PrintWindow', 'capture_interval_seconds': 1, 'pycharm_core_modified': False}
        bridge.write_json(args.output / 'browser-test.json', report)
        print(json.dumps(report, indent=2))
    finally:
        if viewer:
            viewer.terminate()
            viewer.wait(timeout=5)
        for session in sessions:
            if bridge.state_at(session)['status'] != 'stopped':
                call('stop', '--session', session)


if __name__ == '__main__':
    main()
