"""Control a private PyCharm session on a separate Windows desktop."""

import argparse
import contextlib
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parent
DEPS = {'win32api': 'pywin32==311', 'PIL': 'Pillow==13.0.0', 'comtypes': 'comtypes==1.4.16', 'psutil': 'psutil==5.9.8'}
HIDDEN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
DLL_HANDLES = []


def write_json(path, data):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(temp, path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def dependencies(install=False):
    from dependency_setup import activate
    activate()
    import site
    target = ROOT / '.runtime' / 'deps'
    if target.exists():
        site.addsitedir(str(target))
        for extra in ('win32', 'win32/lib', 'Pythonwin', 'pywin32_system32'):
            sys.path.append(str(target / extra))
        if (target / 'pywin32_system32').exists():
            DLL_HANDLES.append(os.add_dll_directory(str(target / 'pywin32_system32')))
    missing = [package for module, package in DEPS.items() if importlib.util.find_spec(module) is None]
    if missing and install:
        target.mkdir(parents=True, exist_ok=True)
        from dependency_setup import install as install_dependencies
        install_dependencies()
        return dependencies(False)
    return missing


def discover_pycharm(explicit=None):
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    elif os.environ.get('PYCHARM_HARNESS_IDE'):
        candidates.append(Path(os.environ['PYCHARM_HARNESS_IDE']))
    else:
        for parent in (Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'JetBrains',
                       Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs',
                       Path(os.environ.get('LOCALAPPDATA', '')) / 'JetBrains/Toolbox/apps'):
            if parent.exists():
                candidates.extend(sorted(parent.glob('PyCharm*'), reverse=True))
                candidates.extend(sorted(parent.glob('pycharm*/ch-*/*'), reverse=True))
    for candidate in candidates:
        if candidate.is_file():
            candidate = candidate.parent.parent
        if all((candidate / part).is_file() for part in ('bin/pycharm64.exe', 'jbr/bin/java.exe', 'jbr/bin/javac.exe')):
            return candidate.resolve()
    raise RuntimeError('PyCharm with its JBR was not found. Supply --pycharm with the installation folder.')


def native_modules():
    global win32api, win32con, win32gui, win32process, win32service, win32ui, psutil
    import win32api, win32con, win32gui, win32process, win32service, win32ui, psutil


def desktop_name(handle):
    size = ctypes.c_ulong()
    ctypes.windll.user32.GetUserObjectInformationW(int(handle), 2, None, 0, ctypes.byref(size))
    value = ctypes.create_unicode_buffer(max(256, size.value // 2))
    if not ctypes.windll.user32.GetUserObjectInformationW(int(handle), 2, value, ctypes.sizeof(value), ctypes.byref(size)):
        raise ctypes.WinError()
    return value.value


def input_desktop():
    handle = win32service.OpenInputDesktop(0, False, win32con.DESKTOP_READOBJECTS)
    try:
        return desktop_name(handle)
    finally:
        handle.CloseDesktop()


class NativeProcess:
    def __init__(self, args, desktop, cwd=None, env=None):
        startup = win32process.STARTUPINFO()
        startup.lpDesktop = 'WinSta0\\' + desktop
        self.process, self.thread, self.pid, _ = win32process.CreateProcess(
            str(args[0]), subprocess.list2cmdline(list(map(str, args))), None, None, False,
            win32con.CREATE_NO_WINDOW | win32con.CREATE_UNICODE_ENVIRONMENT,
            env, str(cwd) if cwd else None, startup)

    def poll(self):
        code = win32process.GetExitCodeProcess(self.process)
        return None if code == win32con.STILL_ACTIVE else code

    def close(self):
        win32api.CloseHandle(self.thread)
        win32api.CloseHandle(self.process)


def session_state(folder):
    folder = Path(folder).resolve()
    state = read_json(folder / 'session.json')
    if state.get('schema') != 1 or Path(state['session']).resolve() != folder:
        raise RuntimeError('Invalid session file.')
    if not state['desktop'].startswith('PyCharmHarness_'):
        raise RuntimeError('Unexpected desktop in session file.')
    return state


def owned_process(state):
    process = psutil.Process(state['pid'])
    if abs(process.create_time() - state['process_created']) > .01:
        raise RuntimeError('The session process number has been reused. Start a new session.')
    if Path(process.exe()).resolve() != Path(state['ide_executable']).resolve():
        raise RuntimeError('The process does not match this session.')
    return process


def assert_isolation(state):
    current = desktop_name(win32service.GetThreadDesktop(win32api.GetCurrentThreadId()))
    if current != state['desktop'] or input_desktop() == current:
        raise RuntimeError('Isolation check failed. No action was performed.')
    owned_process(state)


def windows(state):
    assert_isolation(state)
    process = owned_process(state)
    ids = {process.pid, *(p.pid for p in process.children(recursive=True))}
    result = []
    def visit(hwnd, _):
        tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in ids and win32gui.IsWindowVisible(hwnd):
            if desktop_name(win32service.GetThreadDesktop(tid)) != state['desktop']:
                raise RuntimeError('An owned window is on a different desktop.')
            result.append({'hwnd': hwnd, 'pid': pid, 'kind': 'ide' if pid == process.pid else 'application',
                           'title': win32gui.GetWindowText(hwnd), 'rect': win32gui.GetWindowRect(hwnd)})
        return True
    win32gui.EnumWindows(visit, None)
    return result


def target_window(state, hwnd):
    found = next((item for item in windows(state) if item['hwnd'] == int(hwnd)), None)
    if found is None:
        raise RuntimeError('That window is not owned by this session. Refresh windows.')
    return found


def ui_controls(session, hwnd):
    import comtypes.client
    gen = session / 'comtypes'
    gen.mkdir(exist_ok=True)
    comtypes.client.gen_dir = str(gen)
    lib = comtypes.client.GetModule(str(Path(os.environ['WINDIR']) / 'System32/UIAutomationCore.dll'))
    uia = comtypes.client.CreateObject(lib.CUIAutomation, interface=lib.IUIAutomation)
    elements = uia.ElementFromHandle(hwnd).FindAll(4, uia.CreateTrueCondition())
    return lib, [elements.GetElement(i) for i in range(elements.Length)]


def describe_control(lib, element, index):
    item = {'index': index, 'name': element.CurrentName, 'type': element.CurrentControlType,
            'class': element.CurrentClassName, 'automation_id': element.CurrentAutomationId}
    try:
        item['value'] = element.GetCurrentPattern(10002).QueryInterface(lib.IUIAutomationValuePattern).CurrentValue
    except Exception:
        pass
    return item


def capture(state, target, destination):
    from PIL import Image
    path = Path(destination).expanduser().resolve()
    if path.suffix.lower() != '.png' or path.exists() or path.with_suffix('.capture.json').exists():
        raise ValueError('Use a new PNG filename; existing captures are preserved.')
    path.parent.mkdir(parents=True, exist_ok=True)
    hwnd = target['hwnd']
    rect = win32gui.GetWindowRect(hwnd)
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    if width <= 0 or height <= 0 or win32gui.IsIconic(hwnd):
        raise RuntimeError('The window must have a visible, non-minimized surface.')
    dc = win32gui.GetWindowDC(hwnd)
    source = win32ui.CreateDCFromHandle(dc)
    surface = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(source, width, height)
        surface.SelectObject(bitmap)
        if not ctypes.windll.user32.PrintWindow(hwnd, surface.GetSafeHdc(), 2):
            raise RuntimeError('PrintWindow failed for this application.')
        pixels = Image.frombuffer('RGB', (width, height), bitmap.GetBitmapBits(True), 'raw', 'BGRX', 0, 1)
        pixels.save(path)
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        surface.DeleteDC()
        source.DeleteDC()
        win32gui.ReleaseDC(hwnd, dc)
    result = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
              'width': width, 'height': height, 'method': 'PrintWindow', 'pixel_edits': False,
              'desktop': state['desktop'], 'input_desktop': input_desktop(), **target}
    write_json(path.with_suffix('.capture.json'), result)
    return result


def build_java(ide, destination):
    destination.mkdir(parents=True, exist_ok=True)
    sources = sorted((ROOT / 'java').glob('*.java'))
    subprocess.run([str(ide / 'jbr/bin/javac.exe'), '--add-modules', 'jdk.attach', '-d', str(destination), *map(str, sources)],
                   check=True, capture_output=True, text=True, creationflags=HIDDEN)
    jar = destination / 'harness-agent.jar'
    with zipfile.ZipFile(jar, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('META-INF/MANIFEST.MF', 'Manifest-Version: 1.0\nAgent-Class: HarnessAgent\n\n')
        for file in destination.glob('HarnessAgent*.class'):
            archive.write(file, file.name)
    return jar


def property_value(value):
    return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')


def ide_command(state, command):
    assert_isolation(state)
    session = Path(state['session'])
    output = session / 'requests' / (command['id'] + '.ide.json')
    props = {'action': command['ide_action'], 'output': output.as_posix(), 'projectRoot': state['project'], 'sessionRoot': state['session']}
    for name in ('name', 'path', 'text', 'index', 'selected'):
        if command.get(name) is not None:
            props[name] = command[name]
    if command.get('label') is not None:
        props['expectedLabel'] = command['label']
    if props['action'] == 'open' and 'path' in props:
        requested = Path(props['path'])
        props['path'] = str(requested if requested.is_absolute() else Path(state['project']) / requested)
    request = output.with_suffix('.properties')
    request.write_text('\n'.join(k + '=' + property_value(v) for k, v in props.items()), encoding='utf-8')
    executable = Path(state['installation']) / 'jbr/bin/java.exe'
    result = subprocess.run([str(executable), '--add-modules', 'jdk.attach', '-cp', str(session / 'java'), 'HarnessAttach',
                             str(state['pid']), state['config'], str(session / 'java/harness-agent.jar'), str(request)],
                            capture_output=True, text=True, timeout=45, creationflags=HIDDEN)
    if result.returncode:
        raise RuntimeError('IDE bridge failed: ' + (result.stderr or result.stdout)[-4000:])
    if not output.is_file():
        raise RuntimeError('IDE bridge returned no result.')
    response = read_json(output)
    if not response.get('ok'):
        raise RuntimeError(response.get('error', response.get('result', 'IDE command failed.')))
    return response['result']


def perform(state, command):
    assert_isolation(state)
    action = command['action']
    if action == 'status':
        return {**state, 'alive': True, 'input_desktop': input_desktop(), 'windows': windows(state)}
    if action == 'windows':
        return windows(state)
    if action == 'ide':
        if command['ide_action'] in ('open', 'select', 'run', 'input', 'content', 'roots', 'layout'):
            for _ in range(3):
                tree = ide_command(state, {'id': uuid.uuid4().hex, 'ide_action': 'dump'})
                if ' | Trust Project | ' in tree:
                    ide_command(state, {'id': uuid.uuid4().hex, 'ide_action': 'trust-project'})
                elif ' | Meet the Islands Theme | ' in tree:
                    ide_command(state, {'id': uuid.uuid4().hex, 'ide_action': 'dismiss-tour'})
                elif 'DialogWrapperPeerImpl$MyDialog | ' in tree:
                    raise RuntimeError('An unrecognized IDE dialog needs inspection with ide dump.')
                else:
                    break
        return ide_command(state, command)
    target = target_window(state, command['hwnd'])
    hwnd = target['hwnd']
    if action == 'capture':
        return capture(state, target, command['output'])
    if action == 'click':
        x, y = command['x'], command['y']
        rect = target['rect']
        if not 0 <= x < rect[2] - rect[0] or not 0 <= y < rect[3] - rect[1]:
            raise ValueError('Coordinates are outside the window.')
        point = win32gui.ScreenToClient(hwnd, (rect[0] + x, rect[1] + y))
        position = win32api.MAKELONG(*point)
        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, position)
        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, position)
        return {'action': action, 'hwnd': hwnd, 'point': [x, y]}
    lib, elements = ui_controls(Path(state['session']), hwnd)
    if action == 'controls':
        return [describe_control(lib, el, i) for i, el in enumerate(elements)]
    index = command['index']
    if index is None or not 0 <= index < len(elements):
        raise ValueError('Invalid control index; refresh controls.')
    element = elements[index]
    info = describe_control(lib, element, index)
    if command.get('label') is not None and command['label'] != info['name']:
        raise RuntimeError('The control label changed. Refresh controls before acting.')
    if action == 'value':
        element.GetCurrentPattern(10002).QueryInterface(lib.IUIAutomationValuePattern).SetValue(command['text'])
    elif action == 'invoke':
        element.GetCurrentPattern(10000).QueryInterface(lib.IUIAutomationInvokePattern).Invoke()
    else:
        raise ValueError('Unknown action: ' + action)
    return {'action': action, 'target': info}


def stop_owned(state):
    process = owned_process(state)
    children = process.children(recursive=True)
    for item in windows(state):
        win32gui.PostMessage(item['hwnd'], win32con.WM_CLOSE, 0, 0)
    _, remaining = psutil.wait_procs([*children, process], timeout=5)
    for target in remaining:
        try:
            # psutil checks process creation time before terminating a saved Process.
            target.terminate()
        except psutil.NoSuchProcess:
            pass
    _, remaining = psutil.wait_procs(remaining, timeout=5)
    if remaining:
        raise RuntimeError('Some owned processes are still running.')
    return {'stopped': True, 'session': state['session']}


def worker(session):
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    state = session_state(session)
    active = input_desktop()
    if desktop_name(win32service.GetThreadDesktop(win32api.GetCurrentThreadId())) != state['desktop'] or active == state['desktop']:
        raise RuntimeError('Worker did not start on its isolated desktop.')
    env = os.environ.copy()
    for key in ('QT_QPA_PLATFORM', 'QT_STYLE_OVERRIDE', 'QT_SCALE_FACTOR', 'JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS'):
        env.pop(key, None)
    env.update(PYCHARM_JDK=str(Path(state['installation']) / 'jbr'),
               PYCHARM_VM_OPTIONS=str(session / 'pycharm.vmoptions'), PYCHARM_PROPERTIES=str(session / 'idea.properties'))
    proc = NativeProcess([state['ide_executable'], state['project']], state['desktop'], cwd=Path(state['project']), env=env)
    state.update(pid=proc.pid, process_created=psutil.Process(proc.pid).create_time(), input_desktop_at_start=active,
                 worker_pid=os.getpid(), worker_created=psutil.Process().create_time(), status='running')
    write_json(session / 'session.json', state)
    try:
        while proc.poll() is None:
            pending = sorted((session / 'requests').glob('*.request.json'))
            if not pending:
                time.sleep(.1)
                continue
            for path in pending:
                command = read_json(path)
                response_path = path.with_name(command['id'] + '.response.json')
                try:
                    if command['action'] == 'stop':
                        assert_isolation(state)
                        result = stop_owned(state)
                    else:
                        result = perform(state, command)
                    response = {'ok': True, 'result': result}
                except Exception as error:
                    response = {'ok': False, 'error': str(error)}
                write_json(response_path, response)
                path.unlink()
                if command['action'] == 'stop' and response['ok']:
                    return
    finally:
        if proc.poll() is None:
            with contextlib.suppress(Exception):
                stop_owned(state)
        state['status'] = 'stopped' if proc.poll() is not None else 'cleanup_failed'
        write_json(session / 'session.json', state)
        proc.close()


def keeper(session):
    state = session_state(session)
    desktop = win32service.CreateDesktop(state['desktop'], 0, win32con.DESKTOP_CREATEWINDOW | win32con.DESKTOP_READOBJECTS | win32con.DESKTOP_WRITEOBJECTS | win32con.DESKTOP_ENUMERATE, None)
    proc = None
    try:
        if input_desktop() == state['desktop']:
            raise RuntimeError('The new desktop unexpectedly owns the user input.')
        proc = NativeProcess([sys.executable, str(Path(__file__).resolve()), '_worker', '--session', str(session)], state['desktop'])
        while proc.poll() is None:
            time.sleep(.3)
    finally:
        if proc:
            proc.close()
        desktop.CloseDesktop()


def xml_file(path, root):
    ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)


def configure_project(project, config, interpreter, runs):
    probe = 'import json,sys,sysconfig,site;print(json.dumps({"version":sys.version.split()[0],"paths":list(dict.fromkeys([sysconfig.get_path(k) for k in ("stdlib","platstdlib","purelib","platlib")]+site.getsitepackages()+([site.getusersitepackages()] if site.ENABLE_USER_SITE else [])))}))'
    details = json.loads(subprocess.check_output([str(interpreter), '-c', probe], text=True, creationflags=HIDDEN))
    options = config / 'options'
    options.mkdir(parents=True)
    application = ET.Element('application')
    sdk = ET.SubElement(ET.SubElement(application, 'component', name='ProjectJdkTable'), 'jdk', version='2')
    for name, value in [('name', 'Harness Python'), ('type', 'Python SDK'), ('version', 'Python ' + details['version']), ('homePath', interpreter.as_posix())]:
        ET.SubElement(sdk, name, value=value)
    roots = ET.SubElement(sdk, 'roots')
    classes = ET.SubElement(ET.SubElement(roots, 'classPath'), 'root', type='composite')
    for item in details['paths']:
        if item and Path(item).exists():
            ET.SubElement(classes, 'root', url=Path(item).resolve().as_uri(), type='simple')
    ET.SubElement(ET.SubElement(roots, 'sourcePath'), 'root', type='composite')
    ET.SubElement(sdk, 'additional')
    xml_file(options / 'jdk.table.xml', application)
    meta = project / '.idea'
    meta.mkdir(exist_ok=True)
    (meta / 'runConfigurations').mkdir(exist_ok=True)
    root = ET.Element('project', version='4')
    ET.SubElement(root, 'component', name='ProjectRootManager', version='2', **{'project-jdk-name': 'Harness Python', 'project-jdk-type': 'Python SDK'})
    xml_file(meta / 'misc.xml', root)
    root = ET.Element('project', version='4')
    modules = ET.SubElement(ET.SubElement(root, 'component', name='ProjectModuleManager'), 'modules')
    ET.SubElement(modules, 'module', fileurl='file://$PROJECT_DIR$/.idea/harness.iml', filepath='$PROJECT_DIR$/.idea/harness.iml')
    xml_file(meta / 'modules.xml', root)
    module = ET.Element('module', type='PYTHON_MODULE', version='4')
    manager = ET.SubElement(module, 'component', name='NewModuleRootManager')
    ET.SubElement(manager, 'content', url=project.as_uri())
    ET.SubElement(manager, 'orderEntry', type='inheritedJdk')
    ET.SubElement(manager, 'orderEntry', type='sourceFolder', forTests='false')
    xml_file(meta / 'harness.iml', module)
    for index, spec in enumerate(runs):
        name, relative = spec.split('=', 1)
        script = (project / relative).resolve(strict=True)
        if not name.strip() or not script.is_relative_to(project) or not script.is_file():
            raise ValueError('Run entries must name a file inside the project copy.')
        root = ET.Element('component', name='ProjectRunConfigurationManager')
        run = ET.SubElement(root, 'configuration', default='false', name=name, type='PythonConfigurationType', factoryName='Python')
        ET.SubElement(run, 'module', name='harness')
        for key, value in {'SDK_HOME': interpreter.as_posix(), 'WORKING_DIRECTORY': script.parent.as_posix(), 'IS_MODULE_SDK': 'false',
                           'ADD_CONTENT_ROOTS': 'true', 'ADD_SOURCE_ROOTS': 'true', 'SCRIPT_NAME': script.as_posix(), 'PARAMETERS': '',
                           'SHOW_COMMAND_LINE': 'false', 'EMULATE_TERMINAL': 'false', 'MODULE_MODE': 'false', 'REDIRECT_INPUT': 'false', 'PARENT_ENVS': 'true'}.items():
            ET.SubElement(run, 'option', name=key, value=value)
        envs = ET.SubElement(run, 'envs')
        ET.SubElement(envs, 'env', name='PYTHONUNBUFFERED', value='1')
        ET.SubElement(run, 'method', v='2')
        xml_file(meta / 'runConfigurations' / (str(index) + '.xml'), root)


def prepare_session(state):
    def ide(action):
        return request(argparse.Namespace(command='ide', session=state['session'], action=action))
    deadline = time.monotonic() + 40
    last_error = None
    while time.monotonic() < deadline:
        try:
            tree = ide('dump')
            if ' | Trust Project | ' in tree:
                ide('trust-project')
            elif ' | Meet the Islands Theme | ' in tree:
                ide('dismiss-tour')
            elif 'DialogWrapperPeerImpl$MyDialog | ' in tree:
                return {**state, 'project_ready': False, 'attention': 'An unrecognized IDE dialog needs inspection with ide dump.'}
            elif state['project'].replace('\\', '/') in ide('roots').replace('\\', '/'):
                # Allow deferred first-run dialogs to appear before returning ready.
                time.sleep(1)
                tree = ide('dump')
                if 'DialogWrapperPeerImpl$MyDialog | ' not in tree:
                    return {**state, 'project_ready': True}
            time.sleep(.5)
        except Exception as error:
            last_error = str(error).split('\n', 1)[0]
            time.sleep(.5)
    return {**state, 'project_ready': False, 'attention': last_error or 'The IDE is still starting. Inspect status and ide dump.'}


def start(args):
    ide = discover_pycharm(args.pycharm)
    source = Path(args.project).expanduser().resolve(strict=True)
    if not source.is_dir():
        raise ValueError('--project must be a directory.')
    interpreter = Path(args.python or sys.executable).resolve(strict=True)
    local = Path(os.environ['LOCALAPPDATA']) / 'PyCharmHarness'
    default = local if shutil.disk_usage(local.parent).free > 2_000_000_000 else Path('D:/PyCharmHarness') / os.environ.get('USERNAME', 'user') if Path('D:/').is_dir() and shutil.disk_usage('D:/').free > 2_000_000_000 else local
    base = Path(args.work_dir).expanduser().resolve() if args.work_dir else default
    if base.is_relative_to(source):
        raise ValueError('The work folder must be outside the source project.')
    base.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(base).free < 1_000_000_000:
        raise RuntimeError('Choose --work-dir on a drive with at least 1 GB free for IDE caches.')
    session = base / ('session-' + uuid.uuid4().hex[:12])
    session.mkdir()
    import win32security, ntsecuritycon
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    acl = win32security.ACL()
    flags = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
    acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, flags, ntsecuritycon.FILE_ALL_ACCESS, sid)
    win32security.SetNamedSecurityInfo(str(session), win32security.SE_FILE_OBJECT,
                                     win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                                     None, None, acl, None)
    project = session / 'project'
    excluded = {'.git', '.idea', '.venv', 'venv', '__pycache__', 'node_modules', '.runtime'}
    def ignore(folder, names):
        return [name for name in names if name in excluded or (Path(folder) / name).is_symlink() or (hasattr(Path, 'is_junction') and (Path(folder) / name).is_junction())]
    shutil.copytree(source, project, ignore=ignore)
    for name in ('config', 'system', 'plugins', 'logs', 'home', 'requests', 'captures'):
        (session / name).mkdir()
    properties = {f'idea.{key}.path': (session / folder).as_posix() for key, folder in [('config', 'config'), ('system', 'system'), ('plugins', 'plugins'), ('log', 'logs')]}
    (session / 'idea.properties').write_text('\n'.join(k + '=' + v for k, v in properties.items()) + '\n')
    vm = (ide / 'bin/pycharm64.exe.vmoptions').read_text()
    vm = '\n'.join(line for line in vm.splitlines() if not line.startswith('-Xmx'))
    vm += '\n-Xmx1024m\n' + '\n'.join('-D' + k + '=' + v for k, v in properties.items())
    vm += '\n-Duser.home=' + (session / 'home').as_posix() + '\n'
    (session / 'pycharm.vmoptions').write_text(vm)
    configure_project(project, session / 'config', interpreter, args.run)
    build_java(ide, session / 'java')
    state = {'schema': 1, 'session': str(session), 'source_project': str(source), 'project': str(project),
             'installation': str(ide), 'ide_executable': str(ide / 'bin/pycharm64.exe'), 'config': str(session / 'config'),
             'python': str(interpreter), 'desktop': 'PyCharmHarness_' + uuid.uuid4().hex[:12], 'status': 'starting'}
    write_json(session / 'session.json', state)
    env = os.environ.copy()
    if args.env_file:
        extra = read_json(args.env_file)
        if not isinstance(extra, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in extra.items()):
            raise ValueError('--env-file must contain a JSON object of string environment variables.')
        env.update(extra)
    for spec in args.env:
        key, value = spec.split('=', 1)
        if not key or '=' in key or '\x00' in key + value:
            raise ValueError('Invalid environment variable.')
        env[key] = value
    with (session / 'worker.log').open('w', encoding='utf-8') as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '_keeper', '--session', str(session)],
                         env=env, stdout=log, stderr=log, creationflags=HIDDEN, close_fds=True)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        state = session_state(session)
        if state['status'] == 'running':
            return state if args.no_prepare else prepare_session(state)
        if state['status'] in ('failed', 'stopped'):
            break
        time.sleep(.2)
    raise RuntimeError('Startup did not complete. Inspect ' + str(session / 'worker.log'))


def request(args):
    state = session_state(args.session)
    if state['status'] != 'running':
        if args.command == 'status':
            return {**state, 'alive': False}
        raise RuntimeError('The session is stopped. Start a fresh session.')
    owned_process(state)
    command = {key: value for key, value in vars(args).items() if value is not None}
    command.update(id=uuid.uuid4().hex, action=args.command)
    if args.command == 'ide':
        command['ide_action'] = args.action
    path = Path(state['session']) / 'requests' / (command['id'] + '.request.json')
    if args.command == 'capture':
        command['output'] = str(Path(args.output).expanduser().resolve())
    write_json(path, command)
    response = path.with_name(command['id'] + '.response.json')
    deadline = time.monotonic() + 55
    while time.monotonic() < deadline:
        if response.exists():
            data = read_json(response)
            if not data['ok']:
                raise RuntimeError(data['error'])
            return data['result']
        time.sleep(.1)
    raise TimeoutError('Command timed out. Inspect the session worker.log before retrying a mutation.')


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='command', required=True)
    for name in ('doctor', 'setup'):
        commands.add_parser(name).add_argument('--pycharm')
    start_parser = commands.add_parser('start')
    start_parser.add_argument('--project', required=True)
    start_parser.add_argument('--no-prepare', action='store_true', help='Leave known first-run dialogs for explicit IDE commands.')
    for option in ('python', 'pycharm', 'work-dir', 'env-file'):
        start_parser.add_argument('--' + option)
    for option in ('run', 'env'):
        start_parser.add_argument('--' + option, action='append', default=[])
    for name in ('status', 'windows', 'controls', 'value', 'invoke', 'click', 'capture', 'ide', 'stop', '_keeper', '_worker'):
        sub = commands.add_parser(name)
        sub.add_argument('--session', required=True)
        if name in ('controls', 'value', 'invoke', 'click', 'capture'):
            sub.add_argument('--hwnd', type=int, required=True)
        if name in ('value', 'invoke', 'ide'):
            sub.add_argument('--index', type=int, required=name in ('value', 'invoke'))
            sub.add_argument('--label')
        if name in ('value', 'ide'):
            sub.add_argument('--text', required=name == 'value')
        if name == 'click':
            sub.add_argument('--x', type=int, required=True)
            sub.add_argument('--y', type=int, required=True)
        if name == 'capture':
            sub.add_argument('--output', required=True)
        if name == 'ide':
            sub.add_argument('--action', required=True)
            sub.add_argument('--name')
            sub.add_argument('--path')
            sub.add_argument('--selected', choices=('true', 'false'))
    return p


def inspect_environment(pycharm=None, *, install=False):
    """Report independent prerequisites without launching an app or assuming IDE presence."""
    from dependency_setup import validate_runtime
    checks = {}
    try:
        runtime = validate_runtime()
        checks['runtime'] = {'ready': True, 'identity': runtime}
    except (RuntimeError, OSError, ValueError) as exc:
        checks['runtime'] = {'ready': False, 'error': str(exc)}
    try:
        missing = dependencies(install=install and checks['runtime']['ready'])
        checks['dependencies'] = {'ready': not missing, 'missing_packages': missing}
    except Exception as exc:
        missing = []
        checks['dependencies'] = {'ready': False, 'error': str(exc), 'missing_packages': missing}
    try:
        ide = discover_pycharm(pycharm)
        checks['pycharm'] = {'installed': True, 'ready': checks['runtime']['ready'] and checks['dependencies']['ready'], 'path': str(ide)}
    except (RuntimeError, OSError, ValueError) as exc:
        ide = None
        checks['pycharm'] = {'installed': False, 'ready': False, 'error': str(exc)}
    native_ready = checks['runtime']['ready'] and checks['dependencies']['ready']
    return {'windows': os.name == 'nt', 'python': sys.executable, 'python_version': list(sys.version_info[:3]),
            'pycharm': str(ide) if ide else None, 'missing_packages': missing, 'checks': checks,
            'native_desktop_ready': native_ready, 'ready': checks['pycharm']['ready'],
            'scope': 'Prerequisite inspection only; no desktop, application, capture backend or project dependencies were exercised. Browser startup separately validates the supplied Chrome, Node and REA paths.'}


def main():
    args = parser().parse_args()
    if args.command == 'doctor':
        return inspect_environment(args.pycharm)
    from dependency_setup import validate_runtime
    validate_runtime()
    if args.command == 'setup':
        return inspect_environment(args.pycharm, install=True)
    missing = dependencies(args.command == 'start')
    if missing:
        raise RuntimeError('Run setup to install the missing local harness dependencies: ' + ', '.join(missing))
    native_modules()
    if args.command == 'start':
        return start(args)
    if args.command == '_keeper':
        return keeper(Path(args.session))
    if args.command == '_worker':
        return worker(Path(args.session))
    return request(args)


if __name__ == '__main__':
    try:
        result = main()
        if result is not None:
            print(json.dumps({'ok': True, 'result': result}, indent=2))
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        if len(sys.argv) > 1 and sys.argv[1].startswith('_'):
            traceback.print_exc()
        sys.exit(1)
