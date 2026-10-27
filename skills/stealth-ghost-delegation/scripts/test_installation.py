"""Portable installer, dependency identity, and pinned-download checks.

All client configurations and binaries are synthetic files in temporary folders.
No account credentials, real cloudflared binary, service startup, or network is used.
"""
from collections import namedtuple
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import sys
import sysconfig
import tempfile
import unittest
from unittest import mock

import dependency_setup as deps
import install_local
import vendor_downloads as vendor


class DependencyIdentityTests(unittest.TestCase):
    def test_binary_cache_changes_with_platform_architecture_and_abi(self):
        with mock.patch.object(sysconfig, 'get_platform', return_value='win-amd64'), mock.patch.object(sysconfig, 'get_config_var', return_value='cp313-win_amd64'), mock.patch.object(platform, 'machine', return_value='AMD64'), mock.patch.object(struct, 'calcsize', return_value=8):
            amd64 = deps.dependency_directory()
        with mock.patch.object(sysconfig, 'get_platform', return_value='win-arm64'), mock.patch.object(sysconfig, 'get_config_var', return_value='cp313-win_arm64'), mock.patch.object(platform, 'machine', return_value='ARM64'), mock.patch.object(struct, 'calcsize', return_value=8):
            arm64 = deps.dependency_directory()
        with mock.patch.object(sysconfig, 'get_platform', return_value='win-amd64'), mock.patch.object(sysconfig, 'get_config_var', return_value='cp313t-win_amd64'), mock.patch.object(platform, 'machine', return_value='AMD64'), mock.patch.object(struct, 'calcsize', return_value=8):
            threaded = deps.dependency_directory()
        with mock.patch.object(sysconfig, 'get_platform', return_value='win-amd64'), mock.patch.object(sysconfig, 'get_config_var', return_value='cp313-win_amd64'), mock.patch.object(platform, 'machine', return_value='AMD64'), mock.patch.object(struct, 'calcsize', return_value=4):
            win32 = deps.dependency_directory()
        self.assertEqual(len({amd64, arm64, threaded, win32}), 4)

    def test_unsupported_runtime_is_rejected_before_resolving_install_paths(self):
        cases = [({'platform': 'linux'}, {}, 'Windows'), ({'version_info': namedtuple('PythonVersion', 'major minor micro')(3, 10, 9)}, {}, '3.11')]
        for system, unused, message in cases:
            with self.subTest(system=system), mock.patch.multiple(sys, **system), mock.patch.object(Path, 'home', side_effect=AssertionError('must not inspect user home')):
                with self.assertRaisesRegex(RuntimeError, message):
                    deps.install()

    def test_32bit_python_is_rejected_before_install(self):
        with mock.patch.object(struct, 'calcsize', return_value=4), mock.patch.object(Path, 'home', side_effect=AssertionError('must not inspect user home')):
            with self.assertRaisesRegex(RuntimeError, '64-bit'):
                deps.install()


class DependencyActivationTests(unittest.TestCase):
    def test_pinned_cache_precedes_global_packages_during_pth_processing_without_duplicates(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('STEALTH_TEST_TMP')) as directory:
            root = Path(directory)
            global_site, cache = root / 'global-site', root / 'pinned-cache'
            extra = cache / 'extra-path'
            for parent, version in ((global_site, 'global'), (cache, 'pinned')):
                package = parent / '_stealth_precedence_fixture'
                package.mkdir(parents=True)
                (package / '__init__.py').write_text('VERSION = ' + repr(version), encoding='utf-8')
            extra.mkdir()
            (extra / '_stealth_pth_extra.py').write_text('VALUE = 42', encoding='utf-8')
            (cache / '_stealth_pth_bootstrap.py').write_text('from _stealth_precedence_fixture import VERSION', encoding='utf-8')
            (cache / 'precedence-fixture.pth').write_text('extra-path\nimport _stealth_pth_bootstrap\n', encoding='utf-8')
            for name in ('win32', 'win32/lib', 'Pythonwin'):
                (cache / name).mkdir(parents=True, exist_ok=True)
            modules = ('_stealth_precedence_fixture', '_stealth_pth_bootstrap', '_stealth_pth_extra')
            try:
                with mock.patch.object(deps, 'dependency_directory', return_value=cache), mock.patch.object(sys, 'path', [str(global_site), *sys.path, str(cache)]):
                    deps.activate()
                    self.assertEqual(importlib.import_module(modules[0]).VERSION, 'pinned')
                    self.assertEqual(importlib.import_module(modules[1]).VERSION, 'pinned')
                    self.assertEqual(importlib.import_module(modules[2]).VALUE, 42)
                    deps.activate()
                    normalized = [os.path.normcase(os.path.abspath(entry)) for entry in sys.path]
                    for name in ('', 'win32', 'win32/lib', 'Pythonwin'):
                        self.assertEqual(normalized.count(os.path.normcase(str(cache / name))), 1)
                    self.assertLess(sys.path.index(str(cache)), sys.path.index(str(global_site)))
                self.assertEqual((global_site / '_stealth_precedence_fixture/__init__.py').read_text(), "VERSION = 'global'")
            finally:
                for module in modules:
                    sys.modules.pop(module, None)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('STEALTH_TEST_TMP'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'profile'
        self.home.mkdir()
        source = Path(__file__).resolve().parent.parent
        self.source = self.root / 'source-skill'
        shutil.copytree(source, self.source, ignore=shutil.ignore_patterns('__pycache__', '.runtime', '*.pyc'))
        # This fixture tests installer behavior. Release-manifest parity is a separate CI gate.
        manifest = {p.relative_to(self.source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.source.rglob('*') if p.is_file() and p.name != 'install-manifest.json'}
        (self.source / 'install-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        self.appdata = self.home / 'AppData' / 'Roaming'
        for folder in ('.codex', '.factory', '.gemini/antigravity', '.gemini/config', '.cursor', '.codeium/windsurf', 'AppData/Roaming/Claude'):
            (self.home / folder).mkdir(parents=True)
        for patcher in (mock.patch.object(Path, 'home', return_value=self.home), mock.patch.object(install_local, '__file__', str(self.source / 'scripts/install_local.py')), mock.patch.dict(os.environ, {'APPDATA': str(self.appdata)})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def install(self):
        return install_local.install(skip_dependencies=True)

    def test_fresh_install_writes_effective_configs_and_verified_complete_skill(self):
        result = self.install()
        self.assertTrue(result['ok'])
        self.assertFalse(result['services_started'])
        self.assertFalse(result['account_state_changed'])
        canonical = self.home / '.agents/skills/stealth-ghost-delegation/scripts/stealth_commander_mcp.py'
        expected = {'command': sys.executable, 'args': ['-B', str(canonical)]}
        self.assertEqual(len(result['skills_installed']), 6)
        for installed in result['skills_installed']:
            manifest = install_local.verified_files(Path(installed))
            self.assertIn('scripts/java/HarnessAgent.java', manifest)
            self.assertIn('scripts/rea_bridge.py', manifest)
        import tomllib
        for name in result['mcp_configs_updated']:
            config = Path(name)
            parsed = tomllib.loads(config.read_text(encoding='utf-8')) if config.suffix == '.toml' else json.loads(config.read_text(encoding='utf-8'))
            self.assertEqual(parsed['mcp_servers' if config.suffix == '.toml' else 'mcpServers']['stealth-commander'], expected)
        self.assertFalse((self.home / '.stealth-commander/account.json').exists())

    def test_upgrade_preserves_other_clients_account_and_backup_of_old_skill(self):
        old = self.home / '.agents/skills/stealth-ghost-delegation'
        old.mkdir(parents=True)
        (old / 'stale.py').write_text('original old source', encoding='utf-8')
        account = self.home / '.stealth-commander/account.json'
        account.parent.mkdir()
        account.write_bytes(b'{"device_token":"SYNTHETIC_ONLY"}')
        codex = self.home / '.codex/config.toml'
        codex.write_text('model="preserve"\n[mcp_servers.other]\ncommand="keep-other"\n[mcp_servers."stealth-commander"]\ncommand="old"\n[mcp_servers."stealth-commander".env]\nSTALE="remove"\n', encoding='utf-8')
        config = self.home / '.factory/mcp.json'
        config.write_text('{"theme":"preserve","mcpServers":{"other":{"command":"keep-other"}}}', encoding='utf-8')
        result = self.install()
        self.assertFalse((old / 'stale.py').exists())
        self.assertEqual((Path(result['backup_directory']) / 'skill-0/stale.py').read_text(), 'original old source')
        self.assertEqual(account.read_bytes(), b'{"device_token":"SYNTHETIC_ONLY"}')
        import tomllib
        parsed = tomllib.loads(codex.read_text(encoding='utf-8'))
        self.assertEqual(parsed['model'], 'preserve')
        self.assertEqual(parsed['mcp_servers']['other']['command'], 'keep-other')
        self.assertNotIn('env', parsed['mcp_servers']['stealth-commander'])
        self.assertEqual(json.loads(config.read_text())['mcpServers']['other'], {'command': 'keep-other'})

    def test_malformed_config_fails_without_installing_any_skill(self):
        config = self.home / '.factory/mcp.json'
        config.write_text('{invalid', encoding='utf-8')
        with self.assertRaises(json.JSONDecodeError):
            self.install()
        self.assertEqual(config.read_text(), '{invalid')
        self.assertFalse((self.home / '.agents').exists())

    def test_write_failure_restores_config_and_old_skill(self):
        old = self.home / '.agents/skills/stealth-ghost-delegation'
        old.mkdir(parents=True)
        (old / 'keep.py').write_text('original source', encoding='utf-8')
        config = self.home / '.factory/mcp.json'
        config.write_text('{"keep":true}', encoding='utf-8')
        replace = os.replace
        failed = False
        def fail_once(source, destination):
            nonlocal failed
            if Path(destination) == config and not failed:
                failed = True
                raise PermissionError('Synthetic configuration write failure')
            return replace(source, destination)
        with mock.patch.object(install_local.os, 'replace', side_effect=fail_once):
            with self.assertRaises(PermissionError):
                self.install()
        self.assertEqual((old / 'keep.py').read_text(), 'original source')
        self.assertEqual(config.read_text(), '{"keep":true}')

    def test_tampered_source_is_rejected_before_client_changes(self):
        (self.source / 'scripts/rea_bridge.py').write_bytes(b'changed fixture')
        with self.assertRaisesRegex(RuntimeError, 'integrity'):
            self.install()
        self.assertFalse((self.home / '.agents').exists())

    def test_unsupported_python_never_installs_skills(self):
        with mock.patch.object(sys, 'version_info', (3, 10, 9)):
            with self.assertRaisesRegex(RuntimeError, '3.11'):
                self.install()
        self.assertFalse((self.home / '.agents').exists())


class VendorDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('STEALTH_TEST_TMP'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = b'not executable: synthetic pinned vendor binary'
        patcher = mock.patch.object(vendor, 'CLOUDFLARED_SHA256', hashlib.sha256(self.fixture).hexdigest())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_download_verifies_fixture_and_replaces_only_unverified_cache_file(self):
        cache = self.root / 'vendor'
        cache.mkdir()
        (cache / 'cloudflared.exe').write_bytes(b'tampered cache')
        with mock.patch.object(vendor.shutil, 'which', return_value=None), mock.patch.object(vendor.urllib.request, 'urlopen', return_value=io.BytesIO(self.fixture)):
            result = vendor.cloudflared(cache)
        self.assertEqual(Path(result).read_bytes(), self.fixture)
        self.assertEqual([p.read_bytes() for p in cache.glob('cloudflared-unverified-*.exe')], [b'tampered cache'])
        self.assertEqual(list(cache.glob('*.download')), [])

    def test_invalid_download_never_replaces_prior_file(self):
        cache = self.root / 'vendor'
        cache.mkdir()
        original = cache / 'cloudflared.exe'
        original.write_bytes(b'preserve prior binary')
        with mock.patch.object(vendor.shutil, 'which', return_value=None), mock.patch.object(vendor.urllib.request, 'urlopen', return_value=io.BytesIO(b'wrong digest')):
            with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
                vendor.cloudflared(cache)
        self.assertEqual(original.read_bytes(), b'preserve prior binary')
        self.assertEqual(list(cache.glob('*.download')), [])

    def test_32bit_runtime_refuses_tunnel_before_download(self):
        with mock.patch.object(struct, 'calcsize', return_value=4), mock.patch.object(vendor.urllib.request, 'urlopen', side_effect=AssertionError('no network expected')):
            with self.assertRaisesRegex(RuntimeError, '64-bit'):
                vendor.cloudflared(self.root / 'vendor')


if __name__ == '__main__':
    unittest.main(verbosity=2)
