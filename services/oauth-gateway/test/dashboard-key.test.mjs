import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

const html = await readFile(new URL('../assets/index.html', import.meta.url), 'utf8');
const script = html.match(/\/\/ Account key display\.[^\n]*\n([\s\S]*?)\/\/ End account key display\./)[1];
function fixture({ key = 'synthetic-account-key', rejectsCopy = false } = {}) {
  const elements = new Map(['acctKeyCode', 'acctKeyReveal', 'acctKeyCopy'].map(id => [id, { textContent: '', disabled: false, attributes: {}, setAttribute(name, value) { this.attributes[name] = value; } }]));
  const copied = [], messages = [];
  const context = { currentAccount: key ? { api_key: key } : null, accountKeyVisible: false,
    document: { getElementById: id => elements.get(id) },
    navigator: { clipboard: { writeText: async value => { if (rejectsCopy) throw Error('synthetic clipboard failure'); copied.push(value); } } },
    toast: text => messages.push(text) };
  runInNewContext(script, context);
  return { context, elements, copied, messages, run: code => runInNewContext(code, context) };
}
test('account keys are masked in DOM text and absent from attributes by default', () => {
  const f = fixture(); f.run('renderAccountKey()');
  assert.equal(f.elements.get('acctKeyCode').textContent, '****************');
  assert.equal(f.elements.get('acctKeyReveal').attributes['aria-pressed'], 'false');
  assert.ok(!JSON.stringify([...f.elements.values()]).includes('synthetic-account-key'));
});
test('copy uses the real account key while the displayed key remains masked', async () => {
  const f = fixture(); f.run('renderAccountKey()'); await f.run('copyAccountKey()');
  assert.deepEqual(f.copied, ['synthetic-account-key']);
  assert.equal(f.elements.get('acctKeyCode').textContent, '****************');
  assert.ok(f.messages.every(message => !message.includes('synthetic-account-key')));
});
test('reveal is explicit and hide restores the mask and accessible button state', () => {
  const f = fixture(); f.run('renderAccountKey(); toggleAccountKey()');
  assert.equal(f.elements.get('acctKeyCode').textContent, 'synthetic-account-key');
  assert.equal(f.elements.get('acctKeyReveal').textContent, 'Hide key');
  assert.equal(f.elements.get('acctKeyReveal').attributes['aria-pressed'], 'true');
  f.run('hideAccountKey()');
  assert.equal(f.elements.get('acctKeyCode').textContent, '****************');
  assert.equal(f.elements.get('acctKeyReveal').textContent, 'Show key');
});
test('clearing private state removes the key and disables reveal/copy', async () => {
  const f = fixture(); f.run('toggleAccountKey(); accountKeyVisible=false; renderAccountKey(""); currentAccount=null');
  assert.equal(f.elements.get('acctKeyCode').textContent, '—');
  assert.equal(f.elements.get('acctKeyReveal').disabled, true);
  assert.equal(f.elements.get('acctKeyCopy').disabled, true);
  await f.run('copyAccountKey()'); assert.deepEqual(f.copied, []);
});
test('unavailable keys never reveal and clipboard failure never claims success', async () => {
  const empty = fixture({ key: '' }); empty.run('toggleAccountKey()');
  assert.equal(empty.context.accountKeyVisible, false);
  const f = fixture({ rejectsCopy: true }); f.run('renderAccountKey()'); await f.run('copyAccountKey()');
  assert.deepEqual(f.copied, []);
  assert.deepEqual(f.messages, ['Could not copy the key. Try again.']);
});
