import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { Miniflare } from 'miniflare';
import { authStore } from '../src/auth-store.js';
import { createHash } from 'node:crypto';
import { hashPassword, createSession } from '../legacy-worker.js';
const origin = 'https://stealthcommander.pages.dev';
const email = 'approved@example.test';
const password = 'synthetic-long-password';
const user = { email, name: 'Synthetic', api_key: 'sc_live_' + 'a'.repeat(40), salt: 'a'.repeat(32), devices: [], created_at: '2026-10-25T12:00:00.000Z' };
user.hash = await hashPassword(password, user.salt);
async function fixture(allowlist) {
  const mf = new Miniflare({ workers: [{ name: 'gateway', modules: true, scriptPath: 'build/worker.js', compatibilityDate: '2026-07-30', compatibilityFlags: ['global_fetch_strictly_public'], bindings: { GATEWAY_ORIGIN: origin, PRIVATE_MODE: 'true', ...(allowlist === undefined ? {} : { PRIVATE_ACCOUNT_ALLOWLIST: allowlist }), CONTROL_CSRF_SECRET: 'synthetic-control-secret-at-least-32-chars', BILLING_TEST_ENABLED: 'true', BILLING_MODE: 'test' }, serviceBindings: { AUTH_STORE: 'auth', BILLING_SERVICE: () => Response.json({ received: true }), ASSETS: () => new Response('PRIVATE_DASHBOARD_SOURCE'), AUTH_RATE_LIMITER: () => Response.json({ success: true }), REGISTRATION_RATE_LIMITER: () => Response.json({ success: true }) }, outboundService() { throw new Error('No external network.'); } }, { name: 'auth', modules: true, script: readFileSync(new URL('../../strong-auth-service/worker.js', import.meta.url), 'utf8'), compatibilityDate: '2026-07-30', durableObjects: { STORE: { className: 'AuthStore', useSQLite: true } }, kvNamespaces: ['LEGACY_AUTH'] }] });
  const store = authStore(await mf.getWorker('auth')); await store.initialize();
  await store.put('user:' + email, JSON.stringify(user)); await store.put('apikey:' + user.api_key, email);
  const call = (path, body, cookie) => mf.dispatchFetch(origin + path, { method: body ? 'POST' : 'GET', headers: { Origin: origin, 'CF-Connecting-IP': '192.0.2.8', ...(body ? { 'Content-Type': 'application/json' } : {}), ...(cookie ? { Cookie: cookie } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) });
  const outsider = { ...user, email: 'outsider@example.test', api_key: 'sc_live_' + 'b'.repeat(40), devices: [{ device_id: 'outside-pc', mcp_url: 'https://outside-pc.trycloudflare.com/' + 'x'.repeat(48) + '/mcp', oauth_pair_id: 'pair', device_credential_hash: createHash('sha256').update('sc_dev_' + 'c'.repeat(48)).digest('hex') }] };
  await store.put('user:' + outsider.email, JSON.stringify(outsider));
  await store.put('apikey:' + outsider.api_key, outsider.email);
  await store.put('deviceauth:' + outsider.devices[0].device_credential_hash, JSON.stringify({ email: outsider.email, deviceId: 'outside-pc', pairId: 'pair', accountKeyHash: createHash('sha256').update(outsider.api_key).digest('hex'), expiresAt: Date.now() + 3600000 }));
  const outsiderSession = await createSession(outsider, store);
  return { mf, call, outsider, outsiderSession };
}

test('private mode never serves the dashboard, downloads, registry or fallback aliases anonymously', async () => {
  const f = await fixture(JSON.stringify([email]));
  try {
    const signin = await f.call('/'); assert.equal(signin.status, 200); const source = await signin.text();
    assert.match(source, /Private testing/); assert.ok(!source.includes('PRIVATE_DASHBOARD_SOURCE'));
    for (const path of ['/index.html', '/download/installer.zip', '/registry.json', '/oauth/unknown', '/.well-known/unknown']) { const r = await f.call(path); assert.equal(r.status, 403); assert.ok(!(await r.text()).includes('PRIVATE_DASHBOARD_SOURCE')); }
    assert.equal((await f.call('/.well-known/oauth-authorization-server')).status, 200);
    const login = await f.call('/api/auth/login', { email, password }); assert.equal(login.status, 200);
    const cookie = login.headers.get('Set-Cookie').split(';')[0];
    assert.equal(await (await f.call('/', null, cookie)).text(), 'PRIVATE_DASHBOARD_SOURCE');
    assert.equal((await f.call('/api/account/session', null, cookie)).status, 200);
    assert.equal((await f.call('/api/auth/register', { email: 'outsider@example.test', password })).status, 403);
    assert.equal((await f.call('/api/auth/login', { email: 'outsider@example.test', password })).status, 401);
    assert.equal((await f.call('/api/account/session', null, cookie + '; ' + cookie)).status, 401);
  } finally { await f.mf.dispose(); }
});

test('missing, empty, malformed and non-normalized private allowlists fail closed for existing and new accounts', async () => {
  for (const allowlist of [undefined, 'invalid', '[]', '[123]', '["APPROVED@example.test"]']) {
    const f = await fixture(allowlist);
    try {
      assert.equal((await f.call('/api/auth/login', { email, password })).status, 401);
      assert.equal((await f.call('/api/auth/login', { api_key: user.api_key })).status, 401);
      assert.equal((await f.call('/api/auth/register', { email, password })).status, 403);
      assert.equal((await f.call('/index.html')).status, 403);
    } finally { await f.mf.dispose(); }
  }
});


test('previously valid nonallowlisted sessions, API keys and PC credentials cannot enter private control or consent', async () => {
  const f = await fixture(JSON.stringify([email]));
  try {
    const cookie = 'sc_session=' + f.outsiderSession;
    assert.equal((await f.call('/index.html', null, cookie)).status, 403);
    assert.equal((await f.call('/api/account/session', null, cookie)).status, 401);
    assert.equal((await f.call('/api/device/control', { device_id: 'outside-pc', path: '/meta', method: 'GET' }, cookie)).status, 401);
    for (const token of [f.outsider.api_key, f.outsiderSession]) assert.equal((await f.mf.dispatchFetch(origin + '/api/auth/me', { headers: { Authorization: 'Bearer ' + token } })).status, 401);
    assert.equal((await f.mf.dispatchFetch(origin + '/api/devices/me', { headers: { Authorization: 'Bearer sc_dev_' + 'c'.repeat(48) } })).status, 401);
    const registered = await f.call('/oauth/register', { client_name: 'Synthetic privacy test', redirect_uris: ['https://client.example/callback'], token_endpoint_auth_method: 'none' });
    assert.equal(registered.status, 201); const client = await registered.json();
    const query = new URLSearchParams({ response_type: 'code', client_id: client.client_id, redirect_uri: 'https://client.example/callback', scope: 'pc:control', resource: origin + '/mcp', code_challenge: 'a'.repeat(43), code_challenge_method: 'S256' });
    const consent = await f.call('/authorize?' + query, null, cookie);
    assert.equal(consent.status, 200); assert.match(await consent.text(), /Sign in to Stealth Commander/);
    const webhook = await f.mf.dispatchFetch(origin + '/api/billing/webhook', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Stripe-Signature': 'synthetic-signature-verified-by-private-service' }, body: '{"id":"synthetic-test-event"}' });
    assert.equal(webhook.status, 200); assert.deepEqual(await webhook.json(), { received: true });
  } finally { await f.mf.dispose(); }
});
