import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import uuid

def verified_files(source):
    manifest = json.loads((source / 'install-manifest.json').read_text(encoding='utf-8'))
    for relative, expected in manifest.items():
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts or '\\' in relative:
            raise RuntimeError('Invalid install manifest path.')
        item = source / path
        if item.is_symlink() or not item.is_file() or hashlib.sha256(item.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Installer integrity check failed: ' + relative)
    return manifest

def inside(path, root):
    if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root.parent):
        raise RuntimeError('Installer paths must not traverse links.')
    path.resolve().relative_to(root.resolve())

def toml_config(text, entry):
    import tomllib
    tomllib.loads(text)
    lines = []
    skip = False
    section = re.compile(r'''^\[mcp_servers\.(?:stealth-commander|"stealth-commander"|'stealth-commander')(?:\.[^\]]+)?\]\s*(?:#.*)?$''')
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith('['):
            skip = bool(section.match(line.strip()))
        if not skip:
            lines.append(line)
    updated = ''.join(lines).rstrip() + '\n\n[mcp_servers.stealth-commander]\ncommand = ' + json.dumps(entry['command']) + '\nargs = ' + json.dumps(entry['args']) + '\n'
    parsed = tomllib.loads(updated)
    if parsed['mcp_servers']['stealth-commander'] != entry:
        raise RuntimeError('Existing Codex MCP entry cannot be replaced safely.')
    return updated

def install(skip_dependencies=False):
    from dependency_setup import validate_runtime
    validate_runtime()
    source = Path(__file__).resolve().parent.parent
    manifest = verified_files(source)
    home = Path.home().resolve()
    appdata = Path(os.environ.get('APPDATA', str(home / 'AppData' / 'Roaming')))
    targets = [home / folder / 'skills' / 'stealth-ghost-delegation' for folder in ('.agents', '.claude', '.codex', '.factory', '.gemini')]
    targets.append(home / '.gemini' / 'config' / 'skills' / 'stealth-ghost-delegation')
    canonical = targets[0] / 'scripts' / 'stealth_commander_mcp.py'
    entry = {'command': sys.executable, 'args': ['-B', str(canonical)]}
    configs = [home / '.factory/mcp.json', home / '.gemini/antigravity/mcp_config.json', home / '.gemini/config/mcp_config.json', home / '.cursor/mcp.json', home / '.codeium/windsurf/mcp_config.json', appdata / 'Claude/claude_desktop_config.json']
    updates = {}
    for config in configs:
        if not config.parent.exists():
            continue
        inside(config, home)
        data = json.loads(config.read_text(encoding='utf-8')) if config.exists() else {}
        if not isinstance(data, dict) or not isinstance(data.get('mcpServers', {}), dict):
            raise RuntimeError('Invalid MCP configuration: ' + str(config))
        data.setdefault('mcpServers', {})['stealth-commander'] = dict(entry)
        updates[config] = json.dumps(data, indent=2) + '\n'
    codex = home / '.codex/config.toml'
    if codex.parent.exists():
        inside(codex, home)
        updates[codex] = toml_config(codex.read_text(encoding='utf-8') if codex.exists() else '', entry)
    if not skip_dependencies:
        from dependency_setup import install as install_dependencies
        install_dependencies()
    backup = home / '.stealth-commander' / 'install-backups' / uuid.uuid4().hex
    backup.mkdir(parents=True)
    staged = []
    changed = []
    old_configs = {}
    try:
        for index, target in enumerate(targets):
            inside(target, home)
            if target.resolve() == source.resolve():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            stage = target.with_name('.stealth-install-' + uuid.uuid4().hex)
            stage.mkdir()
            for relative in manifest:
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / relative, destination)
            shutil.copyfile(source / 'install-manifest.json', stage / 'install-manifest.json')
            verified_files(stage)
            staged.append(stage)
            previous = backup / ('skill-' + str(index))
            if target.exists():
                os.replace(target, previous)
            changed.append((target, previous))
            os.replace(stage, target)
        for index, (config, content) in enumerate(updates.items()):
            old_configs[config] = config.read_bytes() if config.exists() else None
            if old_configs[config] is not None:
                (backup / ('config-' + str(index))).write_bytes(old_configs[config])
            temp = config.with_name(config.name + '.stealth-' + uuid.uuid4().hex)
            temp.write_text(content, encoding='utf-8')
            os.replace(temp, config)
        return {'ok': True, 'skills_installed': [str(p) for p in targets], 'mcp_configs_updated': [str(p) for p in updates], 'backup_directory': str(backup), 'services_started': False, 'account_state_changed': False}
    except Exception:
        for config, content in old_configs.items():
            if content is None:
                if config.exists():
                    config.unlink()
            else:
                config.write_bytes(content)
        for index, (target, previous) in enumerate(reversed(changed)):
            if target.exists():
                os.replace(target, backup / ('failed-new-' + str(index)))
            if previous.exists():
                os.replace(previous, target)
        for index, stage in enumerate(staged):
            if stage.exists():
                os.replace(stage, backup / ('incomplete-stage-' + str(index)))
        raise

if __name__ == '__main__':
    try:
        print(json.dumps(install('--skip-dependencies' in sys.argv), indent=2))
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        sys.exit(1)
