"""Run the included console and Qt examples on a private desktop, then stop them."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent


def cli(*args, succeeds=True):
    result = subprocess.run([sys.executable, str(ROOT / 'harness.py'), *map(str, args)], capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=120,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    data = json.loads(result.stdout)
    if succeeds and (result.returncode or not data['ok']):
        raise RuntimeError(data.get('error', result.stderr))
    if not succeeds and data['ok']:
        raise AssertionError('An invalid operation unexpectedly succeeded.')
    return data.get('result') if succeeds else data['error']


def eventually(operation, predicate, seconds=45):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            last = operation()
            if predicate(last):
                return last
        except Exception as error:
            last = str(error).split('\n', 1)[0]
        time.sleep(.5)
    raise AssertionError('Expected state did not appear: ' + str(last))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', help='Use an existing harness session containing the smoke project; it will be stopped.')
    args = parser.parse_args()
    if importlib.util.find_spec('PyQt6') is None:
        raise RuntimeError('The GUI example needs PyQt6 in the selected Python. This is a project dependency.')
    source = ROOT / 'examples/smoke_project'
    original = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.glob('*.py')}
    if args.session:
        state = cli('status', '--session', args.session)
    else:
        state = cli('start', '--project', source, '--run', 'Console example=console_demo.py', '--run', 'GUI example=gui_demo.py')
    session = Path(state['session'])
    if Path(state['source_project']).resolve() != source.resolve():
        raise ValueError('The supplied session does not belong to the included smoke project.')
    checks = {}
    result = {'session': str(session), 'checks': checks, 'passed': False}
    def action(command, *rest, succeeds=True):
        return cli(command, '--session', session, *rest, succeeds=succeeds)
    def ide(command, *rest, succeeds=True):
        return action('ide', '--action', command, *rest, succeeds=succeeds)
    try:
        status = action('status')
        assert status['desktop'] != status['input_desktop']
        checks['separate_desktop'] = True
        assert str(session / 'project').replace('\\', '/') in ide('roots').replace('\\', '/')
        checks['correct_project_root'] = True
        ide('open', '--path', 'console_demo.py')
        ide('select', '--name', 'Console example')
        ide('run')
        eventually(lambda: ide('content', '--name', 'Console example'), lambda text: 'Enter a value:' in text)
        ide('input', '--text', 'sample')
        output = eventually(lambda: ide('content', '--name', 'Console example'), lambda text: 'Result: sample!' in text and 'Exit code: 0' in text)
        checks['console_input_and_exit_zero'] = True
        (session / 'console-output.txt').write_text(output, encoding='utf-8')
        ide('layout')
        ide_window = max((w for w in action('windows') if w['kind'] == 'ide'), key=lambda w: (w['rect'][2]-w['rect'][0])*(w['rect'][3]-w['rect'][1]))
        console_capture = action('capture', '--hwnd', ide_window['hwnd'], '--output', session / 'captures/console.png')
        ide('select', '--name', 'GUI example')
        ide('run')
        apps = eventually(lambda: action('windows'), lambda items: any(w['title'] == 'Harness example' and w['kind'] == 'application' for w in items))
        app = next(w for w in apps if w['title'] == 'Harness example')
        controls = action('controls', '--hwnd', app['hwnd'])
        field = next(c for c in controls if c['class'] == 'QLineEdit' and c['name'] == 'Value')
        action('value', '--hwnd', app['hwnd'], '--index', field['index'], '--label', 'Value', '--text', 'sample')
        controls = action('controls', '--hwnd', app['hwnd'])
        button = next(c for c in controls if c['class'] == 'QPushButton' and c['name'] == 'Apply')
        action('invoke', '--hwnd', app['hwnd'], '--index', button['index'], '--label', 'Apply')
        eventually(lambda: action('controls', '--hwnd', app['hwnd']), lambda items: any(c['name'] == 'Result: sample!' for c in items))
        checks['gui_value_invoke_and_result'] = True
        gui_capture = action('capture', '--hwnd', app['hwnd'], '--output', session / 'captures/gui.png')
        for capture in (console_capture, gui_capture):
            assert hashlib.sha256(Path(capture['path']).read_bytes()).hexdigest() == capture['sha256']
        checks['native_captures_hash_verified'] = True
        assert 'not owned' in action('capture', '--hwnd', 0, '--output', session / 'captures/rejected.png', succeeds=False)
        checks['unowned_window_rejected'] = True
        assert 'outside' in ide('open', '--path', ROOT / 'harness.py', succeeds=False)
        checks['outside_project_file_rejected'] = True
        sidecar = session / 'captures/preserved.capture.json'
        sidecar.write_text('preserve this file', encoding='utf-8')
        action('capture', '--hwnd', app['hwnd'], '--output', sidecar.with_name('preserved.png'), succeeds=False)
        assert sidecar.read_text(encoding='utf-8') == 'preserve this file'
        checks['existing_sidecar_preserved'] = True
        wrong = subprocess.run([str(Path(state['installation']) / 'jbr/bin/java.exe'), '--add-modules', 'jdk.attach',
                                '-cp', str(session / 'java'), 'HarnessAttach', str(state['pid']), str(session / 'wrong-config'),
                                str(session / 'java/harness-agent.jar'), str(session / 'unused.properties')],
                               capture_output=True, text=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        assert wrong.returncode != 0 and 'Unexpected IDE profile' in wrong.stderr
        checks['wrong_ide_profile_rejected'] = True
        spec = importlib.util.spec_from_file_location('harness_under_test', ROOT / 'harness.py')
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        harness.dependencies()
        harness.native_modules()
        bad = {**state, 'process_created': state['process_created'] + 1}
        try:
            harness.owned_process(bad)
            raise AssertionError('Reused process guard did not reject changed creation time.')
        except RuntimeError as error:
            assert 'reused' in str(error)
        checks['process_creation_time_checked'] = True
        try:
            harness.assert_isolation(state)
            raise AssertionError('An action on the user desktop was permitted.')
        except RuntimeError as error:
            assert 'Isolation' in str(error)
        checks['active_desktop_action_rejected'] = True
        assert original == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.glob('*.py')}
        checks['source_project_unchanged'] = True
        result['captures'] = [console_capture, gui_capture]
        result['passed'] = True
    finally:
        result['shutdown'] = action('stop')
        checks['owned_processes_stopped'] = result['shutdown']['stopped']
        (session / 'smoke-test.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
