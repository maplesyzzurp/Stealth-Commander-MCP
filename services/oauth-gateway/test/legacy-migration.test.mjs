import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';
import { createHash } from 'node:crypto';
import legacy from '../legacy-worker.js';

const origin = 'https://stealthcommander.pages.dev';
const ownerKey = 'sc_live_' + 'a'.repeat(40);
const hash = text => createHash('sha256').update(text).digest('hex');
let calls, originalFetch;
beforeEach(() => {
  calls = []; originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({ url, ...init });
    return Response.json({ result: { selected: new URL(url).hostname } });
  };
});
afterEach(() => { globalThis.fetch = originalFetch; });

function fixture(overrides = {}) {
  const user = { email: 'migration@example.test', api_key: ownerKey, devices: ['pc-one', 'pc-two'].map((id, i) => ({
    device_id: id, mcp_url: 'https://' + id + '.trycloudflare.com/' + String(i + 1).repeat(48) + '/mcp',
    oauth_pair_id: 'pair-' + id, device_credential_hash: hash('synthetic-device-' + id)
  })) };
  const values = new Map([['user:' + user.email, JSON.stringify(user)], ['apikey:' + ownerKey, user.email]]);
  for (const pc of user.devices) values.set('deviceauth:' + pc.device_credential_hash, JSON.stringify({
    email: user.email, deviceId: pc.device_id, pairId: pc.oauth_pair_id, accountKeyHash: hash(ownerKey), expiresAt: Date.now() + 60000
  }));
  const store = {
    async get(key, options) { const value = values.get(key) ?? null; return value === null || options?.type !== 'json' ? value : JSON.parse(value); },
    async claim() { return true; },
    async transact(checks, puts, deletes = []) {
      if (checks.some(c => (values.get(c.key) ?? null) !== c.value)) return false;
      for (const p of puts) values.set(p.key, p.value);
      for (const key of deletes) values.delete(key);
      return true;
    }
  };
  const env = { STEALTH_AUTH: store, ...overrides };
  const call = (path, headers = {}) => legacy.fetch(new Request(origin + path, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
  }), env);
  return { call };
}

test('URL credentials are disabled by default without echoing or forwarding them', async () => {
  for (const flag of [undefined, 'false', 'yes']) {
    const f = fixture(flag === undefined ? {} : { LEGACY_MCP_URL_KEYS_ENABLED: flag });
    const response = await f.call('/api/mcp/' + ownerKey + '/pc-one');
    assert.equal(response.status, 410);
    assert.ok(!(await response.text()).includes(ownerKey));
  }
  assert.equal(calls.length, 0);
});

test('an explicit migration opt-in preserves URL credentials with a selected owned PC', async () => {
  const f = fixture({ LEGACY_MCP_URL_KEYS_ENABLED: 'true' });
  const response = await f.call('/api/mcp/' + ownerKey + '/pc-two');
  assert.equal(response.status, 200);
  assert.equal((await response.json()).result.selected, 'pc-two.trycloudflare.com');
  assert.equal(calls.length, 1);
  assert.ok(calls[0].headers['X-Stealth-PC-Authorization'].startsWith('sc_pct_'));
  assert.equal(calls[0].headers.Authorization, undefined);
});

test('header credentials require and honor the explicit selected PC', async () => {
  const f = fixture();
  const missing = await f.call('/api/mcp', { Authorization: 'Bearer ' + ownerKey });
  assert.equal(missing.status, 400);
  assert.equal(calls.length, 0);
  const response = await f.call('/api/mcp', { Authorization: 'Bearer ' + ownerKey, 'X-Stealth-Device-Id': 'pc-two' });
  assert.equal(response.status, 200);
  assert.equal((await response.json()).result.selected, 'pc-two.trycloudflare.com');
});

test('migration mode never falls back to the first PC and rejects conflicting selectors', async () => {
  const f = fixture({ LEGACY_MCP_URL_KEYS_ENABLED: 'true' });
  assert.equal((await f.call('/api/mcp/' + ownerKey)).status, 400);
  assert.equal((await f.call('/api/mcp/' + ownerKey + '/pc-one', { 'X-Stealth-Device-Id': 'pc-two' })).status, 400);
  assert.equal((await f.call('/api/mcp/' + ownerKey + '/pc-one/extra')).status, 400);
  assert.equal(calls.length, 0);
});

test('header credentials cannot select another account PC or use a device credential', async () => {
  const f = fixture();
  assert.equal((await f.call('/api/mcp', { Authorization: 'Bearer ' + ownerKey, 'X-Stealth-Device-Id': 'not-owned' })).status, 404);
  assert.equal((await f.call('/api/mcp', { Authorization: 'Bearer sc_dev_' + 'b'.repeat(48), 'X-Stealth-Device-Id': 'pc-one' })).status, 401);
  assert.equal(calls.length, 0);
});
