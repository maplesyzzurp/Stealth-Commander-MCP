import assert from 'node:assert/strict';
import { createHash, createHmac } from 'node:crypto';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { handleBilling } from '../src/billing.js';

const ORIGIN = 'https://stealthcommander.example.invalid';
const ALICE = { user: { email: 'alice@example.invalid', created_at: '2026-01-02T03:04:05.000Z' }, session: 'sc_sess_' + 'a'.repeat(48) };
const BOB = { user: { email: 'bob@example.invalid', created_at: '2026-01-03T03:04:05.000Z' }, session: 'sc_sess_' + 'b'.repeat(48) };
const account = context => 'scb_' + createHash('sha256').update(JSON.stringify(['stealth-billing-v1', 'profile', context.user.email, context.user.created_at])).digest('hex');
const emptyStatus = { mode: 'test', billingStatus: 'none', testSubscriptionActive: false, accessAllowed: false };
const require = createRequire(import.meta.url);

function fixture(overrides = {}) {
  const calls = [], limits = [], customers = new Set();
  const state = { response: null };
  const env = {
    GATEWAY_ORIGIN: ORIGIN, BILLING_TEST_ENABLED: 'true', BILLING_MODE: 'test', BILLING_CSRF_SECRET: 'local_synthetic_csrf_secret_at_least_32_characters',
    AUTH_RATE_LIMITER: { async limit({ key }) { limits.push(key); return { success: true }; } },
    BILLING_SERVICE: { async fetch(input, init) {
      const request = input instanceof Request ? input : new Request(input, init);
      const body = await request.text(), accountId = request.headers.get('X-Stealth-Account-Id');
      const path = new URL(request.url).pathname;
      calls.push({ url: request.url, path, accountId, headers: Object.fromEntries(request.headers), body });
      if (state.response) return state.response();
      if (path === '/webhook') return Response.json({ received: true });
      assert.equal(body, '{}');
      if (path === '/status') return Response.json(emptyStatus);
      if (path === '/createcheckout') { customers.add(accountId); return Response.json({ mode: 'test', url: 'https://checkout.stripe.com/c/pay/cs_test_fixture' }); }
      if (path === '/createportal') return customers.has(accountId) ? Response.json({ mode: 'test', url: 'https://billing.stripe.com/p/session/fixture' }) : Response.json({ error: 'No billing customer exists.' }, { status: 404 });
      return Response.json({ error: 'Unknown service route.' }, { status: 404 });
    } },
    ...overrides,
  };
  async function call(path = 'status', { context = ALICE, method = path === 'status' ? 'GET' : 'POST', body = method === 'POST' ? '{}' : undefined, headers = {}, csrfToken, origin = ORIGIN, url = ORIGIN + '/api/billing/' + path } = {}) {
    const base = { 'Sec-Fetch-Site': 'same-origin', 'CF-Connecting-IP': '192.0.2.3', ...(context ? { Cookie: 'sc_session=' + context.session } : {}), ...(method === 'POST' ? { Origin: origin, 'Content-Type': 'application/json' } : {}), ...(csrfToken ? { 'X-CSRF-Token': csrfToken } : {}) };
    for (const [key, value] of Object.entries(headers)) { if (value === null) delete base[key]; else base[key] = value; }
    return handleBilling(new Request(url, { method, headers: base, ...(body !== undefined ? { body } : {}) }), env, context);
  }
  async function token(context = ALICE) { const response = await call('status', { context }); assert.equal(response.status, 200); return (await response.json()).csrfToken; }
  return { env, calls, limits, customers, state, call, token };
}

test('status creates a session-bound CSRF token and returns only the test status', async () => {
  const f = fixture(), response = await f.call(), body = await response.json();
  assert.equal(response.status, 200);
  assert.deepEqual({ ...body, csrfToken: '<token>' }, { ...emptyStatus, csrfToken: '<token>' });
  assert.match(body.csrfToken, /^v1\.[a-f0-9]{64}$/);
  assert.equal(response.headers.get('Cache-Control'), 'no-store');
  assert.equal(f.calls[0].accountId, account(ALICE));
  assert.equal(f.limits.length, 2);
  assert.match(f.limits[0], /^billing:status:source:[a-f0-9]{64}$/);
  assert.match(f.limits[1], /^billing:status:account:[a-f0-9]{64}$/);
});

test('checkout and portal accept only the verified session identity', async () => {
  const f = fixture(), csrfToken = await f.token();
  const checkout = await f.call('checkout', { csrfToken, headers: { 'X-Stealth-Account-Id': account(BOB), Authorization: 'Bearer attacker', 'X-Forwarded-For': '198.51.100.8' } });
  assert.deepEqual(await checkout.json(), { mode: 'test', url: 'https://checkout.stripe.com/c/pay/cs_test_fixture', accessAllowed: false });
  const forwarded = f.calls.at(-1);
  assert.deepEqual(forwarded.headers, { 'content-type': 'application/json', 'x-stealth-account-id': account(ALICE) });
  assert.equal(forwarded.body, '{}');
  const portal = await f.call('portal', { csrfToken });
  assert.deepEqual(await portal.json(), { mode: 'test', url: 'https://billing.stripe.com/p/session/fixture', accessAllowed: false });
  const otherPortal = await f.call('portal', { context: BOB, csrfToken: await f.token(BOB), headers: { 'X-Stealth-Account-Id': account(ALICE) } });
  assert.equal(otherPortal.status, 404);
  assert.deepEqual(await otherPortal.json(), { error: 'No billing customer exists.' });
});

test('missing cookie, bearer-only, mismatched cookie, duplicate cookie and null context are denied', async () => {
  const f = fixture();
  for (const options of [
    { context: null, headers: { Authorization: 'Bearer sc_live_' + 'c'.repeat(40) } },
    { headers: { Cookie: null, Authorization: 'Bearer token' } },
    { headers: { Cookie: 'sc_session=' + BOB.session } },
    { headers: { Cookie: 'sc_session=' + ALICE.session + '; sc_session=' + BOB.session } },
    { context: { ...ALICE, session: 'unverified' } },
  ]) {
    const response = await f.call('status', options);
    assert.equal(response.status, 401);
    assert.deepEqual(await response.json(), { error: 'Sign in to use billing.' });
  }
  assert.equal(f.calls.length, 0);
});

test('CSRF token cannot be omitted, changed, or reused by another session or account', async () => {
  const f = fixture(), csrfToken = await f.token(), rotated = { ...ALICE, session: 'sc_sess_' + 'c'.repeat(48) };
  for (const options of [{}, { csrfToken: 'v1.' + '0'.repeat(64) }, { csrfToken, context: BOB }, { csrfToken, context: rotated }]) {
    const response = await f.call('checkout', options);
    assert.equal(response.status, 403);
    assert.deepEqual(await response.json(), { error: 'Billing request expired. Refresh and try again.' });
  }
  assert.equal(f.calls.filter(call => call.path === '/createcheckout').length, 0);
});

test('browser billing routes reject wrong origins, fetch sites, destinations and queries', async () => {
  const f = fixture(), csrfToken = await f.token();
  for (const options of [
    { origin: 'https://attacker.invalid' }, { headers: { Origin: null } },
    { headers: { 'Sec-Fetch-Site': 'same-site' } }, { headers: { 'Sec-Fetch-Site': 'cross-site' } },
    { headers: { 'Sec-Fetch-Site': null } }, { headers: { 'Sec-Fetch-Mode': 'navigate' } },
  ]) assert.equal((await f.call('checkout', { csrfToken, ...options })).status, 403);
  assert.equal((await f.call('status', { headers: { Origin: 'https://attacker.invalid' } })).status, 403);
  assert.equal((await f.call('status', { url: 'https://attacker.invalid/api/billing/status' })).status, 421);
  assert.equal((await f.call('status', { url: ORIGIN + '/api/billing/status?accountId=bob' })).status, 400);
});

test('JSON actions reject every caller-selected field, invalid JSON and oversized streamed input', async () => {
  const f = fixture(), csrfToken = await f.token();
  for (const body of ['{"accountId":"bob"}', '{"customer":"cus_bob"}', '{"price":"price_bob"}', '{"return_url":"https://attacker.invalid"}', 'null', '[]', '{']) {
    assert.equal((await f.call('checkout', { csrfToken, body })).status, 400);
  }
  assert.equal((await f.call('checkout', { csrfToken, headers: { 'Content-Type': 'text/plain' } })).status, 415);
  assert.equal((await f.call('checkout', { csrfToken, body: ' '.repeat(2049) })).status, 413);
  assert.equal(f.calls.filter(call => call.path === '/createcheckout').length, 0);
});

test('mode, service, CSRF and abuse-protection configuration fails closed', async () => {
  for (const overrides of [{ BILLING_TEST_ENABLED: undefined }, { BILLING_TEST_ENABLED: 'false' }, { BILLING_MODE: 'live' }, { BILLING_MODE: undefined }, { BILLING_SERVICE: undefined }, { BILLING_CSRF_SECRET: 'short' }, { AUTH_RATE_LIMITER: undefined }, { AUTH_RATE_LIMITER: { async limit() { throw new Error('offline'); } } }, { AUTH_RATE_LIMITER: { async limit() { return {}; } } }]) {
    const f = fixture(overrides), response = await f.call();
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), { error: 'Test billing is unavailable.' });
    assert.equal(f.calls.length, 0);
  }
  const f = fixture();
  assert.equal((await f.call('status', { headers: { 'CF-Connecting-IP': null } })).status, 503);
});

test('denied source or account limits prevent checkout and return a retry delay', async () => {
  for (const denied of ['source', 'account']) {
    const f = fixture(), csrfToken = await f.token();
    f.env.AUTH_RATE_LIMITER = { async limit({ key }) { return { success: !key.includes(':' + denied + ':') }; } };
    const response = await f.call('checkout', { csrfToken });
    assert.equal(response.status, 429);
    assert.equal(response.headers.get('Retry-After'), '60');
    assert.deepEqual(await response.json(), { error: 'Too many billing requests. Try again later.' });
    assert.equal(f.calls.filter(call => call.path === '/createcheckout').length, 0);
  }
});

test('service-binding limiter receives opaque source and account keys and fails closed on errors', async () => {
  const keys = [], f = fixture({ AUTH_RATE_LIMITER: { async fetch(input, init) { const request = new Request(input, init); keys.push((await request.json()).key); assert.equal(request.url, 'https://rate-limit.internal/check'); return Response.json({ success: true }); } } });
  assert.equal((await f.call()).status, 200);
  assert.equal(keys.length, 2);
  assert.equal(keys.some(key => key.includes('alice') || key.includes('192.0.2.3')), false);
  f.env.AUTH_RATE_LIMITER = { async fetch() { return Response.json({ success: true }, { status: 500 }); } };
  assert.equal((await f.call()).status, 503);
  f.env.AUTH_RATE_LIMITER = { async fetch() { return new Response('{broken', { headers: { 'Content-Type': 'application/json' } }); } };
  assert.equal((await f.call()).status, 503);
});

test('profile identity is stable across key rotation and changes when an email is recreated', async () => {
  const f = fixture();
  await f.call('status');
  await f.call('status', { context: { ...ALICE, user: { ...ALICE.user, api_key: 'rotated' } } });
  assert.equal(f.calls[0].accountId, f.calls[1].accountId);
  await f.call('status', { context: { ...ALICE, user: { ...ALICE.user, created_at: '2026-02-03T03:04:05.000Z' } } });
  assert.notEqual(f.calls[0].accountId, f.calls[2].accountId);
  for (const user of [{ email: ALICE.user.email }, { email: ALICE.user.email, created_at: 'invalid' }, { email: ' ALICE@example.invalid ', created_at: ALICE.user.created_at }]) assert.equal((await f.call('status', { context: { ...ALICE, user } })).status, 503);
});

test('only valid test-only service replies become website responses', async () => {
  const f = fixture(), csrfToken = await f.token();
  for (const value of [{ ...emptyStatus, mode: 'live' }, { ...emptyStatus, accessAllowed: true }, { ...emptyStatus, billingStatus: 'unknown' }, { ...emptyStatus, testSubscriptionActive: true }]) {
    f.state.response = () => Response.json(value);
    assert.equal((await f.call()).status, 503);
  }
  for (const value of [{ mode: 'live', url: 'https://checkout.stripe.com/c/pay/test' }, { mode: 'test', url: 'https://attacker.invalid' }, { mode: 'test', url: 'https://checkout.stripe.com@attacker.invalid' }, { mode: 'test', url: 'https://checkout.stripe.com/c/pay/test', accessAllowed: true }]) {
    f.state.response = () => Response.json(value);
    assert.equal((await f.call('checkout', { csrfToken })).status, 503);
  }
  for (const response of [() => new Response('{broken', { headers: { 'Content-Type': 'application/json' } }), () => Response.json(emptyStatus, { status: 201 }), () => new Response(' '.repeat(4097), { headers: { 'Content-Type': 'application/json' } })]) {
    f.state.response = response;
    assert.equal((await f.call()).status, 503);
  }
});

test('webhook forwards original raw bytes and signature only to its fixed private route', async () => {
  const f = fixture(), raw = new TextEncoder().encode('{\n "object": "event", "note": "é"\n}\n'), signature = 't=1790000000,v1=' + 'f'.repeat(64);
  const response = await f.call('webhook', { context: null, body: raw, headers: { 'Stripe-Signature': signature, 'X-Stealth-Account-Id': account(ALICE) } });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { received: true });
  assert.deepEqual(f.calls[0], { url: 'https://billing.internal/webhook', path: '/webhook', accountId: null, headers: { 'content-type': 'application/json', 'stripe-signature': signature }, body: new TextDecoder().decode(raw) });
  assert.equal(f.limits.length, 0);
});

test('webhook rejects missing signatures, oversized input, wrong methods and unexpected route parameters', async () => {
  const f = fixture(), signature = 't=1790000000,v1=' + 'a'.repeat(64);
  assert.equal((await f.call('webhook', { context: null })).status, 400);
  assert.equal((await f.call('webhook', { context: null, headers: { 'Stripe-Signature': signature }, body: 'a'.repeat(256 * 1024 + 1) })).status, 413);
  assert.equal((await f.call('webhook', { context: null, method: 'GET', body: undefined })).status, 405);
  assert.equal((await f.call('webhook', { context: null, headers: { 'Stripe-Signature': signature }, url: ORIGIN + '/api/billing/webhook?path=/createcheckout' })).status, 400);
  assert.equal(f.calls.length, 0);
});

test('workerd gateway and private ledger complete checkout, separate accounts, and verify the forwarded signature', async () => {
  const { Miniflare } = require('miniflare'), { build } = require('esbuild');
  const entry = `import {handleBilling} from './src/billing.js';
    const users = ${JSON.stringify([ALICE, BOB])};
    export default { fetch(request, env) {
      const cookie = request.headers.get('Cookie');
      const context = users.find(user => cookie === 'sc_session=' + user.session) || null;
      return handleBilling(request, env, context);
    } };`;
  const bundled = await build({ stdin: { contents: entry, resolveDir: fileURLToPath(new URL('../', import.meta.url)), sourcefile: 'billing-fixture.js' }, bundle: true, format: 'esm', platform: 'browser', write: false });
  const customers = new Map(), stripeCalls = [];
  const webhookSecret = 'whsec_synthetic_adapter_fixture';
  const mf = new Miniflare({ workers: [
    { name: 'billing-gateway-fixture', modules: true, script: bundled.outputFiles[0].text, compatibilityDate: '2026-07-30',
      bindings: { GATEWAY_ORIGIN: ORIGIN, BILLING_TEST_ENABLED: 'true', BILLING_MODE: 'test', BILLING_CSRF_SECRET: 'synthetic_workerd_secret_at_least_32_characters' },
      serviceBindings: { BILLING_SERVICE: 'billing-private-fixture', AUTH_RATE_LIMITER: async () => Response.json({ success: true }) } },
    { name: 'billing-private-fixture', modules: true, script: require('node:fs').readFileSync(new URL('../../billing-service/worker.js', import.meta.url), 'utf8'), compatibilityDate: '2026-07-30',
      bindings: { BILLING_TEST_ENABLED: 'true', BILLING_MODE: 'test', STRIPE_SECRET_KEY: 'sk_test_synthetic_adapter_fixture', STRIPE_WEBHOOK_SECRET: webhookSecret, STRIPE_ACCOUNT_ID: 'acct_fixture', STRIPE_PRICE_ID: 'price_fixture', WEB_ORIGIN: ORIGIN },
      durableObjects: { BILLING: { className: 'BillingLedger', useSQLite: true } },
      outboundService: async request => {
        const url = new URL(request.url), form = Object.fromEntries(new URLSearchParams(request.method === 'POST' ? await request.text() : ''));
        assert.equal(url.origin, 'https://api.stripe.com');
        stripeCalls.push({ path: url.pathname, form });
        if (url.pathname === '/v1/account') return Response.json({ id: 'acct_fixture', object: 'account' });
        if (url.pathname === '/v1/prices/price_fixture') return Response.json({ id: 'price_fixture', object: 'price', livemode: false, active: true, type: 'recurring', recurring: { usage_type: 'licensed' } });
        if (url.pathname === '/v1/customers') {
          const customer = { object: 'customer', id: 'cus_adapter' + (customers.size + 1), livemode: false, metadata: { billing_binding_id: form['metadata[billing_binding_id]'] } };
          customers.set(customer.id, customer); return Response.json(customer);
        }
        if (url.pathname.startsWith('/v1/customers/')) return Response.json(customers.get(url.pathname.split('/').at(-1)));
        if (url.pathname === '/v1/subscriptions') return Response.json({ object: 'list', data: [], has_more: false });
        if (url.pathname === '/v1/checkout/sessions') return Response.json({ object: 'checkout.session', id: 'cs_test_adapter', livemode: false, customer: form.customer, mode: 'subscription', status: 'open', expires_at: Number(form.expires_at), url: 'https://checkout.stripe.com/c/pay/cs_test_adapter' });
        if (url.pathname === '/v1/billing_portal/sessions') return Response.json({ object: 'billing_portal.session', customer: form.customer, livemode: false, return_url: form.return_url, url: 'https://billing.stripe.com/p/session/adapter' });
        return Response.json({ error: 'Unknown Stripe fixture path.' }, { status: 500 });
      } },
  ] });
  const call = (path, context, csrfToken) => mf.dispatchFetch(ORIGIN + '/api/billing/' + path, { method: path === 'status' ? 'GET' : 'POST', headers: { Cookie: 'sc_session=' + context.session, 'CF-Connecting-IP': '192.0.2.3', 'Sec-Fetch-Site': 'same-origin', ...(path !== 'status' ? { Origin: ORIGIN, 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken } : {}) }, ...(path !== 'status' ? { body: '{}' } : {}) });
  try {
    const status = await call('status', ALICE);
    assert.equal(status.status, 200);
    const aliceStatus = await status.json();
    assert.deepEqual({ ...aliceStatus, csrfToken: '<token>' }, { ...emptyStatus, csrfToken: '<token>' });
    const checkout = await call('checkout', ALICE, aliceStatus.csrfToken);
    assert.equal(checkout.status, 200);
    assert.deepEqual(await checkout.json(), { mode: 'test', url: 'https://checkout.stripe.com/c/pay/cs_test_adapter', accessAllowed: false });
    assert.equal(customers.size, 1);
    const bobStatus = await (await call('status', BOB)).json();
    assert.equal((await call('portal', BOB, bobStatus.csrfToken)).status, 404);
    assert.equal((await call('portal', ALICE, aliceStatus.csrfToken)).status, 200);
    assert.equal(stripeCalls.find(call => call.path === '/v1/billing_portal/sessions').form.customer, 'cus_adapter1');
    const payload = '{\n"object":"event","id":"evt_adapter","api_version":"2026-09-30.endive","livemode":false,"type":"invoice.paid","data":{"object":{"customer":"cus_unknown"}}}\n';
    const timestamp = Math.floor(Date.now() / 1000), signature = `t=${timestamp},v1=${createHmac('sha256', webhookSecret).update(timestamp + '.' + payload).digest('hex')}`;
    const webhook = await mf.dispatchFetch(ORIGIN + '/api/billing/webhook', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Stripe-Signature': signature }, body: payload });
    assert.equal(webhook.status, 200);
    assert.deepEqual(await webhook.json(), { received: true, ignored: true });
    const tampered = await mf.dispatchFetch(ORIGIN + '/api/billing/webhook', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Stripe-Signature': signature }, body: payload + ' ' });
    assert.equal(tampered.status, 400);
    assert.deepEqual(await tampered.json(), { error: 'Invalid webhook signature.' });
  } finally { await mf.dispose(); }
});
