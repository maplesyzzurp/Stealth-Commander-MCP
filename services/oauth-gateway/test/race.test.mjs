import { before, after, test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { Miniflare } from 'miniflare';
import legacy from '../legacy-worker.js';
import { authStore } from '../src/auth-store.js';

const origin = 'https://stealthcommander.pages.dev';
let mf, authority, sequence = 0;
before(async () => {
  mf = new Miniflare({ name: 'auth', modules: true, script: readFileSync(new URL('../../strong-auth-service/worker.js', import.meta.url), 'utf8'), compatibilityDate: '2026-07-30', durableObjects: { STORE: { className: 'AuthStore', useSQLite: true } }, kvNamespaces: ['LEGACY_AUTH'], outboundService() { throw new Error('No external network.'); } });
  authority = authStore({ fetch: (...args) => mf.dispatchFetch(...args) });
  await authority.initialize();
});
after(async () => mf?.dispose());

async function fixture() {
  let gate, signalArrival, release;
  const store = { ...authority, async transact(checks, puts, deletes) {
    if (gate && puts.some(p => p.key.startsWith(gate))) { gate = null; signalArrival(); await store.released; }
    return authority.transact(checks, puts, deletes);
  } };
  const pause = key => { gate = key; store.arrived = new Promise(resolve => { signalArrival = resolve; }); store.released = new Promise(resolve => { release = resolve; }); return () => release(); };
  const env = { STEALTH_AUTH: store, LEGACY_MCP_URL_KEYS_ENABLED: 'true', ASSETS: { fetch: () => new Response('synthetic') } };
  const call = (path, token, body, method = body ? 'POST' : 'GET') => legacy.fetch(new Request(origin + path, { method, headers: { Origin: origin, 'Content-Type': 'application/json', ...(token ? { Authorization: 'Bearer ' + token } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) }), env);
  const registered = await call('/api/auth/register', null, { email: `race${++sequence}@example.test`, password: 'synthetic-long-password' });
  assert.equal(registered.status, 200); const owner = await registered.json();
  const details = { device_id: 'race-pc', mcp_url: `https://race-pc.trycloudflare.com/${String(sequence).padStart(48, 'r')}/mcp` };
  return { call, owner, details, store, pause };
}

test('a delayed rotation cannot replace the recovery key after the original session is revoked (SQLite)', async () => {
  const f = await fixture(); const release = f.pause('accountkey:');
  const delayed = f.call('/api/account/rotate-key', f.owner.token, {});
  await f.store.arrived;
  const recovered = await f.call('/api/account/rotate-key', f.owner.token, {}); assert.equal(recovered.status, 200);
  const key = (await recovered.json()).user.api_key;
  release(); assert.equal((await delayed).status, 401);
  assert.equal((await f.call('/api/auth/me', key)).status, 200);
  assert.equal((await f.call('/api/auth/me', f.owner.token)).status, 401);
});

test('delayed heartbeats cannot restore a rotated, unlinked, unpaired or replaced PC (SQLite)', async () => {
  for (const action of ['rotate', 'owner-unlink', 'own-unpair', 're-pair']) {
    const f = await fixture();
    const paired = await (await f.call('/api/devices/pair', f.owner.token, f.details)).json();
    const release = f.pause('user:');
    const delayed = f.call('/api/devices/heartbeat', paired.device_token, f.details);
    await f.store.arrived;
    let replacement;
    if (action === 'rotate') { const response = await f.call('/api/account/rotate-key', f.owner.token, {}); assert.equal(response.status, 200); replacement = (await response.json()).user.api_key; }
    if (action === 'owner-unlink') assert.equal((await f.call('/api/account/devices', f.owner.token, { device_id: 'race-pc' }, 'DELETE')).status, 200);
    if (action === 'own-unpair') assert.equal((await f.call('/api/devices/unpair', paired.device_token, {})).status, 200);
    if (action === 're-pair') assert.equal((await f.call('/api/devices/pair', f.owner.token, f.details)).status, 200);
    release(); assert.equal((await delayed).status, 401);
    assert.equal((await f.call('/api/devices/me', paired.device_token)).status, 401);
    const profile = await authority.get('user:' + f.owner.user.email, { type: 'json' });
    if (action === 'owner-unlink' || action === 'own-unpair') {
      assert.deepEqual(profile.devices, []);
      assert.equal((await f.call('/api/mcp/' + f.owner.user.api_key + '/race-pc', null, {})).status, 404);
    }
    if (replacement) assert.equal((await f.call('/api/auth/me', replacement)).status, 200);
  }
});

test('concurrent registration creates exactly one password and credential authority (SQLite)', async () => {
  const f = await fixture(); const email = `registration${sequence}@example.test`;
  const replies = await Promise.all(['first-password-long', 'second-password-long'].map(password => f.call('/api/auth/register', null, { email, password })));
  assert.deepEqual(replies.map(r => r.status).sort(), [200, 409]);
  const winner = await replies.find(r => r.status === 200).json();
  assert.equal((await f.call('/api/auth/me', winner.user.api_key)).status, 200);
  assert.equal((await authority.get('accountkey:' + email, { type: 'json' })).api_key, winner.user.api_key);
});
