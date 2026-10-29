import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { Miniflare } from 'miniflare';
import { authStore } from '../src/auth-store.js';
import { hashPassword } from '../legacy-worker.js';

const origin = 'https://stealthcommander.pages.dev';
const callback = 'https://synthetic-client.example/callback';
const digest = value => createHash('sha256').update(value).digest('hex');

// The gateway and SQLite authority are real. Only off-PC network/GUI work is replaced.
async function fixture({ privateMode = 'true', pairPcs = true } = {}) {
  const outgoing = [];
  const identities = [
    { email: 'account-a@example.test', password: 'synthetic-account-a-password', key: 'a' },
    { email: 'account-b@example.test', password: 'synthetic-account-b-password', key: 'b' }
  ];
  const mf = new Miniflare({ workers: [{
    name: 'gateway', modules: true, scriptPath: 'build/worker.js', compatibilityDate: '2026-07-30',
    compatibilityFlags: ['global_fetch_strictly_public'],
    bindings: { GATEWAY_ORIGIN: origin, PRIVATE_MODE: privateMode, PRIVATE_ACCOUNT_ALLOWLIST: privateMode === 'true' ? JSON.stringify([...identities.map(i => i.email), 'not-provisioned@example.test']) : '[]', CONTROL_CSRF_SECRET: 'synthetic-account-boundary-secret-32-chars', BILLING_TEST_ENABLED: 'false' },
    serviceBindings: { AUTH_STORE: 'auth', ASSETS: request => new Response(new URL(request.url).pathname === '/landing' ? 'PUBLIC_INFORMATION' : 'PRIVATE_CONTROL_CENTER'), AUTH_RATE_LIMITER: () => Response.json({ success: true }), REGISTRATION_RATE_LIMITER: () => Response.json({ success: true }) },
    outboundService: async request => {
      const url = new URL(request.url);
      assert.match(url.hostname, /^(account-a-first|account-a-second|account-b)-pc\.trycloudflare\.com$/);
      const body = await request.text();
      outgoing.push({ url: request.url, method: request.method, headers: Object.fromEntries(request.headers), body });
      const rpc = body ? JSON.parse(body) : null;
      return Response.json({ jsonrpc: '2.0', id: rpc?.id ?? 1, result: { pc: url.hostname } });
    }
  }, {
    name: 'auth', modules: true, script: readFileSync(new URL('../../strong-auth-service/worker.js', import.meta.url), 'utf8'),
    compatibilityDate: '2026-07-30', durableObjects: { STORE: { className: 'AuthStore', useSQLite: true } }, kvNamespaces: ['LEGACY_AUTH'],
    outboundService() { throw new Error('External network is disabled.'); }
  }] });

  class Browser {
    cookies = new Map();
    async fetch(path, init = {}) {
      const headers = new Headers(init.headers);
      headers.set('CF-Connecting-IP', '192.0.2.17');
      if (!headers.has('Origin')) headers.set('Origin', origin);
      if (this.cookies.size) headers.set('Cookie', [...this.cookies].map(([key, value]) => `${key}=${value}`).join('; '));
      const response = await mf.dispatchFetch(origin + path, { ...init, headers, redirect: 'manual' });
      for (const cookie of response.headers.getSetCookie()) {
        const [key, value] = cookie.split(';')[0].split('=');
        if (/Max-Age=0(?:;|$)/i.test(cookie)) this.cookies.delete(key); else this.cookies.set(key, value);
      }
      return response;
    }
    json(path, body, headers = {}) {
      return this.fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body) });
    }
    form(path, body) {
      return this.fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams(body).toString() });
    }
  }

  try {
    const store = authStore(await mf.getWorker('auth'));
    await store.initialize();
    const accounts = [];
    for (const identity of identities) {
      const browser = new Browser();
      if (privateMode === 'true') {
        const salt = randomBytes(16).toString('hex');
        const user = { email: identity.email, salt, hash: await hashPassword(identity.password, salt), api_key: 'sc_live_' + identity.key.repeat(40), devices: [] };
        await store.put('user:' + user.email, JSON.stringify(user));
        await store.put('apikey:' + user.api_key, user.email);
      } else {
        const registered = await browser.json('/api/auth/register', { email: identity.email, password: identity.password });
        assert.equal(registered.status, 200);
        assert.deepEqual((await registered.json()).user.devices, []);
      }
      assert.equal((await browser.json('/api/auth/login', { email: identity.email, password: identity.password })).status, 200);
      const session = await (await browser.fetch('/api/account/session')).json();
      accounts.push({ ...identity, browser, csrf: session.csrfToken, pcs: [] });
    }
    const pairPc = async (account, name) => {
      const id = name + '-pc';
      const mcpUrl = `https://${id}.trycloudflare.com/${randomBytes(24).toString('hex')}/mcp`;
      const response = await account.browser.json('/api/devices/pair', { device_id: id, hostname: name, mcp_url: mcpUrl });
      assert.equal(response.status, 200);
      account.pcs.push({ id, mcpUrl, token: (await response.json()).device_token });
    };
    if (pairPcs) for (const [account, name] of [[accounts[0], 'account-a-first'], [accounts[0], 'account-a-second'], [accounts[1], 'account-b']]) await pairPc(account, name);
    const control = (account, body) => account.browser.json('/api/device/control', body, { 'X-Stealth-CSRF': account.csrf });
    const redeem = (pc, record, changes = {}) => mf.dispatchFetch(origin + '/api/devices/authorize', {
      method: 'POST', headers: { Authorization: 'Bearer ' + pc.token, 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticket: record.headers['x-stealth-pc-authorization'], method: record.method, path: new URL(record.url).pathname.replace(/^\/[A-Za-z0-9_-]{32,128}/, ''), body_sha256: digest(record.body), ...changes })
    });
    const mcp = token => mf.dispatchFetch(origin + '/mcp', { method: 'POST', headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 8, method: 'tools/list' }) });
    return { mf, store, outgoing, a: accounts[0], b: accounts[1], anonymous: new Browser(), control, redeem, mcp, pairPc };
  } catch (error) { await mf.dispose(); throw error; }
}

async function oauth(f, account, pc, rejectedDevices = []) {
  const registration = await account.browser.json('/oauth/register', { client_name: 'Synthetic boundary acceptance', redirect_uris: [callback], token_endpoint_auth_method: 'none', grant_types: ['authorization_code', 'refresh_token'], response_types: ['code'] });
  assert.equal(registration.status, 201);
  const client = await registration.json();
  const verifier = randomBytes(48).toString('base64url');
  const query = new URLSearchParams({ response_type: 'code', client_id: client.client_id, redirect_uri: callback, scope: 'pc:control', state: 'synthetic-boundary-state', resource: origin + '/mcp', code_challenge: createHash('sha256').update(verifier).digest('base64url'), code_challenge_method: 'S256' });
  const consent = await account.browser.fetch('/authorize?' + query);
  assert.equal(consent.status, 200);
  const handle = (await consent.text()).match(/name="handle" value="([^"]+)"/)[1];
  for (const deviceId of rejectedDevices) {
    const denied = await account.browser.form('/authorize', { handle, stage: 'consent', decision: 'approve', ...(deviceId === undefined ? {} : { device_id: deviceId }) });
    assert.equal(denied.status, 403);
  }
  const approved = await account.browser.form('/authorize', { handle, stage: 'consent', decision: 'approve', device_id: pc.id });
  assert.equal(approved.status, 302);
  const code = new URL(approved.headers.get('Location')).searchParams.get('code');
  const response = await account.browser.form('/oauth/token', { grant_type: 'authorization_code', client_id: client.client_id, redirect_uri: callback, code, code_verifier: verifier, resource: origin + '/mcp' });
  assert.equal(response.status, 200);
  return response.json();
}

test('private A/B acceptance: public pages stay public; new identities cannot register; existing accounts see only their own PCs', async () => {
  const f = await fixture();
  try {
    assert.equal(await (await f.anonymous.fetch('/')).text(), 'PUBLIC_INFORMATION');
    assert.equal((await f.anonymous.fetch('/sign-in')).status, 200);
    assert.equal((await f.anonymous.fetch('/dashboard')).status, 303);
    assert.equal((await f.anonymous.json('/api/auth/register', { email: 'not-provisioned@example.test', password: 'synthetic-unverified-password' })).status, 403);
    assert.equal(await f.store.get('user:not-provisioned@example.test'), null);
    assert.equal((await f.anonymous.json('/api/auth/login', { email: f.a.email, password: f.b.password })).status, 401);
    for (const [account, expected, forbidden] of [[f.a, ['account-a-second-pc', 'account-a-first-pc'], f.b.email], [f.b, ['account-b-pc'], f.a.email]]) {
      const response = await account.browser.fetch('/api/auth/me');
      assert.equal(response.status, 200);
      const body = await response.json();
      assert.equal(body.user.email, account.email);
      assert.deepEqual(body.user.devices.map(pc => pc.device_id), expected);
      assert.ok(!JSON.stringify(body).includes(forbidden));
      assert.equal(await (await account.browser.fetch('/dashboard')).text(), 'PRIVATE_CONTROL_CENTER');
    }
    assert.equal((await f.a.browser.fetch('/api/billing/status')).status, 503);
  } finally { await f.mf.dispose(); }
});

test('private A/B acceptance: dashboard control rejects another account and missing selectors without choosing a fallback PC', async () => {
  const f = await fixture();
  try {
    const selected = { device_id: f.a.pcs[1].id, method: 'GET', path: '/meta' };
    assert.equal((await f.control(f.a, selected)).status, 200);
    assert.equal(new URL(f.outgoing.at(-1).url).hostname, 'account-a-second-pc.trycloudflare.com');
    assert.equal(f.outgoing.at(-1).headers.authorization, undefined);
    assert.equal(f.outgoing.at(-1).headers.cookie, undefined);
    const before = f.outgoing.length;
    assert.equal((await f.control(f.a, { ...selected, device_id: f.b.pcs[0].id })).status, 401);
    assert.equal((await f.control(f.b, { ...selected })).status, 401);
    assert.equal((await f.control(f.a, { method: 'GET', path: '/meta' })).status, 400);
    assert.equal((await f.control(f.a, { ...selected, device_id: '' })).status, 401);
    assert.equal((await f.a.browser.json('/api/mcp', { jsonrpc: '2.0', id: 1, method: 'tools/list' })).status, 400);
    assert.equal(f.outgoing.length, before);
  } finally { await f.mf.dispose(); }
});

test('private A/B acceptance: OAuth stays bound to the selected PC and does not fall back after unlink', async () => {
  const f = await fixture();
  try {
    const aTokens = await oauth(f, f.a, f.a.pcs[1], [f.b.pcs[0].id, undefined]);
    const bTokens = await oauth(f, f.b, f.b.pcs[0], [f.a.pcs[0].id]);
    const response = await f.mcp(aTokens.access_token);
    assert.equal(response.status, 200);
    assert.equal((await response.json()).result.pc, 'account-a-second-pc.trycloudflare.com');
    assert.equal((await f.redeem(f.b.pcs[0], f.outgoing.at(-1))).status, 401);
    const unlinked = await f.a.browser.fetch('/api/account/devices', { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ device_id: f.a.pcs[1].id }) });
    assert.equal(unlinked.status, 200);
    assert.deepEqual((await unlinked.json()).devices.map(pc => pc.device_id), ['account-a-first-pc']);
    const before = f.outgoing.length;
    assert.equal((await f.mcp(aTokens.access_token)).status, 401);
    assert.equal(f.outgoing.length, before);
    assert.equal((await f.mcp(bTokens.access_token)).status, 200);
    assert.equal(new URL(f.outgoing.at(-1).url).hostname, 'account-b-pc.trycloudflare.com');
  } finally { await f.mf.dispose(); }
});

test('private A/B acceptance: PC tickets bind exact device, method, path and body and cannot replay', async () => {
  const f = await fixture();
  try {
    const selected = f.a.pcs[1];
    assert.equal((await f.control(f.a, { device_id: selected.id, method: 'POST', path: '/action', payload: { kind: 'doctor' } })).status, 200);
    const record = f.outgoing.at(-1);
    assert.equal((await f.redeem(f.b.pcs[0], record)).status, 401);
    assert.equal((await f.redeem(f.a.pcs[0], record)).status, 401);
    for (const changes of [{ method: 'GET' }, { path: '/meta' }, { body_sha256: digest('{"kind":"different"}') }]) assert.equal((await f.redeem(selected, record, changes)).status, 401);
    assert.equal((await f.redeem(selected, record)).status, 200);
    assert.equal((await f.redeem(selected, record)).status, 401);
  } finally { await f.mf.dispose(); }
});

test('private A/B acceptance: logging A out revokes its pending ticket while B remains authorized', async () => {
  const f = await fixture();
  try {
    const aRequest = { device_id: f.a.pcs[0].id, method: 'GET', path: '/meta' };
    const bRequest = { device_id: f.b.pcs[0].id, method: 'GET', path: '/meta' };
    assert.equal((await f.control(f.a, aRequest)).status, 200); const aTicket = f.outgoing.at(-1);
    assert.equal((await f.control(f.b, bRequest)).status, 200); const bTicket = f.outgoing.at(-1);
    assert.equal((await f.a.browser.json('/api/auth/logout', {})).status, 200);
    assert.equal((await f.redeem(f.a.pcs[0], aTicket)).status, 401);
    assert.equal((await f.control(f.a, aRequest)).status, 401);
    assert.equal((await f.redeem(f.b.pcs[0], bTicket)).status, 200);
    assert.equal((await f.control(f.b, bRequest)).status, 200);
  } finally { await f.mf.dispose(); }
});

test('private A/B acceptance: rotating A key revokes its session, pending ticket and OAuth without affecting B', async () => {
  const f = await fixture();
  try {
    const aTokens = await oauth(f, f.a, f.a.pcs[0]);
    const bTokens = await oauth(f, f.b, f.b.pcs[0]);
    assert.equal((await f.control(f.a, { device_id: f.a.pcs[0].id, method: 'GET', path: '/meta' })).status, 200);
    const pending = f.outgoing.at(-1);
    assert.equal((await f.a.browser.json('/api/account/rotate-key', {})).status, 200);
    assert.equal((await f.a.browser.fetch('/api/auth/me')).status, 401);
    assert.equal((await f.redeem(f.a.pcs[0], pending)).status, 401);
    assert.equal((await f.mcp(aTokens.access_token)).status, 401);
    assert.equal((await f.b.browser.fetch('/api/auth/me')).status, 200);
    assert.equal((await f.mcp(bTokens.access_token)).status, 200);
  } finally { await f.mf.dispose(); }
});

test('public A/B registration preserves account ownership and starts with no PC access', async () => {
  const f = await fixture({ privateMode: 'false', pairPcs: false });
  try {
    assert.equal((await f.anonymous.fetch('/sign-up')).status, 200);
    for (const account of [f.a, f.b]) {
      const me = await account.browser.fetch('/api/auth/me');
      assert.equal(me.status, 200);
      const body = await me.json();
      assert.equal(body.user.email, account.email);
      assert.deepEqual(body.user.devices, []);
      assert.equal((await account.browser.fetch('/dashboard')).status, 200);
      assert.equal((await f.control(account, { device_id: 'unpaired-pc', method: 'GET', path: '/meta' })).status, 401);
    }
    assert.equal(f.outgoing.length, 0);
    const before = await f.store.get('user:' + f.b.email);
    const duplicate = await f.anonymous.json('/api/auth/register', { email: f.b.email.toUpperCase(), password: 'different-synthetic-password' });
    assert.equal(duplicate.status, 409);
    assert.equal(duplicate.headers.get('Set-Cookie'), null);
    assert.equal(await f.store.get('user:' + f.b.email), before);
    assert.equal((await f.b.browser.json('/api/auth/logout', {})).status, 200);
    assert.equal((await f.b.browser.fetch('/api/auth/me')).status, 401);
    assert.equal((await f.b.browser.json('/api/auth/login', { email: f.b.email, password: 'different-synthetic-password' })).status, 401);
    assert.equal((await f.b.browser.json('/api/auth/login', { email: f.b.email, password: f.b.password })).status, 200);
    f.b.csrf = (await (await f.b.browser.fetch('/api/account/session')).json()).csrfToken;
    await f.pairPc(f.a, 'account-a-first');
    await f.pairPc(f.b, 'account-b');
    assert.equal((await f.control(f.a, { device_id: f.a.pcs[0].id, method: 'GET', path: '/meta' })).status, 200);
    const sent = f.outgoing.length;
    assert.equal((await f.control(f.a, { device_id: f.b.pcs[0].id, method: 'GET', path: '/meta' })).status, 401);
    assert.equal((await f.control(f.b, { device_id: f.a.pcs[0].id, method: 'GET', path: '/meta' })).status, 401);
    assert.equal(f.outgoing.length, sent);
    assert.equal((await f.a.browser.fetch('/api/billing/status')).status, 503);
  } finally { await f.mf.dispose(); }
});

test('public mode exposes only explicit public routes and keeps dashboard asset aliases authenticated', async () => {
  const f = await fixture({ privateMode: 'false', pairPcs: false });
  try {
    assert.equal(await (await f.anonymous.fetch('/')).text(), 'PUBLIC_INFORMATION');
    assert.equal((await f.anonymous.fetch('/sign-in')).status, 200);
    assert.equal((await f.anonymous.fetch('/sign-up')).status, 200);
    assert.equal((await f.anonymous.fetch('/sign-up?ignored=1')).status, 404);
    assert.equal((await f.anonymous.fetch('/sign-up', { method: 'POST' })).status, 405);
    const head = await f.anonymous.fetch('/sign-up', { method: 'HEAD' });
    assert.equal(head.status, 200);
    assert.equal(await head.text(), '');
    assert.equal((await f.anonymous.fetch('/dashboard')).status, 303);
    for (const path of ['/index.html', '/registry.json', '/download/installer.zip', '/oauth/unknown', '/dashboard/']) {
      const denied = await f.anonymous.fetch(path);
      assert.equal(denied.status, 403);
      assert.ok(!(await denied.text()).includes('PRIVATE_CONTROL_CENTER'));
    }
    assert.equal((await f.anonymous.fetch('/api/auth/me')).status, 401);
    assert.equal((await f.anonymous.fetch('/api/account/session')).status, 401);
    assert.equal((await f.anonymous.json('/api/device/control', { device_id: 'unpaired-pc', method: 'GET', path: '/meta' })).status, 401);
    assert.equal((await f.anonymous.json('/api/auth/login', { email: f.a.email, password: f.a.password }, { Origin: 'https://untrusted.example' })).status, 403);
    assert.equal((await f.anonymous.json('/api/auth/register', { email: 'untrusted-origin@example.test', password: 'synthetic-origin-password' }, { Origin: 'https://untrusted.example' })).status, 403);
    assert.equal(await f.store.get('user:untrusted-origin@example.test'), null);
    assert.equal(await (await f.a.browser.fetch('/dashboard')).text(), 'PRIVATE_CONTROL_CENTER');
    assert.equal(await (await f.b.browser.fetch('/index.html')).text(), 'PRIVATE_CONTROL_CENTER');
  } finally { await f.mf.dispose(); }
});
