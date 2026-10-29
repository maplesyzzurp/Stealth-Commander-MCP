const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../bin/stealth-commander.js'), 'utf8');

function runLauncher({ platform = 'win32', arch = 'x64', probe, spawnError, argv = ['node', 'launcher', 'doctor'], env = {}, stdinTTY = false, stdoutTTY = false, columns = 80 } = {}) {
  const calls = [], errors = [], exits = [], output = [];
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stdout.setEncoding = () => {};
  const exit = Symbol('exit');
  const process = { platform, arch, argv, env, stdin: { isTTY: stdinTTY }, stdout: { isTTY: stdoutTTY, columns, write(value) { output.push(value); } }, exit(code) { exits.push(code); throw exit; } };
  const childProcess = {
    spawnSync(command, args, options) { calls.push({ probe: command, args, options }); return probe ? probe(command, args) : { status: 0 }; },
    spawn(command, args, options) { calls.push({ spawn: command, args, options }); if (spawnError) throw spawnError; return child; }
  };
  try { vm.runInNewContext(source, { require(name) { if (name === 'child_process' || name === 'node:child_process') return childProcess; return require(name); }, __dirname: path.join(__dirname, '../bin'), process, console: { error(message) { errors.push(message); } } }); }
  catch (error) { if (error !== exit) throw error; }
  return { calls, errors, exits, child, exit, output };
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

function finishPretty(result, body, code = 0) {
  result.child.stdout.emit('data', body);
  assert.throws(() => result.child.emit('close', code), error => error === result.exit);
  return result.output.join('');
}

test('interactive installation reports success only after valid completion and keeps stdin available', () => {
  const result = runLauncher({ argv: ['node', 'launcher', 'install'], stdinTTY: true, stdoutTTY: true, env: { NO_COLOR: '' } });
  const launch = result.calls.find(call => call.spawn);
  assert.deepEqual(Array.from(launch.options.stdio), ['inherit', 'pipe', 'inherit']);
  assert.equal(result.output.length, 0);
  result.child.stdout.emit('data', '{"ok":');
  assert.equal(result.output.length, 0);
  const output = finishPretty(result, 'true,"private_field":"synthetic-private-value"}');
  assert.match(output, /STEALTH COMMANDER/);
  assert.match(output, /INSTALL COMPLETE/);
  assert.match(output, /#.*#/);
  assert.doesNotMatch(output, /synthetic-private-value|\u001b/);
  assert.deepEqual(result.exits, [0]);
  assert.ok(output.split('\n').every(line => line.length <= 76));
});

test('explicit pretty setup is formatted without ANSI in a noninteractive capture', () => {
  const result = runLauncher({ argv: ['node', 'launcher', 'setup', '--enable-remote', '--pretty'] });
  const launch = result.calls.find(call => call.spawn);
  assert.equal(launch.args.includes('--pretty'), false);
  assert.equal(launch.args.includes('--enable-remote'), true);
  const output = finishPretty(result, '{"ok":true}');
  assert.match(output, /SETUP COMPLETE/);
  assert.doesNotMatch(output, /\u001b/);
});

test('pretty installation preserves failure details and never prints a success banner', () => {
  const result = runLauncher({ argv: ['node', 'launcher', 'install', '--pretty'] });
  const body = '{"ok":false,"error":"Synthetic prerequisite failure"}\n';
  assert.equal(finishPretty(result, body, 1), body);
  assert.deepEqual(result.exits, [1]);
});

test('pretty output cannot turn a failed process or failed JSON result into success', () => {
  const failedExit = runLauncher({ argv: ['node', 'launcher', 'install', '--pretty'] });
  assert.equal(finishPretty(failedExit, '{"ok":true}', 2), '{"ok":true}');
  assert.deepEqual(failedExit.exits, [2]);
  const failedResult = runLauncher({ argv: ['node', 'launcher', 'install', '--pretty'] });
  assert.equal(finishPretty(failedResult, '{"ok":false}'), '{"ok":false}');
  assert.deepEqual(failedResult.exits, [1]);
});

test('noninteractive JSON and stdio MCP keep direct inherited output', () => {
  for (const options of [
    { argv: ['node', 'launcher', 'install'] },
    { argv: ['node', 'launcher', 'serve', '--pretty'], stdinTTY: true, stdoutTTY: true },
    { argv: ['node', 'launcher', 'doctor', '--pretty'], stdinTTY: true, stdoutTTY: true }
  ]) {
    const result = runLauncher(options);
    const launch = result.calls.find(call => call.spawn);
    assert.equal(launch.options.stdio, 'inherit');
    assert.equal(launch.args.includes('--pretty'), false);
    assert.equal(result.output.length, 0);
    assert.throws(() => result.child.emit('exit', 0), error => error === result.exit);
    assert.deepEqual(result.exits, [0]);
  }
});

test('large or unfamiliar pretty output stays bounded and passes through without a success claim', () => {
  const result = runLauncher({ argv: ['node', 'launcher', 'install', '--pretty'] });
  const oversized = 'x'.repeat(1024 * 1024 + 1);
  result.child.stdout.emit('data', oversized);
  result.child.stdout.emit('data', 'tail');
  assert.throws(() => result.child.emit('close', 0), error => error === result.exit);
  assert.equal(result.output.join(''), oversized + 'tail');
  const unfamiliar = runLauncher({ argv: ['node', 'launcher', 'install', '--pretty'] });
  assert.equal(finishPretty(unfamiliar, 'not JSON'), 'not JSON');
});

test('narrow terminal uses a compact ninja and readable title', () => {
  const result = runLauncher({ argv: ['node', 'launcher', 'install'], stdinTTY: true, stdoutTTY: true, columns: 40, env: { NO_COLOR: '1' } });
  const output = finishPretty(result, '{"ok":true}');
  assert.match(output, /\[> <\]/);
  assert.match(output, /STEALTH COMMANDER/);
  assert.match(output, /INSTALL COMPLETE/);
  assert.ok(output.split('\n').every(line => line.length <= 40));
});
