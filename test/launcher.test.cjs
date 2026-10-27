const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../bin/stealth-commander.js'), 'utf8');

function runLauncher({ platform = 'win32', arch = 'x64', probe, spawnError, argv = ['node', 'launcher', 'doctor'], env = {} } = {}) {
  const calls = [], errors = [], exits = [];
  const child = new EventEmitter();
  const exit = Symbol('exit');
  const process = { platform, arch, argv, env, stdin: { isTTY: false }, exit(code) { exits.push(code); throw exit; } };
  const childProcess = {
    spawnSync(command, args, options) { calls.push({ probe: command, args, options }); return probe ? probe(command, args) : { status: 0 }; },
    spawn(command, args, options) { calls.push({ spawn: command, args, options }); if (spawnError) throw spawnError; return child; }
  };
  try { vm.runInNewContext(source, { require(name) { if (name === 'child_process' || name === 'node:child_process') return childProcess; return require(name); }, __dirname: path.join(__dirname, '../bin'), process, console: { error(message) { errors.push(message); } } }); }
  catch (error) { if (error !== exit) throw error; }
  return { calls, errors, exits, child, exit };
}

test('rejects unsupported operating systems before probing or launching', () => {
  const result = runLauncher({ platform: 'linux' });
  assert.deepEqual(result.exits, [1]);
  assert.equal(result.calls.length, 0);
  assert.match(result.errors.join(' '), /Windows/i);
});

test('rejects unsupported host architecture before probing or launching', () => {
  const result = runLauncher({ arch: 'arm64' });
  assert.deepEqual(result.exits, [1]);
  assert.equal(result.calls.length, 0);
});

test('skips an older Python and launches the next supported interpreter without a visible window', () => {
  const result = runLauncher({ probe(command, args) {
    assert.equal(args[0], '-c');
    return { status: command === 'python' ? 1 : 0 };
  } });
  const spawn = result.calls.find(call => call.spawn);
  assert.equal(spawn.spawn, 'python3');
  assert.equal(spawn.options.windowsHide, true);
  assert.equal(spawn.args[0], '-B');
  assert.equal(spawn.args.at(-1), 'doctor');
});

test('uses an explicitly selected interpreter only when its prerequisite probe succeeds', () => {
  const result = runLauncher({ env: { PYCHARM_HARNESS_PYTHON: 'selected-python.exe' }, probe(command) { return { status: command === 'selected-python.exe' ? 0 : 1 }; } });
  assert.equal(result.calls.find(call => call.spawn).spawn, 'selected-python.exe');
});

test('returns an actionable failure without launching when no Python meets prerequisites', () => {
  const result = runLauncher({ probe() { return { status: 1 }; } });
  assert.deepEqual(result.exits, [1]);
  assert.equal(result.calls.some(call => call.spawn), false);
  assert.match(result.errors.join(' '), /3\.11/);
});

test('reports asynchronous spawn failures with a nonzero exit', () => {
  const result = runLauncher();
  assert.throws(() => result.child.emit('error', Object.assign(new Error('sensitive command line'), { code: 'ENOENT' })), error => error === result.exit);
  assert.deepEqual(result.exits, [1]);
  assert.match(result.errors.join(' '), /ENOENT/);
  assert.doesNotMatch(result.errors.join(' '), /sensitive command line/);
});

test('maps signal termination to failure instead of reporting success', () => {
  const result = runLauncher();
  assert.throws(() => result.child.emit('exit', null, 'SIGTERM'), error => error === result.exit);
  assert.deepEqual(result.exits, [1]);
});
