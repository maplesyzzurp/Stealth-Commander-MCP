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
function assertSignInForm(source) {
  assert.match(source, /<form\b[^>]*id="signin"/);
  assert.match(source, /<input\b[^>]*name="email"[^>]*type="email"/);
  assert.match(source, /<input\b[^>]*name="password"[^>]*type="password"/);
  assert.match(source, /fetch\('\/api\/auth\/login'/);
  assert.match(source, /href="\/sign-up"/);
}
async function fixture(allowlist, bindings = {}, assets = request => new Response(new URL(request.url).pathname === '/landing' ? 'PUBLIC_LANDING_SOURCE' : 'PRIVATE_DASHBOARD_SOURCE')) {
  const mf = new Miniflare({ workers: [{ name: 'gateway', modules: true, scriptPath: 'build/worker.js', compatibilityDate: '2026-07-30', compatibilityFlags: ['global_fetch_strictly_public'], bindings: { GATEWAY_ORIGIN: origin, PRIVATE_MODE: 'true', ...(allowlist === undefined ? {} : { PRIVATE_ACCOUNT_ALLOWLIST: allowlist }), CONTROL_CSRF_SECRET: 'synthetic-control-secret-at-least-32-chars', BILLING_TEST_ENABLED: 'true', BILLING_MODE: 'test', ...bindings }, serviceBindings: { AUTH_STORE: 'auth', BILLING_SERVICE: () => Response.json({ received: true }), ASSETS: assets, AUTH_RATE_LIMITER: () => Response.json({ success: true }), REGISTRATION_RATE_LIMITER: () => Response.json({ success: true }) }, outboundService() { throw new Error('No external network.'); } }, { name: 'auth', modules: true, script: readFileSync(new URL('../../strong-auth-service/worker.js', import.meta.url), 'utf8'), compatibilityDate: '2026-07-30', durableObjects: { STORE: { className: 'AuthStore', useSQLite: true } }, kvNamespaces: ['LEGACY_AUTH'] }] });
  const store = authStore(await mf.getWorker('auth')); await store.initialize();
  await store.put('user:' + email, JSON.stringify(user)); await store.put('apikey:' + user.api_key, email);
  const call = (path, body, cookie) => mf.dispatchFetch(origin + path, { method: body ? 'POST' : 'GET', redirect: 'manual', headers: { Origin: origin, 'CF-Connecting-IP': '192.0.2.8', ...(body ? { 'Content-Type': 'application/json' } : {}), ...(cookie ? { Cookie: cookie } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) });
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
    const landing = await f.call('/'); assert.equal(landing.status, 200); assert.equal(await landing.text(), 'PUBLIC_LANDING_SOURCE');
    const signin = await f.call('/sign-in'); assert.equal(signin.status, 200); const source = await signin.text();
    assertSignInForm(source); assert.ok(!source.includes('PRIVATE_DASHBOARD_SOURCE'));
    const dashboard = await f.call('/dashboard'); assert.equal(dashboard.status, 303); assert.equal(dashboard.headers.get('Location'), '/sign-in');
    for (const path of ['/index.html', '/download/installer.zip', '/registry.json', '/oauth/unknown', '/.well-known/unknown']) { const r = await f.call(path); assert.equal(r.status, 403); assert.ok(!(await r.text()).includes('PRIVATE_DASHBOARD_SOURCE')); }
    assert.equal((await f.call('/.well-known/oauth-authorization-server')).status, 200);
    const login = await f.call('/api/auth/login', { email, password }); assert.equal(login.status, 200);
    const cookie = login.headers.get('Set-Cookie').split(';')[0];
    const authenticatedDashboard = await f.call('/dashboard', null, cookie);
    assert.equal(await authenticatedDashboard.text(), 'PRIVATE_DASHBOARD_SOURCE');
    assert.equal(authenticatedDashboard.headers.get('Cache-Control'), 'private, no-store');
    assert.equal(authenticatedDashboard.headers.get('Vary'), 'Cookie');
    assert.equal(await (await f.call('/', null, cookie)).text(), 'PUBLIC_LANDING_SOURCE');
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

test('only exact informational routes, homepage bundles and the original icon are public', async () => {
  const assetRequests = [];
  const publicPaths = ['/', '/about', '/support', '/icon.png', '/app.js', '/app.css'];
  const assetPaths = ['/landing', '/about', '/support', '/icon.png', '/app.js', '/app.css'];
  const f = await fixture(JSON.stringify([email]), {}, request => {
    const path = new URL(request.url).pathname;
    assetRequests.push({ path, method: request.method });
    assert.equal(request.headers.get('Cookie'), null);
    assert.equal(request.headers.get('Authorization'), null);
    assert.ok(assetPaths.includes(path));
    return new Response(request.method === 'HEAD' ? null : 'PUBLIC_ASSET:' + path, { headers: { 'Content-Type': path === '/icon.png' ? 'image/png' : 'text/html; charset=utf-8', 'Set-Cookie': 'must-not-reach-client=1' } });
  });
  try {
    for (const path of publicPaths) {
      const get = await f.mf.dispatchFetch(origin + path, { headers: { Cookie: 'sc_session=synthetic', Authorization: 'Bearer synthetic' } });
      assert.equal(get.status, 200);
      assert.equal(await get.text(), 'PUBLIC_ASSET:' + (path === '/' ? '/landing' : path));
      assert.equal(get.headers.get('Cache-Control'), ['/', '/app.js', '/app.css'].includes(path) ? 'no-store' : 'public, max-age=300');
      if (path === '/app.js') assert.equal(get.headers.get('Content-Type'), 'text/javascript; charset=utf-8');
      if (path === '/app.css') assert.equal(get.headers.get('Content-Type'), 'text/css; charset=utf-8');
      assert.equal(get.headers.get('Set-Cookie'), null);
      assert.equal(get.headers.get('X-Frame-Options'), 'DENY');
      assert.match(get.headers.get('Content-Security-Policy'), /default-src 'none'/);
      const head = await f.mf.dispatchFetch(origin + path, { method: 'HEAD' });
      assert.equal(head.status, 200);
      assert.equal(await head.text(), '');
      assert.equal(head.headers.get('Content-Type'), get.headers.get('Content-Type'));
      assert.equal(head.headers.get('Cache-Control'), get.headers.get('Cache-Control'));
      for (const method of ['POST', 'PUT', 'DELETE', 'OPTIONS']) {
        const denied = await f.mf.dispatchFetch(origin + path, { method });
        assert.equal(denied.status, 405);
        assert.equal(denied.headers.get('Allow'), 'GET, HEAD');
      }
      assert.equal((await f.call(path + '?ignored=1')).status, 404);
    }
    assert.deepEqual(assetRequests, assetPaths.flatMap(path => [{ path, method: 'GET' }, { path, method: 'HEAD' }]));
    for (const path of ['/landing', '/landing.html', '/about.html', '/support.html', '/about/', '/support/', '/icon.png/', '/index.html', '/download/installer.zip', '/registry.json', '/app.js/', '/app.css/', '/APP.js', '/assets/app.js', '/app.js.map', '/app.css.map', '/web/App.jsx', '/build/site/app.js']) {
      const denied = await f.call(path);
      assert.equal(denied.status, 403);
      assert.ok(!(await denied.text()).includes('PUBLIC_ASSET'));
    }
    const signin = await f.call('/sign-in');
    assert.equal(signin.status, 200);
    assertSignInForm(await signin.text());
    assert.equal((await f.call('/mcp')).status, 401);
    assert.equal((await f.call('/api/account/session')).status, 401);
    assert.equal((await f.call('/api/device/control', { device_id: 'outside-pc', path: '/meta', method: 'GET' })).status, 401);
  } finally { await f.mf.dispose(); }
});

test('informational and homepage bundle redirects and missing files fail closed', async () => {
  const f = await fixture(JSON.stringify([email]), {}, request => new URL(request.url).pathname === '/about'
    ? Response.redirect(origin + '/about', 307)
    : new Response('missing', { status: 404 }));
  try {
    for (const path of ['/about', '/support', '/app.js', '/app.css']) {
      const response = await f.call(path);
      assert.equal(response.status, 404);
      assert.equal(response.headers.get('Location'), null);
      assert.deepEqual(await response.json(), { error: 'Public page not found.' });
    }
  } finally { await f.mf.dispose(); }
});

test('homepage CSP permits the bundled application without granting script execution to informational pages', async () => {
  const f = await fixture(JSON.stringify([email]));
  const directives = response => new Map(response.headers.get('Content-Security-Policy').split(';').map(part => part.trim().split(/\s+/)).filter(([name]) => name).map(([name, ...values]) => [name, values]));
  try {
    const homepage = await f.call('/');
    const policy = directives(homepage);
    assert.deepEqual(policy.get('default-src'), ["'none'"]);
    assert.deepEqual(policy.get('script-src'), ["'self'"]);
    assert.deepEqual(policy.get('connect-src'), ["'self'"]);
    assert.deepEqual(policy.get('style-src'), ["'self'", "'unsafe-inline'"]);
    assert.deepEqual(policy.get('img-src'), ["'self'"]);
    assert.deepEqual(policy.get('frame-ancestors'), ["'none'"]);
    assert.deepEqual(policy.get('base-uri'), ["'none'"]);
    for (const path of ['/about', '/support']) {
      const restricted = directives(await f.call(path));
      assert.deepEqual(restricted.get('default-src'), ["'none'"]);
      assert.equal(restricted.has('script-src'), false);
      assert.equal(restricted.has('connect-src'), false);
      assert.deepEqual(restricted.get('style-src'), ["'unsafe-inline'"]);
    }
  } finally { await f.mf.dispose(); }
});

test('public pages and sign-in remain readable during account-store failure while private routes fail closed', async () => {
  for (const unavailableStore of [undefined, () => Response.json({ error: 'synthetic unavailable storage' }, { status: 503 })]) {
    const mf = new Miniflare({ modules: true, scriptPath: 'build/worker.js', compatibilityDate: '2026-07-30', compatibilityFlags: ['global_fetch_strictly_public'], bindings: { GATEWAY_ORIGIN: origin, PRIVATE_MODE: 'true', PRIVATE_ACCOUNT_ALLOWLIST: JSON.stringify([email]) }, serviceBindings: {
      ...(unavailableStore ? { AUTH_STORE: unavailableStore } : {}),
      ASSETS: request => new Response('PUBLIC_ASSET:' + new URL(request.url).pathname)
    }, outboundService() { throw new Error('No external network.'); } });
    try {
      for (const path of ['/', '/about', '/support', '/sign-in', '/app.js', '/app.css']) assert.equal((await mf.dispatchFetch(origin + path)).status, 200);
      for (const path of ['/dashboard', '/index.html', '/api/auth/me', '/mcp']) assert.equal((await mf.dispatchFetch(origin + path)).status, 503);
      assert.equal((await mf.dispatchFetch(origin + '/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })).status, 503);
    } finally { await mf.dispose(); }
  }
});

test('dashboard maps the existing index only after session approval and sign-in has no redirect parameters', async () => {
  const assetRequests = [];
  const f = await fixture(JSON.stringify([email]), {}, request => {
    assetRequests.push(new URL(request.url).pathname);
    assert.equal(new URL(request.url).pathname, '/');
    assert.equal(request.headers.get('Cookie'), null);
    assert.equal(request.headers.get('Authorization'), null);
    return new Response(request.method === 'HEAD' ? null : 'PRIVATE_DASHBOARD_SOURCE', { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
  });
  try {
    for (const cookie of [undefined, 'sc_session=invalid', 'sc_session=' + f.outsiderSession]) {
      const denied = await f.call('/dashboard', null, cookie);
      assert.equal(denied.status, 303);
      assert.equal(denied.headers.get('Location'), '/sign-in');
      assert.equal(denied.headers.get('Cache-Control'), 'no-store');
      assert.equal(await denied.text(), '');
    }
    assert.equal(assetRequests.length, 0);
    const login = await f.call('/api/auth/login', { email, password }); assert.equal(login.status, 200);
    const cookie = login.headers.get('Set-Cookie').split(';')[0];
    const dashboard = await f.call('/dashboard', null, cookie);
    assert.equal(dashboard.status, 200); assert.equal(await dashboard.text(), 'PRIVATE_DASHBOARD_SOURCE');
    const head = await f.mf.dispatchFetch(origin + '/dashboard', { method: 'HEAD', headers: { Cookie: cookie } });
    assert.equal(head.status, 200); assert.equal(await head.text(), '');
    assert.deepEqual(assetRequests, ['/', '/']);
    for (const path of ['/sign-in', '/dashboard']) {
      assert.equal((await f.call(path, {})).status, 405);
      assert.equal((await f.call(path + '?next=https://outside.example/')).status, 404);
    }
    const signInHead = await f.mf.dispatchFetch(origin + '/sign-in', { method: 'HEAD' });
    assert.equal(signInHead.status, 200); assert.equal(await signInHead.text(), '');
    for (const alias of ['/sign-in/', '/sign-in.html', '/dashboard/', '/dashboard.html']) assert.equal((await f.call(alias)).status, 403);
    assert.equal((await f.call('/api/auth/logout', {}, cookie)).status, 200);
    assert.equal((await f.call('/dashboard', null, cookie)).status, 303);
  } finally { await f.mf.dispose(); }
});


test('previously valid nonallowlisted sessions, API keys and PC credentials cannot enter private control or consent', async () => {
  const f = await fixture(JSON.stringify([email]));
  try {
    const cookie = 'sc_session=' + f.outsiderSession;
    assert.equal((await f.call('/dashboard', null, cookie)).status, 303);
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
