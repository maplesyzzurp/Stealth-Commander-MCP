import { before, after, test } from 'node:test';
import assert from 'node:assert/strict';
import { pbkdf2Sync, createHash, randomBytes } from 'node:crypto';
import { Miniflare, Response } from 'miniflare';
import { readFileSync } from 'node:fs';
import { authStore } from '../src/auth-store.js';
import { UPSTREAM_TIMEOUT_MS } from '../src/limits.js';

const ORIGIN = 'https://stealthcommander.pages.dev';
const CALLBACK = 'https://client.example/callback';
const PASSWORD = 'synthetic-password-only';
let mf, users, oauth, upstreamCalls = [], assetRequests = [], upstreamMode = 'json', sequence = 0;
let browserSequence = 0;

function fixedTestWindowLimiter() {
  const counts = new Map();
  // Each test uses fresh account/source identities. Keep their ten-request
  // window fixed so a real minute rollover cannot reset a test mid-assertion.
  return async request => {
    assert.equal(request.method, 'POST');
    const { key } = await request.json();
    assert.match(key, /^(signin|dcr):(source|account):[a-f0-9]{64}$/);
    const count = (counts.get(key) || 0) + 1;
    counts.set(key, count);
    return Response.json({ success: count <= 10 });
  };
}

before(async () => {
  mf = new Miniflare({ workers: [{
    name: 'gateway', modules: true, scriptPath: 'build/worker.js', compatibilityDate: '2026-07-30', compatibilityFlags: ['global_fetch_strictly_public'],
    bindings: { GATEWAY_ORIGIN: ORIGIN, LEGACY_MCP_URL_KEYS_ENABLED: 'true', CONTROL_CSRF_SECRET: 'synthetic-control-secret-at-least-32-chars' }, kvNamespaces: ['STEALTH_AUTH', 'OAUTH_KV'],
    serviceBindings: { ASSETS: request => { assetRequests.push(request.url); return new Response('synthetic website'); }, AUTH_STORE: 'auth-store', AUTH_RATE_LIMITER: fixedTestWindowLimiter(), REGISTRATION_RATE_LIMITER: fixedTestWindowLimiter() },
    outboundService: async request => {
      upstreamCalls.push({ url: request.url, method: request.method, headers: Object.fromEntries(request.headers), body: await request.text() });
      const url = new URL(request.url);
      if (request.url === 'https://client.example/metadata.json') return Response.json({ client_id: request.url, client_name: 'Synthetic CIMD client', redirect_uris: [CALLBACK], token_endpoint_auth_method: 'none' });
      assert.match(url.hostname, /^(alice|bob|moved)-pc\.trycloudflare\.com$/);
      if (upstreamMode === 'redirect') return new Response(null, { status: 302, headers: { Location: 'https://elsewhere.invalid/' } });
      if (upstreamMode === 'empty') return new Response(null, { status: 202 });
      if (upstreamMode === 'html') return new Response('<script>unsafe</script>', { headers: { 'Content-Type': 'text/html' } });
      if (upstreamMode === 'oversize') return new Response('x'.repeat(8 * 1024 * 1024 + 1), { headers: { 'Content-Type': 'application/json' } });
      if (upstreamMode === 'sse') return new Response('data: {}\n\n', { headers: { 'Content-Type': 'text/event-stream' } });
      if (upstreamMode === 'delayed') await new Promise(resolve => setTimeout(resolve, 30));
      if (['public-metadata', 'public-error', 'command-output'].includes(upstreamMode)) {
        const rpc = JSON.parse(upstreamCalls.at(-1).body);
        if (rpc.method === 'tools/list') return Response.json({ jsonrpc: '2.0', id: rpc.id, result: { tools: [{ name: 'dashboard_status' }, { name: 'start_tunnel' }, { name: 'run_command' }] } });
        const secretUrl = `https://alice-pc.trycloudflare.com/${'a'.repeat(48)}/mcp`;
        const payload = upstreamMode === 'command-output' ? { ok: false, stdout: `ordinary stdout ${secretUrl}`, stderr: 'ordinary error', returncode: 2 } : { ok: upstreamMode !== 'public-error', dashboard_url: `http://127.0.0.1:61076/${'s'.repeat(32)}`, token: 'synthetic-server-token', error: upstreamMode === 'public-error' ? 'Synthetic tool error survives.' : undefined, result: { mcp_url: secretUrl, desktops: [{ name: 'Desktop A', url: secretUrl }] } };
        return Response.json({ jsonrpc: '2.0', id: rpc.id, result: { content: [{ type: 'text', text: JSON.stringify(payload) }], structuredContent: payload, isError: !payload.ok } });
      }
      return Response.json({ jsonrpc: '2.0', id: 1, result: { pc: url.hostname } }, { headers: { 'Mcp-Session-Id': 'synthetic-session' } });
    }
  }, { name: 'auth-store', modules: true, script: readFileSync(new URL('../../strong-auth-service/worker.js', import.meta.url), 'utf8'), compatibilityDate: '2026-07-30', durableObjects: { STORE: { className: 'AuthStore', useSQLite: true } }, kvNamespaces: ['LEGACY_AUTH', 'LEGACY_OAUTH'], outboundService: () => { throw new Error('No external network.'); } }] });
  const service = await mf.getWorker('auth-store');
  const facade = prefix => { const store = authStore(service, prefix); return { ...store, get: (key, options) => store.get(key, options === 'json' ? { type: 'json' } : options) }; };
  users = facade(''); oauth = facade('oauth:');
  await users.initialize();
});
after(async () => { await mf?.dispose(); });

test('an enrolled PC capability cannot be claimed, published or reached through another account', async () => {
  const f = await setup();
  const tokens = await grant(f);
  assert.equal((await mcp(tokens.access_token)).status, 200);
  const call = (path, user, body) => f.browser.fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: ORIGIN, Authorization: 'Bearer ' + user.api_key }, body: JSON.stringify(body) });
  const owned = { device_id: 'bob-pc', hostname: 'Bob PC', mcp_url: f.bob.devices[0].mcp_url };
  const paired = await call('/api/devices/pair', f.bob, owned);
  assert.equal(paired.status, 200);
  const deviceToken = (await paired.json()).device_token;
  const stolen = { ...owned, mcp_url: f.alice.devices[0].mcp_url };
  assert.equal((await call('/api/devices/pair', f.bob, stolen)).status, 409);
  assert.equal((await call('/api/account/devices', f.bob, stolen)).status, 409);
  assert.equal((await f.browser.fetch('/api/devices/heartbeat', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + deviceToken }, body: JSON.stringify(stolen) })).status, 409);
  const storedBob = JSON.parse(await users.get('user:' + f.bob.email));
  storedBob.devices[0].mcp_url = stolen.mcp_url;
  await users.put('user:' + f.bob.email, JSON.stringify(storedBob));
  const count = upstreamCalls.length;
  assert.equal((await call('/api/mcp/' + f.bob.api_key + '/bob-pc', f.bob, { jsonrpc: '2.0', id: 1, method: 'tools/list' })).status, 403);
  assert.equal(upstreamCalls.length, count);
  assert.equal((await mcp(tokens.access_token)).status, 200);
});

test('the deployed gateway fails closed when the strong account store binding is absent', async () => {
  const absent = new Miniflare({ modules: true, scriptPath: 'build/worker.js', compatibilityDate: '2026-07-30', bindings: { GATEWAY_ORIGIN: ORIGIN }, kvNamespaces: ['STEALTH_AUTH', 'OAUTH_KV'], serviceBindings: { ASSETS: () => new Response('synthetic') } });
  try { assert.equal((await absent.dispatchFetch(ORIGIN + '/api/auth/me')).status, 503); }
  finally { await absent.dispose(); }
});

class Browser {
  cookies = new Map();
  ip = '203.0.113.' + (++browserSequence);
  async fetch(path, init = {}) {
    const headers = new Headers(init.headers);
    if (!headers.has('CF-Connecting-IP')) headers.set('CF-Connecting-IP', this.ip);
    if (this.cookies.size) headers.set('Cookie', [...this.cookies].map(([k, v]) => `${k}=${v}`).join('; '));
    const response = await mf.dispatchFetch(new URL(path, ORIGIN).href, { ...init, headers, redirect: 'manual' });
    for (const value of response.headers.getSetCookie()) {
      const [name, content] = value.split(';')[0].split('=');
      if (/Max-Age=0(?:;|$)/i.test(value)) this.cookies.delete(name); else this.cookies.set(name, content);
    }
    return response;
  }
  post(form, headers = {}) {
    return this.fetch('/authorize', { method: 'POST', headers: { Origin: ORIGIN, 'Content-Type': 'application/x-www-form-urlencoded', ...headers }, body: new URLSearchParams(form).toString() });
  }
}

async function setup() {
  const n = ++sequence;
  const alice = { email: `alice${n}@example.test`, name: 'Alice', salt: randomBytes(16).toString('hex'), api_key: 'sc_live_' + randomBytes(20).toString('hex'), devices: [{ device_id: 'alice-pc', hostname: 'Alice PC', mcp_url: `https://alice-pc.trycloudflare.com/${randomBytes(24).toString('hex')}/mcp`, oauth_pair_id: randomBytes(24).toString('hex') }] };
  alice.hash = pbkdf2Sync(PASSWORD, alice.salt, 100000, 32, 'sha256').toString('hex');
  const bob = { ...alice, email: `bob${n}@example.test`, api_key: 'sc_live_' + randomBytes(20).toString('hex'), devices: [{ device_id: 'bob-pc', hostname: 'Bob PC', mcp_url: `https://bob-pc.trycloudflare.com/${randomBytes(24).toString('hex')}/mcp`, oauth_pair_id: randomBytes(24).toString('hex') }] };
  for (const user of [alice, bob]) {
    const token = 'sc_dev_' + randomBytes(24).toString('hex');
    const hash = createHash('sha256').update(token).digest('hex');
    user.devices[0].device_credential_hash = hash;
    await users.put('deviceauth:' + hash, JSON.stringify({ email: user.email, deviceId: user.devices[0].device_id, pairId: user.devices[0].oauth_pair_id, accountKeyHash: createHash('sha256').update(user.api_key).digest('hex'), expiresAt: Date.now() + 2592000000 }));
    await users.put('user:' + user.email, JSON.stringify(user)); await users.put('apikey:' + user.api_key, user.email);
  }
  const browser = new Browser();
  const session = 'sc_sess_' + randomBytes(24).toString('hex');
  await users.put('session:' + session, JSON.stringify({ email: alice.email, expiresAt: Date.now() + 43200000, accountKeyHash: createHash('sha256').update(alice.api_key).digest('hex') }));
  browser.cookies.set('sc_session', session);
  const registration = await browser.fetch('/oauth/register', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_name: 'Synthetic client <script>', redirect_uris: [CALLBACK], token_endpoint_auth_method: 'none', grant_types: ['authorization_code', 'refresh_token'], response_types: ['code'] }) });
  assert.equal(registration.status, 201);
  const client = await registration.json();
  const verifier = randomBytes(48).toString('base64url');
  const query = new URLSearchParams({ response_type: 'code', client_id: client.client_id, redirect_uri: CALLBACK, scope: 'pc:control', state: 'synthetic-state', resource: ORIGIN + '/mcp', code_challenge: createHash('sha256').update(verifier).digest('base64url'), code_challenge_method: 'S256' });
  return { alice, bob, browser, client, verifier, query };
}

async function openConsent(flow, browser = flow.browser) {
  const response = await browser.fetch('/authorize?' + flow.query);
  assert.equal(response.status, 200);
  const html = await response.text();
  const handle = html.match(/name="handle" value="([^"]+)"/)[1];
  return { response, html, handle };
}
async function issueCode(flow) {
  const { handle } = await openConsent(flow);
  const response = await flow.browser.post({ handle, stage: 'consent', decision: 'approve', device_id: 'alice-pc' });
  assert.equal(response.status, 302);
  const redirect = new URL(response.headers.get('Location'));
  assert.equal(redirect.origin, new URL(CALLBACK).origin);
  assert.equal(redirect.searchParams.get('state'), 'synthetic-state');
  assert.equal(redirect.searchParams.get('iss'), ORIGIN);
  return redirect.searchParams.get('code');
}
async function exchange(flow, code, extra = {}) {
  return flow.browser.fetch('/oauth/token', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ grant_type: 'authorization_code', client_id: flow.client.client_id, redirect_uri: CALLBACK, code, code_verifier: flow.verifier, resource: ORIGIN + '/mcp', ...extra }).toString() });
}
async function grant(flow) {
  const response = await exchange(flow, await issueCode(flow));
  assert.equal(response.status, 200);
  const tokens = await response.json();
  assert.equal(tokens.scope, 'pc:control');
  assert.equal(tokens.expires_in, 300);
  return tokens;
}
function mcp(token, extra = {}) {
  return mf.dispatchFetch(ORIGIN + '/mcp', { method: 'POST', headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: 'Bearer ' + token } : {}), ...extra.headers }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list' }), ...extra, redirect: 'manual' });
}
async function refresh(flow, token, extra = {}) {
  return flow.browser.fetch('/oauth/token', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ grant_type: 'refresh_token', client_id: flow.client.client_id, refresh_token: token, resource: ORIGIN + '/mcp', ...extra }).toString() });
}

test('discovery and unauthenticated challenge publish a universal resource without account or PC secrets', async () => {
  const response = await mcp();
  assert.equal(response.status, 401);
  assert.match(response.headers.get('WWW-Authenticate'), /oauth-protected-resource/);
  const metadata = await (await mf.dispatchFetch(ORIGIN + '/.well-known/oauth-protected-resource/mcp')).json();
  assert.equal(metadata.resource, ORIGIN + '/mcp');
  assert.deepEqual(metadata.scopes_supported, ['pc:control']);
  const auth = await (await mf.dispatchFetch(ORIGIN + '/.well-known/oauth-authorization-server')).json();
  assert.equal(auth.authorization_endpoint, ORIGIN + '/authorize');
  assert.equal(auth.registration_endpoint, ORIGIN + '/oauth/register');
  assert.ok(auth.code_challenge_methods_supported.includes('S256'));
  assert.ok(!/sc_live_|trycloudflare|@example/.test(JSON.stringify([metadata, auth])));
});

test('authorization code, PKCE token exchange and MCP forwarding select exactly one owned PC', async () => {
  const f = await setup(); const tokens = await grant(f); const start = upstreamCalls.length;
  const response = await mcp(tokens.access_token);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).result.pc, 'alice-pc.trycloudflare.com');
  assert.equal(upstreamCalls.length, start + 1);
  assert.equal(upstreamCalls.at(-1).headers.authorization, undefined);
  assert.equal(upstreamCalls.at(-1).headers.cookie, undefined);
  assert.equal(response.headers.get('Mcp-Session-Id'), 'synthetic-session');
});

test('sign-in uses the existing account, then requires explicit PC consent', async () => {
  const f = await setup(); f.browser.cookies.clear();
  const login = await openConsent(f);
  assert.match(login.html, /Sign in to Stealth Commander/);
  const response = await f.browser.post({ handle: login.handle, stage: 'signin', decision: 'signin', email: f.alice.email, password: PASSWORD });
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /Allow access to one PC/);
  assert.match(html, /Choose a PC/);
  assert.ok(!html.includes(f.alice.api_key));
  const handle = html.match(/name="handle" value="([^"]+)"/)[1];
  const approved = await f.browser.post({ handle, stage: 'consent', decision: 'approve', device_id: 'alice-pc' });
  assert.equal(approved.status, 302);
  assert.equal((await exchange(f, new URL(approved.headers.get('Location')).searchParams.get('code'))).status, 200);
});

test('a CIMD client authorizes through its official-library metadata flow', async () => {
  const f = await setup();
  f.client = { client_id: 'https://client.example/metadata.json' };
  f.query.set('client_id', f.client.client_id);
  const { html } = await openConsent(f);
  assert.match(html, /Client domain: client.example/);
  const tokens = await grant(f);
  assert.equal((await mcp(tokens.access_token)).status, 200);
});

test('consent HTML escapes client metadata, forbids framing, explains control and hides account/device keys', async () => {
  const f = await setup(); const { response, html } = await openConsent(f);
  assert.match(html, /Synthetic client &#60;script&#62;/);
  assert.ok(!html.includes('<script>'));
  assert.match(html, /full control/);
  assert.match(html, /client.example/);
  assert.equal(response.headers.get('X-Frame-Options'), 'DENY');
  assert.match(response.headers.get('Content-Security-Policy'), /frame-ancestors 'none'/);
  for (const secret of [f.alice.api_key, f.alice.devices[0].mcp_url, f.alice.devices[0].oauth_pair_id]) assert.ok(!html.includes(secret));
});

test('a PC owned by another account and an omitted PC are denied', async () => {
  const f = await setup(); const { handle } = await openConsent(f);
  for (const device_id of ['bob-pc', 'missing', '']) assert.equal((await f.browser.post({ handle, stage: 'consent', decision: 'approve', device_id })).status, 403);
});

test('missing/wrong bearer tokens and account API keys cannot enter /mcp', async () => {
  const f = await setup(); const count = upstreamCalls.length;
  for (const token of [undefined, 'invalid', f.alice.api_key]) assert.equal((await mcp(token)).status, 401);
  assert.equal(upstreamCalls.length, count);
});

test('wrong PKCE verifier and authorization-code replay fail', async () => {
  const f = await setup(); const code = await issueCode(f);
  assert.equal((await exchange(f, code, { code_verifier: 'x'.repeat(64) })).status, 400);
  const f2 = await setup(); const code2 = await issueCode(f2);
  assert.equal((await exchange(f2, code2)).status, 200);
  assert.equal((await exchange(f2, code2)).status, 400);
});

test('public clients require S256 PKCE and exact registered redirect URIs', async () => {
  const f = await setup();
  for (const [key, value] of [['code_challenge_method', 'plain'], ['redirect_uri', 'https://attacker.example/callback']]) {
    const query = new URLSearchParams(f.query); query.set(key, value);
    const response = await f.browser.fetch('/authorize?' + query);
    assert.ok(response.status === 400 || response.status === 302);
    if (response.status === 302) assert.equal(new URL(response.headers.get('Location')).origin, new URL(CALLBACK).origin);
  }
  f.query.delete('code_challenge'); f.query.delete('code_challenge_method');
  const r = await f.browser.fetch('/authorize?' + f.query);
  assert.ok(r.status === 400 || (r.status === 302 && new URL(r.headers.get('Location')).searchParams.has('error')));
});

test('consent is bound to browser, origin, user session and one use', async () => {
  const f = await setup(); const { handle } = await openConsent(f); const form = { handle, stage: 'consent', decision: 'approve', device_id: 'alice-pc' };
  const outsider = new Browser(); outsider.cookies.set('sc_session', f.browser.cookies.get('sc_session'));
  assert.equal((await outsider.post(form)).status, 400);
  assert.equal((await f.browser.post(form, { Origin: 'https://attacker.example' })).status, 403);
  assert.equal((await f.browser.post(form, { Origin: '' })).status, 403);
  const session = 'sc_sess_' + randomBytes(24).toString('hex'); await users.put('session:' + session, JSON.stringify({ email: f.bob.email, expiresAt: Date.now() + 43200000, accountKeyHash: createHash('sha256').update(f.bob.api_key).digest('hex') }));
  const old = f.browser.cookies.get('sc_session'); f.browser.cookies.set('sc_session', session);
  assert.equal((await f.browser.post(form)).status, 403);
  f.browser.cookies.set('sc_session', old);
  assert.equal((await f.browser.post(form)).status, 302);
  assert.equal((await f.browser.post(form)).status, 403);
});

test('consent tampering cannot switch client or redirect destination', async () => {
  const f = await setup(); const { handle } = await openConsent(f);
  const response = await f.browser.post({ handle, stage: 'consent', decision: 'approve', device_id: 'alice-pc', client_id: 'evil', redirect_uri: 'https://attacker.example/', resource: 'https://attacker.example/mcp' });
  assert.equal(response.status, 302);
  assert.equal(new URL(response.headers.get('Location')).origin, new URL(CALLBACK).origin);
});

test('cancel returns access_denied only to the registered client', async () => {
  const f = await setup(); const { handle } = await openConsent(f);
  const response = await f.browser.post({ handle, decision: 'deny' });
  assert.equal(response.status, 302);
  const url = new URL(response.headers.get('Location'));
  assert.equal(url.origin, new URL(CALLBACK).origin);
  assert.equal(url.searchParams.get('error'), 'access_denied');
});

test('resource audience cannot be changed during token exchange', async () => {
  const f = await setup();
  assert.equal((await exchange(f, await issueCode(f), { resource: 'https://different.example/mcp' })).status, 400);
});

test('unlinked PC blocks existing access and refresh tokens without target fallback', async () => {
  const f = await setup(); const tokens = await grant(f); const count = upstreamCalls.length;
  f.alice.devices = f.bob.devices; await users.put('user:' + f.alice.email, JSON.stringify(f.alice));
  assert.equal((await mcp(tokens.access_token)).status, 401);
  assert.equal((await refresh(f, tokens.refresh_token)).status, 400);
  assert.equal(upstreamCalls.length, count);
});

test('account key rotation, device capability rekey and re-pair invalidate old grants', async () => {
  for (const change of ['account', 'capability', 'pair']) {
    const f = await setup(); const tokens = await grant(f);
    if (change === 'account') f.alice.api_key = 'sc_live_' + randomBytes(20).toString('hex');
    if (change === 'capability') f.alice.devices[0].mcp_url = f.alice.devices[0].mcp_url.replace(new URL(f.alice.devices[0].mcp_url).pathname.split('/')[1], randomBytes(24).toString('hex'));
    if (change === 'pair') f.alice.devices[0].oauth_pair_id = randomBytes(24).toString('hex');
    await users.put('user:' + f.alice.email, JSON.stringify(f.alice));
    assert.equal((await mcp(tokens.access_token)).status, 401);
    assert.equal((await refresh(f, tokens.refresh_token)).status, 400);
  }
});

test('a renewed tunnel address uses the current endpoint for the same pairing capability', async () => {
  const f = await setup(); const tokens = await grant(f);
  f.alice.devices[0].mcp_url = f.alice.devices[0].mcp_url.replace('alice-pc.', 'moved-pc.');
  await users.put('user:' + f.alice.email, JSON.stringify(f.alice));
  const r = await mcp(tokens.access_token); assert.equal(r.status, 200);
  assert.equal((await r.json()).result.pc, 'moved-pc.trycloudflare.com');
});

test('token revocation and expired token records prevent access', async () => {
  const f = await setup(); const tokens = await grant(f);
  const metadata = await (await mf.dispatchFetch(ORIGIN + '/.well-known/oauth-authorization-server')).json();
  const revoke = await f.browser.fetch(metadata.revocation_endpoint, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ client_id: f.client.client_id, token: tokens.refresh_token }).toString() });
  assert.equal(revoke.status, 200);
  assert.equal((await mcp(tokens.access_token)).status, 401);
  const f2 = await setup(); const tokens2 = await grant(f2);
  for (const key of (await oauth.list({ prefix: 'token:' + f2.alice.email + ':' })).keys) {
    const record = await oauth.get(key.name, 'json'); record.expiresAt = Math.floor(Date.now() / 1000) - 1; await oauth.put(key.name, JSON.stringify(record));
  }
  assert.equal((await mcp(tokens2.access_token)).status, 401);
});

test('refresh issues a usable access token and rejects another client', async () => {
  const f = await setup(); const tokens = await grant(f); const other = await setup();
  assert.equal((await refresh(f, tokens.refresh_token, { client_id: other.client.client_id })).status, 400);
  const response = await refresh(f, tokens.refresh_token); assert.equal(response.status, 200);
  assert.equal((await mcp((await response.json()).access_token)).status, 200);
});

test('an orphaned access token cannot outlive a removed or expired grant', async () => {
  for (const change of ['remove', 'expire']) {
    const f = await setup(); const tokens = await grant(f);
    const keys = (await oauth.list({ prefix: 'grant:' + f.alice.email + ':' })).keys;
    assert.equal(keys.length, 1);
    if (change === 'remove') await oauth.delete(keys[0].name);
    else { const stored = await oauth.get(keys[0].name, 'json'); stored.expiresAt = Math.floor(Date.now() / 1000) - 1; await oauth.put(keys[0].name, JSON.stringify(stored)); }
    assert.equal((await mcp(tokens.access_token)).status, 401);
  }
});

test('universal endpoint refuses user-controlled targets, hostile origins and unsafe upstream URLs', async () => {
  const f = await setup(); const tokens = await grant(f);
  const assetsBefore = assetRequests.length, upstreamBefore = upstreamCalls.length;
  for (const [path, status] of [['/mcp/bob-pc', 403], ['/mcp?device_id=bob-pc', 404]]) {
    const denied = await mf.dispatchFetch(ORIGIN + path, { headers: { Authorization: 'Bearer ' + tokens.access_token } });
    assert.equal(denied.status, status);
    assert.ok(!(await denied.text()).includes('synthetic website'));
  }
  assert.equal(assetRequests.length, assetsBefore);
  assert.equal(upstreamCalls.length, upstreamBefore);
  assert.equal((await mcp(tokens.access_token, { headers: { Authorization: 'Bearer ' + tokens.access_token, Origin: 'https://evil.example', 'Content-Type': 'application/json' } })).status, 403);
  for (const endpoint of ['http://127.0.0.1/mcp', 'https://evil.example/mcp', 'https://alice-pc.trycloudflare.com/short/mcp', f.alice.devices[0].mcp_url + '?redirect=x']) {
    f.alice.devices[0].mcp_url = endpoint; await users.put('user:' + f.alice.email, JSON.stringify(f.alice));
    assert.equal((await mcp(tokens.access_token)).status, 401);
  }
});

test('forwarding blocks redirects, unsupported response types and oversized replies; preserves 202', async () => {
  const f = await setup(); const tokens = await grant(f);
  try {
    for (const [mode, status] of [['redirect', 502], ['html', 502], ['sse', 502], ['oversize', 502], ['empty', 202]]) { upstreamMode = mode; assert.equal((await mcp(tokens.access_token)).status, status); }
  } finally { upstreamMode = 'json'; }
});

test('JSON validation and request limit apply before the device is called', async () => {
  const f = await setup(); const tokens = await grant(f); const count = upstreamCalls.length;
  for (const [body, status] of [['invalid', 400], ['[]', 400], ['x'.repeat(1024 * 1024 + 1), 413]]) assert.equal((await mcp(tokens.access_token, { body })).status, status);
  assert.equal(upstreamCalls.length, count);
});

test('OAuth protocol bodies are bounded before library parsing', async () => {
  for (const path of ['/oauth/token', '/oauth/register']) {
    const r = await mf.dispatchFetch(ORIGIN + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: 'x'.repeat(16385) });
    assert.equal(r.status, 413);
  }
});

test('expired consent fails even while its saved browser handle still exists', async () => {
  const f = await setup(); const { handle } = await openConsent(f);
  const key = 'sc:consent:' + createHash('sha256').update(handle).digest('hex');
  const saved = await oauth.get(key, 'json'); saved.expires = Date.now() - 1; await oauth.put(key, JSON.stringify(saved));
  assert.equal((await f.browser.post({ handle, stage: 'consent', decision: 'approve', device_id: 'alice-pc' })).status, 403);
});

test('legacy website and account routes still work and device registration keeps or renews pairing identity', async () => {
  const f = await setup();
  assert.equal(await (await f.browser.fetch('/')).text(), 'synthetic website');
  assert.equal((await f.browser.fetch('/api/auth/me')).status, 200);
  const previous = f.alice.devices[0].oauth_pair_id;
  const register = async mcp_url => f.browser.fetch('/api/account/devices', { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: ORIGIN }, body: JSON.stringify({ device_id: 'alice-pc', hostname: 'Alice PC', mcp_url }) });
  assert.equal((await register(f.alice.devices[0].mcp_url.replace('alice-pc.', 'moved-pc.'))).status, 200);
  assert.equal((await users.get('user:' + f.alice.email, 'json')).devices[0].oauth_pair_id, previous);
  await f.browser.fetch('/api/account/devices', { method: 'DELETE', headers: { 'Content-Type': 'application/json', Origin: ORIGIN }, body: JSON.stringify({ device_id: 'alice-pc' }) });
  assert.equal((await register(f.alice.devices[0].mcp_url)).status, 200);
  assert.notEqual((await users.get('user:' + f.alice.email, 'json')).devices[0].oauth_pair_id, previous);
});

test('rotating an account key revokes derived web sessions and prevents recovery of the replacement key', async () => {
  const f = await setup(); const tokens = await grant(f); const stolen = new Browser();
  const login = await stolen.fetch('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: ORIGIN }, body: JSON.stringify({ api_key: f.alice.api_key }) });
  assert.equal(login.status, 200); const session = (await login.json()).token;
  const saved = await users.get('session:' + session, 'json');
  assert.ok(saved.expiresAt > Date.now());
  const key = (await users.list({ prefix: 'session:' + session })).keys[0];
  assert.ok(key.expiration >= Math.floor(Date.now() / 1000) + 43195);
  assert.equal((await stolen.fetch('/api/auth/me')).status, 200);
  const rotated = await f.browser.fetch('/api/account/rotate-key', { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: ORIGIN }, body: '{}' });
  assert.equal(rotated.status, 200);
  assert.equal((await stolen.fetch('/api/auth/me')).status, 401);
  assert.equal((await f.browser.fetch('/api/auth/me')).status, 401);
  assert.equal((await stolen.fetch('/api/account/devices')).status, 401);
  assert.equal((await mcp(tokens.access_token)).status, 401);
  assert.equal((await refresh(f, tokens.refresh_token)).status, 400);
  await users.put('apikey:' + f.alice.api_key, f.alice.email);
  assert.equal((await stolen.fetch('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: ORIGIN }, body: JSON.stringify({ api_key: f.alice.api_key }) })).status, 401);
  assert.equal((await mf.dispatchFetch(`${ORIGIN}/api/mcp/${f.alice.api_key}/alice-pc`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })).status, 401);
  assert.equal((await mf.dispatchFetch(ORIGIN + '/api/auth/me', { headers: { Authorization: 'Bearer ' + f.alice.api_key } })).status, 401);
});

test('expired and pre-migration web sessions fail closed on account and OAuth routes', async () => {
  for (const kind of ['expired', 'legacy', 'missing-version']) {
    const f = await setup(); const session = f.browser.cookies.get('sc_session');
    let saved = kind === 'legacy' ? f.alice.email : JSON.stringify({ email: f.alice.email, expiresAt: kind === 'expired' ? Date.now() - 1 : Date.now() + 43200000, ...(kind === 'expired' ? { accountKeyHash: createHash('sha256').update(f.alice.api_key).digest('hex') } : {}) });
    await users.put('session:' + session, saved);
    assert.equal((await f.browser.fetch('/api/auth/me')).status, 401);
    const { html } = await openConsent(f);
    assert.match(html, /Sign in to Stealth Commander/);
  }
});

async function pair(flow, details = {}) {
  const response = await flow.browser.fetch('/api/devices/pair', { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: ORIGIN }, body: JSON.stringify({ device_id: 'alice-pc', hostname: 'Alice PC', mcp_url: flow.alice.devices[0].mcp_url, ...details }) });
  assert.equal(response.status, 200);
  return response.json();
}
async function deviceCall(token, path = '/api/devices/me', body) {
  return mf.dispatchFetch(ORIGIN + path, { method: body ? 'POST' : 'GET', headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, ...(body ? { body: JSON.stringify(body) } : {}) });
}

test('pairing returns only a scoped device credential and stores its hash; heartbeat and me return only that PC', async () => {
  const f = await setup(); const paired = await pair(f);
  assert.match(paired.device_token, /^sc_dev_[a-f0-9]{48}$/);
  assert.equal(paired.device.device_id, 'alice-pc');
  assert.ok(!JSON.stringify(paired).includes(f.alice.api_key));
  assert.equal(paired.user, undefined); assert.equal(paired.token, undefined);
  const hash = createHash('sha256').update(paired.device_token).digest('hex');
  const credential = await users.get('deviceauth:' + hash, 'json');
  assert.equal(credential.email, f.alice.email);
  assert.equal(credential.deviceId, 'alice-pc');
  assert.ok(!JSON.stringify(credential).includes(paired.device_token));
  assert.ok(!JSON.stringify(await users.get('user:' + f.alice.email, 'json')).includes(paired.device_token));
  const response = await deviceCall(paired.device_token);
  assert.equal(response.status, 200);
  const me = await response.json();
  assert.equal(me.device.device_id, 'alice-pc');
  assert.equal(me.user, undefined); assert.equal(me.devices, undefined); assert.equal(me.token, undefined);
  assert.ok(!JSON.stringify(me).includes(f.alice.api_key));
  assert.equal(me.device.oauth_pair_id, undefined); assert.equal(me.device.device_credential_hash, undefined);
  const heartbeat = await deviceCall(paired.device_token, '/api/devices/heartbeat', { device_id: 'alice-pc', mcp_url: f.alice.devices[0].mcp_url.replace('alice-pc.', 'moved-pc.') });
  assert.equal(heartbeat.status, 200);
  assert.match((await heartbeat.json()).device.mcp_url, /^https:\/\/moved-pc\./);
});

test('device credentials cannot enter account management, legacy MCP, OAuth MCP or another device heartbeat', async () => {
  const f = await setup(); const paired = await pair(f); const token = paired.device_token;
  for (const [path, body] of [['/api/auth/me'], ['/api/account/devices'], ['/api/account/rotate-key', {}], ['/api/devices/pair', { device_id: 'bob-pc' }], ['/api/mcp', {}]]) assert.equal((await deviceCall(token, path, body)).status, 401);
  assert.equal((await mcp(token)).status, 401);
  assert.equal((await deviceCall(token, '/api/devices/heartbeat', { device_id: 'bob-pc' })).status, 403);
  assert.equal((await deviceCall(token, '/api/devices/heartbeat', { device_id: 'alice-pc', email: f.bob.email })).status, 403);
  assert.equal((await deviceCall(f.alice.api_key, '/api/devices/heartbeat', { device_id: 'alice-pc' })).status, 401);
  assert.equal((await deviceCall(token, '/api/devices/heartbeat', { device_id: 'alice-pc', mcp_url: 'http://127.0.0.1/mcp' })).status, 400);
});

test('owner re-pair, account rotation, own unpair and credential expiry revoke old device credentials and OAuth grants', async () => {
  for (const action of ['re-pair', 'rotate', 'unpair', 'expire']) {
    const f = await setup(); const paired = await pair(f); const tokens = await grant(f);
    if (action === 're-pair') await pair(f);
    if (action === 'rotate') assert.equal((await f.browser.fetch('/api/account/rotate-key', { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: ORIGIN }, body: '{}' })).status, 200);
    if (action === 'unpair') assert.equal((await deviceCall(paired.device_token, '/api/devices/unpair', {})).status, 200);
    if (action === 'expire') {
      const key = 'deviceauth:' + createHash('sha256').update(paired.device_token).digest('hex');
      const credential = await users.get(key, 'json'); credential.expiresAt = Date.now() - 1; await users.put(key, JSON.stringify(credential));
    }
    assert.equal((await deviceCall(paired.device_token)).status, 401);
    assert.equal((await mcp(tokens.access_token)).status, 401);
    assert.equal((await refresh(f, tokens.refresh_token)).status, 400);
  }
});

test('new registrations need 12 characters; rate limiting stops repeated logins, sign-ins and DCR', async () => {
  const browser = new Browser();
  const register = async password => browser.fetch('/api/auth/register', { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: ORIGIN }, body: JSON.stringify({ email: `registered${sequence}@example.test`, password }) });
  assert.equal((await register('123456')).status, 400);
  assert.equal((await register('synthetic-long-password')).status, 200);
  const f = await setup(); const attempts = [];
  for (let i = 0; i < 12; i++) attempts.push(await f.browser.fetch('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: ORIGIN }, body: JSON.stringify({ email: f.alice.email, password: 'incorrect' }) }));
  assert.equal(attempts[0].status, 401); assert.equal(attempts.at(-1).status, 429); assert.equal(attempts.at(-1).headers.get('Retry-After'), '60');
  const { handle } = await openConsent(f);
  assert.equal((await f.browser.post({ handle, stage: 'signin', decision: 'signin', email: f.alice.email, password: PASSWORD })).status, 429);
  const dcrBrowser = new Browser(); const statuses = [];
  for (let i = 0; i < 12; i++) statuses.push((await dcrBrowser.fetch('/oauth/register', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_name: 'Synthetic rate test', redirect_uris: [CALLBACK], token_endpoint_auth_method: 'none' }) })).status);
  assert.equal(statuses[0], 201); assert.equal(statuses.at(-1), 429);
});

test('rate limiting follows the account across source addresses and fails closed without its platform binding', async () => {
  const f = await setup();
  for (let i = 0; i < 10; i++) await f.browser.fetch('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: ORIGIN, 'CF-Connecting-IP': '198.51.100.' + i }, body: JSON.stringify({ email: f.alice.email, password: 'wrong' }) });
  assert.equal((await f.browser.fetch('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: ORIGIN, 'CF-Connecting-IP': '198.51.100.200' }, body: JSON.stringify({ email: f.alice.email, password: PASSWORD }) })).status, 429);
  const noLimit = new Miniflare({ modules: true, scriptPath: 'build/worker.js', compatibilityDate: '2026-07-30', compatibilityFlags: ['global_fetch_strictly_public'], bindings: { GATEWAY_ORIGIN: ORIGIN }, kvNamespaces: ['STEALTH_AUTH', 'OAUTH_KV'], serviceBindings: { AUTH_STORE: () => Response.json({ value: null, ok: true }), ASSETS: () => new Response('synthetic') }, outboundService: () => { throw new Error('External network is disabled.'); } });
  try {
    for (const path of ['/api/auth/login', '/api/auth/register', '/oauth/register']) assert.equal((await noLimit.dispatchFetch(ORIGIN + path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'CF-Connecting-IP': '192.0.2.1' }, body: '{}' })).status, 503);
    assert.equal((await noLimit.dispatchFetch(ORIGIN + '/authorize', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded', Origin: ORIGIN, 'CF-Connecting-IP': '192.0.2.1' }, body: 'stage=signin&decision=signin' })).status, 503);
  } finally { await noLimit.dispose(); }
});

test('bounded JSON deadline permits the PC maximum 300-second command plus overhead', async () => {
  assert.ok(UPSTREAM_TIMEOUT_MS >= 300000 + 30000 && UPSTREAM_TIMEOUT_MS <= 360000);
  const f = await setup(); const tokens = await grant(f);
  try { upstreamMode = 'delayed'; assert.equal((await mcp(tokens.access_token)).status, 200); }
  finally { upstreamMode = 'json'; }
});

test('non-string email cannot bypass account rate limiting through type coercion', async () => {
  const f = await setup();
  for (const email of [[f.alice.email], { value: f.alice.email }, null, 12]) {
    const response = await f.browser.fetch('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: ORIGIN, 'CF-Connecting-IP': '198.51.100.' + (++browserSequence) }, body: JSON.stringify({ email, password: PASSWORD }) });
    assert.equal(response.status, 400);
  }
});

test('a private Fetcher service can enforce rate limits and fails closed on invalid responses', async () => {
  let requests = 0; let malformed = false;
  const service = async request => {
    assert.equal(request.url, 'https://rate-limit.internal/check');
    const { key } = await request.json(); assert.match(key, /^signin:source:[a-f0-9]{64}$/);
    return Response.json(malformed ? { success: 'invalid' } : { success: ++requests < 2 });
  };
  const viaService = new Miniflare({ modules: true, scriptPath: 'build/worker.js', compatibilityDate: '2026-07-30', compatibilityFlags: ['global_fetch_strictly_public'], bindings: { GATEWAY_ORIGIN: ORIGIN }, kvNamespaces: ['STEALTH_AUTH', 'OAUTH_KV'], serviceBindings: { AUTH_STORE: () => Response.json({ value: null, ok: true }), ASSETS: () => new Response('synthetic'), AUTH_RATE_LIMITER: service, REGISTRATION_RATE_LIMITER: service }, outboundService: () => { throw new Error('External network is disabled.'); } });
  try {
    const call = () => viaService.dispatchFetch(ORIGIN + '/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json', 'CF-Connecting-IP': '192.0.2.1' }, body: '{}' });
    assert.equal((await call()).status, 400);
    assert.equal((await call()).status, 429);
    malformed = true; assert.equal((await call()).status, 503);
  } finally { await viaService.dispose(); }
});

test('public OAuth tools omit tunnel setup and cannot execute it directly or in a batch', async () => {
  const f = await setup(); const tokens = await grant(f);
  try {
    upstreamMode = 'public-metadata';
    const listed = await (await mcp(tokens.access_token)).json();
    assert.deepEqual(listed.result.tools.map(t => t.name), ['dashboard_status', 'run_command']);
    const count = upstreamCalls.length;
    const direct = await mcp(tokens.access_token, { body: JSON.stringify({ jsonrpc: '2.0', id: 5, method: 'tools/call', params: { name: 'start_tunnel', arguments: {} } }) });
    assert.equal(direct.status, 200); assert.equal((await direct.json()).error.code, -32601);
    assert.equal((await mcp(tokens.access_token, { body: JSON.stringify([{ jsonrpc: '2.0', id: 6, method: 'tools/call', params: { name: 'start_tunnel' } }]) })).status, 400);
    assert.equal(upstreamCalls.length, count);
    assert.equal((await mf.dispatchFetch(ORIGIN + '/mcp', { headers: { Authorization: 'Bearer ' + tokens.access_token } })).status, 405);
  } finally { upstreamMode = 'json'; }
});

test('OAuth removes server credentials and capability URLs from structured and JSON-text metadata; errors survive', async () => {
  const f = await setup(); const tokens = await grant(f);
  try {
    for (const mode of ['public-metadata', 'public-error']) {
      upstreamMode = mode;
      const response = await mcp(tokens.access_token, { body: JSON.stringify({ jsonrpc: '2.0', id: 7, method: 'tools/call', params: { name: 'dashboard_status', arguments: {} } }) });
      assert.equal(response.status, 200); const result = (await response.json()).result;
      const text = JSON.stringify(result);
      for (const secret of ['synthetic-server-token', 'a'.repeat(48), 's'.repeat(32), 'trycloudflare.com', '127.0.0.1:61076']) assert.ok(!text.includes(secret));
      assert.equal(result.structuredContent.result.desktops[0].name, 'Desktop A');
      if (mode === 'public-error') { assert.equal(result.isError, true); assert.equal(JSON.parse(result.content[0].text).error, 'Synthetic tool error survives.'); }
    }
  } finally { upstreamMode = 'json'; }
});

test('ordinary run_command stdout, stderr, exit status and user-provided content remain intact', async () => {
  const f = await setup(); const tokens = await grant(f);
  try {
    upstreamMode = 'command-output';
    const response = await mcp(tokens.access_token, { body: JSON.stringify({ jsonrpc: '2.0', id: 8, method: 'tools/call', params: { name: 'run_command', arguments: { argv: ['synthetic'] } } }) });
    const result = (await response.json()).result; const payload = JSON.parse(result.content[0].text);
    assert.equal(payload.stdout, `ordinary stdout https://alice-pc.trycloudflare.com/${'a'.repeat(48)}/mcp`);
    assert.equal(payload.stderr, 'ordinary error'); assert.equal(payload.returncode, 2); assert.equal(result.isError, true);
  } finally { upstreamMode = 'json'; }
});

test('scoped device metadata cannot redirect the owner website to an arbitrary server', async () => {
  const f = await setup(); const paired = await pair(f);
  for (const field of ['tunnel_url', 'local_portal_url', 'dashboard_url']) {
    assert.equal((await deviceCall(paired.device_token, '/api/devices/heartbeat', { device_id: 'alice-pc', mcp_url: f.alice.devices[0].mcp_url, [field]: 'https://attacker.example/collect' })).status, 400);
  }
  assert.equal((await deviceCall(paired.device_token, '/api/devices/heartbeat', { device_id: 'alice-pc', mcp_url: f.alice.devices[0].mcp_url, tunnel_url: f.alice.devices[0].mcp_url.slice(0, -4), local_portal_url: 'http://127.0.0.1:61077/' + 'p'.repeat(32), dashboard_url: 'http://127.0.0.1:61076/' + 'd'.repeat(32) })).status, 200);
});


test('concurrent consent and authorization-code redemption succeed at most once (SQLite)', async () => {
  const f = await setup(); const { handle } = await openConsent(f);
  const decisions = await Promise.all([1, 2].map(() => f.browser.post({ handle, stage: 'consent', decision: 'approve', device_id: 'alice-pc' })));
  assert.equal(decisions.filter(r => r.status === 302).length, 1);
  assert.ok(decisions.every(r => [302, 400, 403, 409].includes(r.status)));
  const code = new URL(decisions.find(r => r.status === 302).headers.get('Location')).searchParams.get('code');
  const tokens = await Promise.all([exchange(f, code), exchange(f, code)]);
  assert.equal(tokens.filter(r => r.status === 200).length, 1);
  assert.ok(tokens.every(r => [200, 400, 409].includes(r.status)));
});

async function redeemPcTicket(paired, record, changes = {}) {
  return mf.dispatchFetch(ORIGIN + '/api/devices/authorize', { method: 'POST', headers: { Authorization: 'Bearer ' + paired.device_token, 'Content-Type': 'application/json' }, body: JSON.stringify({ ticket: record.headers['x-stealth-pc-authorization'], method: record.method, path: new URL(record.url).pathname.replace(/^\/[A-Za-z0-9_-]{32,128}/, ''), body_sha256: createHash('sha256').update(record.body).digest('hex'), ...changes }) });
}

test('PC tickets bind exact pairing, method, path and bytes and consume once (SQLite)', async () => {
  const f = await setup(); const paired = await pair(f); const tokens = await grant(f);
  assert.equal((await mcp(tokens.access_token)).status, 200); const record = upstreamCalls.at(-1);
  assert.match(record.headers['x-stealth-pc-authorization'], /^sc_pct_[a-f0-9]{48}$/);
  for (const changes of [{ method: 'GET' }, { path: '/action' }, { body_sha256: '0'.repeat(64) }, { email: f.bob.email }, { ticket: undefined }]) assert.equal((await redeemPcTicket(paired, record, changes)).status, changes.email ? 403 : 401);
  const contenders = await Promise.all([redeemPcTicket(paired, record), redeemPcTicket(paired, record)]);
  assert.deepEqual(contenders.map(r => r.status).sort(), [200, 401]);
  assert.equal((await contenders.find(r => r.status === 200).json()).device_id, 'alice-pc');
  assert.equal((await redeemPcTicket(paired, record)).status, 401);
});

test('pending OAuth PC tickets are denied immediately after token revocation (SQLite)', async () => {
  const f = await setup(); const paired = await pair(f); const tokens = await grant(f);
  await mcp(tokens.access_token); const record = upstreamCalls.at(-1);
  const metadata = await (await mf.dispatchFetch(ORIGIN + '/.well-known/oauth-authorization-server')).json();
  const revoked = await f.browser.fetch(metadata.revocation_endpoint, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ client_id: f.client.client_id, token: tokens.access_token }).toString() });
  assert.equal(revoked.status, 200);
  assert.equal((await redeemPcTicket(paired, record)).status, 401);
});

test('dashboard proxy requires current session, owned PC, CSRF and allowed paths; logout ends pending tickets', async () => {
  const f = await setup(); const paired = await pair(f);
  const nonce = await (await f.browser.fetch('/api/account/session')).json();
  const control = (body, csrf = nonce.csrfToken) => f.browser.fetch('/api/device/control', { method: 'POST', headers: { Origin: ORIGIN, 'Content-Type': 'application/json', 'X-Stealth-CSRF': csrf }, body: JSON.stringify(body) });
  const basic = { device_id: 'alice-pc', path: '/meta', method: 'GET' };
  assert.equal((await control(basic, 'wrong')).status, 403);
  assert.equal((await control({ ...basic, device_id: 'bob-pc' })).status, 401);
  for (const path of ['https://evil.example/', '//evil.example/', '/settings', '/stream/../../x.mjpg']) assert.equal((await control({ ...basic, path })).status, 400);
  for (const payload of [{ kind: 'publish_pages' }, { kind: 'save_settings', settings: { auto_publish_pages: true } }]) assert.equal((await control({ ...basic, path: '/action', method: 'POST', payload })).status, 403);
  assert.equal((await control(basic)).status, 200); const record = upstreamCalls.at(-1);
  assert.equal((await redeemPcTicket(paired, record)).status, 200);
  await control(basic); const pending = upstreamCalls.at(-1);
  assert.equal((await f.browser.fetch('/api/auth/logout', { method: 'POST', headers: { Origin: ORIGIN, 'Content-Type': 'application/json' }, body: '{}' })).status, 200);
  assert.equal((await redeemPcTicket(paired, pending)).status, 401);
  assert.equal((await control(basic)).status, 401);
});


test('expired and re-paired PC tickets cannot authorize a retained connection', async () => {
  for (const change of ['expiry', 're-pair']) {
    const f = await setup(); const paired = await pair(f); const tokens = await grant(f);
    await mcp(tokens.access_token); const record = upstreamCalls.at(-1);
    if (change === 'expiry') {
      const key = 'pcticket:' + createHash('sha256').update(record.headers['x-stealth-pc-authorization']).digest('hex');
      const ticket = await users.get(key, 'json'); ticket.expiresAt = Date.now() - 1; await users.put(key, JSON.stringify(ticket));
    } else await pair(f);
    assert.equal((await redeemPcTicket(paired, record)).status, 401);
  }
});
