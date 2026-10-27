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
async function fixture(allowlist, bindings = {}, assets = () => new Response('PRIVATE_DASHBOARD_SOURCE')) {
  const mf = new Miniflare({ workers: [{ name: 'gateway', modules: true, scriptPath: 'build/worker.js', compatibilityDate: '2026-07-30', compatibilityFlags: ['global_fetch_strictly_public'], bindings: { GATEWAY_ORIGIN: origin, PRIVATE_MODE: 'true', ...(allowlist === undefined ? {} : { PRIVATE_ACCOUNT_ALLOWLIST: allowlist }), CONTROL_CSRF_SECRET: 'synthetic-control-secret-at-least-32-chars', BILLING_TEST_ENABLED: 'true', BILLING_MODE: 'test', ...bindings }, serviceBindings: { AUTH_STORE: 'auth', BILLING_SERVICE: () => Response.json({ received: true }), ASSETS: assets, AUTH_RATE_LIMITER: () => Response.json({ success: true }), REGISTRATION_RATE_LIMITER: () => Response.json({ success: true }) }, outboundService() { throw new Error('No external network.'); } }, { name: 'auth', modules: true, script: readFileSync(new URL('../../strong-auth-service/worker.js', import.meta.url), 'utf8'), compatibilityDate: '2026-07-30', durableObjects: { STORE: { className: 'AuthStore', useSQLite: true } }, kvNamespaces: ['LEGACY_AUTH'] }] });
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

test('domain verification serves only the configured challenge without opening private routes', async () => {
  const challenge = 'synthetic-review-challenge';
  const f = await fixture(JSON.stringify([email]), { OPENAI_APPS_CHALLENGE: challenge });
  try {
    const response = await f.call('/.well-known/openai-apps-challenge');
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('Content-Type'), 'text/plain; charset=utf-8');
    assert.equal(response.headers.get('Cache-Control'), 'no-store');
    assert.equal(response.headers.get('X-Content-Type-Options'), 'nosniff');
    assert.equal(await response.text(), challenge);
    const head = await f.mf.dispatchFetch(origin + '/.well-known/openai-apps-challenge', { method: 'HEAD' });
    assert.equal(head.status, 200);
    assert.equal(await head.text(), '');
    const post = await f.call('/.well-known/openai-apps-challenge', {});
    assert.equal(post.status, 405);
    assert.equal(post.headers.get('Allow'), 'GET, HEAD');
    assert.equal((await f.call('/.well-known/openai-apps-challenge?token=ignored')).status, 404);
    for (const path of ['/.well-known/openai-apps-challenge/', '/.well-known/unknown', '/index.html']) {
      const denied = await f.call(path);
      assert.equal(denied.status, 403);
      const body = await denied.text();
      assert.ok(!body.includes(challenge));
      assert.ok(!body.includes('PRIVATE_DASHBOARD_SOURCE'));
    }
    assert.equal((await f.call('/mcp')).status, 401);
    assert.equal((await f.call('/api/account/session')).status, 401);
    assert.equal((await f.call('/api/auth/register', { email: 'outsider@example.test', password })).status, 403);
  } finally { await f.mf.dispose(); }
});

test('missing or malformed domain challenges fail closed', async () => {
  for (const challenge of [undefined, '', 'line\nbreak', ' padded ', 123, 'a'.repeat(4097)]) {
    const f = await fixture(JSON.stringify([email]), challenge === undefined ? {} : { OPENAI_APPS_CHALLENGE: challenge });
    try {
      const response = await f.call('/.well-known/openai-apps-challenge');
      assert.equal(response.status, 404);
      assert.deepEqual(await response.json(), { error: 'Verification challenge is not configured.' });
    } finally { await f.mf.dispose(); }
  }
});

test('only exact informational routes and the original icon are public', async () => {
  const assetRequests = [];
  const publicPaths = ['/about', '/support', '/icon.png'];
  const f = await fixture(JSON.stringify([email]), {}, request => {
    const path = new URL(request.url).pathname;
    assetRequests.push({ path, method: request.method });
    assert.equal(request.headers.get('Cookie'), null);
    assert.equal(request.headers.get('Authorization'), null);
    assert.ok(publicPaths.includes(path));
    return new Response(request.method === 'HEAD' ? null : 'PUBLIC_ASSET:' + path, { headers: { 'Content-Type': path === '/icon.png' ? 'image/png' : 'text/html; charset=utf-8', 'Set-Cookie': 'must-not-reach-client=1' } });
  });
  try {
    for (const path of publicPaths) {
      const get = await f.mf.dispatchFetch(origin + path, { headers: { Cookie: 'sc_session=synthetic', Authorization: 'Bearer synthetic' } });
      assert.equal(get.status, 200);
      assert.equal(await get.text(), 'PUBLIC_ASSET:' + path);
      assert.equal(get.headers.get('Cache-Control'), 'public, max-age=300');
      assert.equal(get.headers.get('Set-Cookie'), null);
      assert.equal(get.headers.get('X-Frame-Options'), 'DENY');
      assert.match(get.headers.get('Content-Security-Policy'), /default-src 'none'/);
      const head = await f.mf.dispatchFetch(origin + path, { method: 'HEAD' });
      assert.equal(head.status, 200);
      assert.equal(await head.text(), '');
      for (const method of ['POST', 'PUT', 'DELETE', 'OPTIONS']) {
        const denied = await f.mf.dispatchFetch(origin + path, { method });
        assert.equal(denied.status, 405);
        assert.equal(denied.headers.get('Allow'), 'GET, HEAD');
      }
      assert.equal((await f.call(path + '?ignored=1')).status, 404);
    }
    assert.deepEqual(assetRequests, publicPaths.flatMap(path => [{ path, method: 'GET' }, { path, method: 'HEAD' }]));
    for (const path of ['/about.html', '/support.html', '/about/', '/support/', '/icon.png/', '/index.html', '/download/installer.zip', '/registry.json']) {
      const denied = await f.call(path);
      assert.equal(denied.status, 403);
      assert.ok(!(await denied.text()).includes('PUBLIC_ASSET'));
    }
    const signin = await f.call('/');
    assert.equal(signin.status, 200);
    assert.match(await signin.text(), /Private testing/);
    assert.equal((await f.call('/mcp')).status, 401);
    assert.equal((await f.call('/api/account/session')).status, 401);
    assert.equal((await f.call('/api/device/control', { device_id: 'outside-pc', path: '/meta', method: 'GET' })).status, 401);
  } finally { await f.mf.dispose(); }
});

test('informational asset redirects and missing files fail closed', async () => {
  const f = await fixture(JSON.stringify([email]), {}, request => new URL(request.url).pathname === '/about'
    ? Response.redirect(origin + '/about', 307)
    : new Response('missing', { status: 404 }));
  try {
    for (const path of ['/about', '/support']) {
      const response = await f.call(path);
      assert.equal(response.status, 404);
      assert.equal(response.headers.get('Location'), null);
      assert.deepEqual(await response.json(), { error: 'Public page not found.' });
    }
  } finally { await f.mf.dispose(); }
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
