// Validate the exact npm payload, source inventory, and install-manifest agreement.
const assert = require('node:assert/strict');
const childProcess = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const skillPrefix = 'skills/stealth-ghost-delegation/';
const skill = path.join(root, 'skills', 'stealth-ghost-delegation');
const manifest = JSON.parse(fs.readFileSync(path.join(skill, 'install-manifest.json'), 'utf8'));
const npmCli = process.env.npm_execpath;
assert.ok(npmCli, 'Run this contract using npm run verify:package.');
const tempBase = path.resolve(process.env.STEALTH_TEST_TMP || os.tmpdir());
const temporary = fs.mkdtempSync(path.join(tempBase, 'stealth-package-'));

function sourceFiles(directory, prefix = '') {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    if (['.runtime', '__pycache__', 'node_modules', '.venv'].includes(entry.name) || /\.py[co]$/.test(entry.name)) return [];
    const relative = prefix + entry.name;
    const full = path.join(directory, entry.name);
    assert.equal(entry.isSymbolicLink(), false, `Source links are not package inputs: ${relative}`);
    return entry.isDirectory() ? sourceFiles(full, relative + '/') : [relative];
  });
}

try {
  const userConfig = path.join(temporary, 'user.npmrc');
  const globalConfig = path.join(temporary, 'global.npmrc');
  fs.writeFileSync(userConfig, '');
  fs.writeFileSync(globalConfig, '');
  const packed = childProcess.spawnSync(process.execPath, [npmCli, '--cache', path.join(temporary, 'cache'), '--userconfig', userConfig, '--globalconfig', globalConfig, '--offline', 'pack', '--dry-run', '--json', '--ignore-scripts'], { cwd: root, encoding: 'utf8', windowsHide: true });
  assert.equal(packed.status, 0, `npm dry-run failed: ${packed.stderr}`);
  const document = JSON.parse(packed.stdout);
  const metadata = Array.isArray(document) ? document[0] : Object.values(document)[0];
  const paths = metadata.files.map(file => file.path);
  const required = ['LICENSE', 'README.md', 'docs/RELEASE.md', 'plugin.json', '.codex-plugin/plugin.json', 'mcp.json', '.mcp.json', 'assets/ninja-mask.png', skillPrefix + 'references/CAPABILITIES.md'];
  for (const name of required) assert.ok(paths.includes(name), `Missing release input: ${name}`);
  const expected = sourceFiles(skill).filter(name => name !== 'install-manifest.json').sort();
  const actual = paths.filter(name => name.startsWith(skillPrefix) && name !== skillPrefix + 'install-manifest.json').map(name => name.slice(skillPrefix.length)).sort();
  assert.deepEqual(actual, expected, 'npm payload omitted authored skill files or added undeclared files.');
  assert.deepEqual(Object.keys(manifest).sort(), expected, 'Install manifest must cover every authored skill file.');
  for (const [relative, digest] of Object.entries(manifest)) {
    assert.ok(!relative.includes('\\') && !relative.split('/').includes('..') && !path.isAbsolute(relative), `Invalid manifest path: ${relative}`);
    const actualDigest = crypto.createHash('sha256').update(fs.readFileSync(path.join(skill, relative))).digest('hex');
    assert.equal(actualDigest, digest, `Stale install-manifest hash: ${relative}`);
  }
  console.log(JSON.stringify({ ok: true, version: metadata.version, packed_files: paths.length, verified_skill_files: expected.length }));
} finally {
  assert.equal(path.dirname(temporary), tempBase, 'Cleanup must remain within its task temp directory.');
  assert.ok(path.basename(temporary).startsWith('stealth-package-'));
  fs.rmSync(temporary, { recursive: true, force: true });
}
