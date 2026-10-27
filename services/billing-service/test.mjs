import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { createHmac } from 'node:crypto';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const require = createRequire(new URL('../oauth-gateway/package.json', import.meta.url));
const { Miniflare } = require('miniflare');
const WEB = 'https://stealthcommander.example.invalid';
const SECRET = 'whsec_synthetic_local_fixture_only';

async function fixture(overrides = {}) {
  const customers = new Map(), sessions = new Map(), subscriptions = new Map(), idempotent = new Map(), calls = [];
  const state = { failure: null, accountId: 'acct_fixture', priceLive: false, responseCustomer: null, dropCustomerOnce: false };
  const server = createServer(async (request, response) => {
    let body = ''; for await (const chunk of request) body += chunk;
    const url = new URL(request.url, 'http://127.0.0.1');
    const form = Object.fromEntries(new URLSearchParams(body));
    calls.push({ method: request.method, path: url.pathname, query: Object.fromEntries(url.searchParams), form, idempotencyKey: request.headers['idempotency-key'], version: request.headers['stripe-version'] });
    if (state.failure?.path === url.pathname) {
      if (state.failure.kind === 'hang') return;
      response.writeHead(state.failure.status || 500, { 'Content-Type': state.failure.kind === 'html' ? 'text/html' : 'application/json' });
      response.end(state.failure.kind === 'html' ? '<html>upstream failure</html>' : JSON.stringify({ error: { message: 'synthetic Stripe error' } })); return;
    }
    const send = (value, status = 200) => { response.writeHead(status, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(value)); };
    if (url.pathname === '/v1/account') return send({ id: state.accountId, object: 'account' });
    if (url.pathname === '/v1/prices/price_fixture') return send({ id: 'price_fixture', object: 'price', livemode: state.priceLive, active: true, type: 'recurring', recurring: { interval: 'month', usage_type: 'licensed' } });
    if (url.pathname === '/v1/customers' && request.method === 'POST') {
      const key = request.headers['idempotency-key'];
      let customer = idempotent.get(key);
      if (!customer) {
        customer = { id: 'cus_fixture' + (customers.size + 1), object: 'customer', livemode: false, metadata: { billing_binding_id: form['metadata[billing_binding_id]'] } };
        customers.set(customer.id, customer); idempotent.set(key, customer);
      }
      if (state.dropCustomerOnce) { state.dropCustomerOnce = false; response.destroy(); return; }
      return send(customer);
    }
    if (url.pathname.startsWith('/v1/customers/')) return send(customers.get(url.pathname.split('/').at(-1)) || { error: 'unknown customer' }, customers.has(url.pathname.split('/').at(-1)) ? 200 : 404);
    if (url.pathname === '/v1/subscriptions') return send({ object: 'list', data: subscriptions.get(url.searchParams.get('customer')) || [], has_more: false });
    if (url.pathname === '/v1/checkout/sessions' && request.method === 'POST') {
      const key = request.headers['idempotency-key']; let session = idempotent.get(key);
      if (!session) {
        session = { id: 'cs_test_fixture' + (sessions.size + 1), object: 'checkout.session', livemode: false, customer: state.responseCustomer || form.customer, mode: 'subscription', status: 'open', expires_at: Number(form.expires_at), url: 'https://checkout.stripe.com/c/pay/cs_test_fixture' + (sessions.size + 1) };
        sessions.set(session.id, session); idempotent.set(key, session);
      }
      return send(session);
    }
    if (url.pathname.startsWith('/v1/checkout/sessions/')) return send(sessions.get(url.pathname.split('/').at(-1)));
    if (url.pathname === '/v1/billing_portal/sessions') return send({ id: 'bps_fixture', object: 'billing_portal.session', livemode: false, customer: state.responseCustomer || form.customer, configuration: form.configuration || 'bpc_default', return_url: form.return_url, url: 'https://billing.stripe.com/p/session/synthetic' });
    send({ error: 'Endpoint not defined by this simulation.' }, 501);
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const mockOrigin = 'http://127.0.0.1:' + server.address().port;
  const mf = new Miniflare({
    name: 'billing-candidate', modules: true, scriptPath: fileURLToPath(new URL('./worker.js', import.meta.url)), compatibilityDate: '2026-07-30',
    bindings: { BILLING_TEST_ENABLED: 'true', BILLING_MODE: 'test', STRIPE_SECRET_KEY: 'sk_test_synthetic_local_fixture_only', STRIPE_WEBHOOK_SECRET: SECRET, STRIPE_ACCOUNT_ID: 'acct_fixture', STRIPE_PRICE_ID: 'price_fixture', STRIPE_PORTAL_CONFIGURATION_ID: 'bpc_fixture', WEB_ORIGIN: WEB, ...overrides },
    durableObjects: { BILLING: { className: 'BillingLedger', useSQLite: true } },
    outboundService: async request => {
      const url = new URL(request.url); assert.equal(url.origin, 'https://api.stripe.com');
      const remote = await fetch(mockOrigin + url.pathname + url.search, { method: request.method, redirect: 'manual', headers: request.headers, ...(request.method === 'POST' ? { body: await request.text() } : {}), signal: request.signal });
      return new Response(remote.body, { status: remote.status, headers: remote.headers });
    },
  });
  const call = (path, accountId, extra = {}) => mf.dispatchFetch('https://billing.internal/' + path, { method: 'POST', headers: { 'Content-Type': 'application/json', ...(accountId ? { 'X-Stealth-Account-Id': accountId } : {}) }, body: JSON.stringify(extra) });
  const webhook = (event, { timestamp = Math.floor(Date.now() / 1000), signature, body = JSON.stringify(event) } = {}) => {
    const computed = createHmac('sha256', SECRET).update(timestamp + '.' + body).digest('hex');
    return mf.dispatchFetch('https://billing.internal/webhook', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Stripe-Signature': signature ?? `t=${timestamp},v1=${computed}` }, body });
  };
  return { mf, customers, sessions, subscriptions, calls, state, call, webhook, async close() { await mf.dispose(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); } };
}

test('concurrent checkout creates one bound customer and one hosted session per account', async () => {
  const f = await fixture();
  try {
    const responses = await Promise.all(Array.from({ length: 4 }, () => f.call('createcheckout', 'account-alice')));
    const results = await Promise.all(responses.map(response => response.json()));
    assert.deepEqual(responses.map(response => response.status), [200, 200, 200, 200], JSON.stringify({ results, calls: f.calls }));
    assert.equal(new Set(results.map(result => result.url)).size, 1);
    assert.equal(f.customers.size, 1); assert.equal(f.sessions.size, 1);
    assert.equal((await f.call('createcheckout', 'account-bob')).status, 200);
    assert.equal(f.customers.size, 2);
    assert.notEqual([...f.sessions.values()][0].customer, [...f.sessions.values()][1].customer);
    const created = f.calls.find(call => call.path === '/v1/checkout/sessions' && call.method === 'POST');
    assert.equal(created.form['line_items[0][price]'], 'price_fixture');
    assert.equal(created.form.success_url, WEB + '/billing?checkout=returned');
    assert.equal(created.form.cancel_url, WEB + '/billing?checkout=cancelled');
    assert.equal(created.version, '2026-09-30.endive');
    assert.equal((await (await f.call('status', 'account-alice')).json()).accessAllowed, false);
  } finally { await f.close(); }
});

const subscription = (customer, status = 'active') => ({ id: 'sub_fixture', object: 'subscription', customer, livemode: false, status, pause_collection: null, items: { data: [{ price: { id: 'price_fixture', livemode: false }, quantity: 1 }], has_more: false } });
const event = (id, customer, status = 'active') => ({ id, object: 'event', api_version: '2026-09-30.endive', livemode: false, type: 'customer.subscription.updated', created: 100, data: { object: { ...subscription(customer, status), metadata: { accountId: 'account-alice' } } } });

test('private interface rejects browser origins, missing identity, and caller-selected billing fields', async () => {
  const f = await fixture();
  try {
    assert.equal((await f.call('createcheckout')).status, 401);
    for (const extra of [{ accountId: 'account-bob' }, { customer: 'cus_attacker' }, { subscription: 'sub_attacker' }, { price: 'price_attacker' }, { return_url: 'https://attacker.invalid' }]) assert.equal((await f.call('createcheckout', 'account-alice', extra)).status, 400);
    const browser = await f.mf.dispatchFetch('https://billing.internal/createcheckout', { method: 'POST', headers: { Origin: WEB, 'Content-Type': 'application/json', 'X-Stealth-Account-Id': 'account-bob' }, body: '{}' });
    assert.equal(browser.status, 403); assert.equal(browser.headers.has('Access-Control-Allow-Origin'), false);
    assert.equal((await f.mf.dispatchFetch('https://public.invalid/createcheckout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })).status, 404);
    assert.equal(f.calls.length, 0);
  } finally { await f.close(); }
});

test('disabled, live, wrong-account, and live-price configuration cannot create Stripe objects', async () => {
  for (const overrides of [{ BILLING_TEST_ENABLED: 'false' }, { BILLING_MODE: 'live' }, { STRIPE_SECRET_KEY: 'sk_live_synthetic_never_used' }]) {
    const f = await fixture(overrides);
    try { assert.equal((await f.call('createcheckout', 'account-alice')).status, 503); assert.equal(f.calls.length, 0); }
    finally { await f.close(); }
  }
  for (const change of ['accountId', 'priceLive']) {
    const f = await fixture();
    try {
      f.state[change] = change === 'accountId' ? 'acct_wrong' : true;
      assert.equal((await f.call('createcheckout', 'account-alice')).status, 503);
      assert.equal(f.customers.size, 0);
    } finally { await f.close(); }
  }
});

test('customer creation with a lost HTTP response survives eviction and retries the same idempotency key', async () => {
  const f = await fixture();
  try {
    f.state.dropCustomerOnce = true;
    const failed = await f.call('createcheckout', 'account-alice'); assert.equal(failed.status, 503); await failed.arrayBuffer();
    assert.equal(f.customers.size, 1);
    await f.mf.unsafeEvictDurableObject('billing-candidate', 'BillingLedger', { name: 'test:acct_fixture' });
    assert.equal((await f.call('createcheckout', 'account-alice')).status, 200);
    assert.equal(f.customers.size, 1);
    const creates = f.calls.filter(call => call.path === '/v1/customers' && call.method === 'POST');
    assert.equal(creates.length, 2); assert.equal(creates[0].idempotencyKey, creates[1].idempotencyKey);
    assert.deepEqual(creates[0].form, creates[1].form);
  } finally { await f.close(); }
});

test('portal uses only the bound customer and rejects mismatched Stripe ownership', async () => {
  const f = await fixture();
  try {
    assert.equal((await f.call('createportal', 'account-unknown')).status, 404);
    assert.equal((await f.call('createcheckout', 'account-alice')).status, 200);
    assert.equal((await f.call('createcheckout', 'account-bob')).status, 200);
    assert.equal((await f.call('createportal', 'account-alice')).status, 200);
    const portal = f.calls.find(call => call.path === '/v1/billing_portal/sessions');
    assert.equal(portal.form.customer, [...f.customers.keys()][0]); assert.equal(portal.form.return_url, WEB + '/billing');
    assert.equal(portal.form.configuration, 'bpc_fixture');
    f.state.responseCustomer = [...f.customers.keys()][1];
    assert.equal((await f.call('createportal', 'account-alice')).status, 503);
    const alice = [...f.customers.values()][0]; alice.metadata.billing_binding_id = 'different-owner';
    assert.equal((await f.call('status', 'account-alice')).status, 503);
  } finally { await f.close(); }
});

test('signed webhooks reconcile canonical subscriptions, tolerate replay, and ignore stale event state', async () => {
  const f = await fixture();
  try {
    await f.call('createcheckout', 'account-alice'); await f.call('createcheckout', 'account-bob');
    const [alice, bob] = f.customers.keys();
    f.subscriptions.set(alice, [subscription(alice)]);
    const newer = event('evt_newer', alice, 'canceled'); newer.created = 200;
    assert.equal((await f.webhook(newer)).status, 200);
    const beforeDuplicate = f.calls.length;
    assert.equal((await (await f.webhook(newer)).json()).duplicate, true);
    assert.equal(f.calls.length, beforeDuplicate);
    assert.deepEqual(await (await f.call('status', 'account-alice')).json(), { mode: 'test', billingStatus: 'active', testSubscriptionActive: true, accessAllowed: false });
    assert.equal((await f.call('createcheckout', 'account-alice')).status, 409);
    f.subscriptions.set(alice, [subscription(alice, 'canceled')]);
    assert.equal((await f.webhook(event('evt_older', alice, 'active'))).status, 200);
    assert.equal((await (await f.call('status', 'account-alice')).json()).testSubscriptionActive, false);
    assert.equal((await f.webhook(event('evt_other', bob, 'active'))).status, 200);
    assert.equal((await (await f.call('status', 'account-bob')).json()).testSubscriptionActive, false);
    assert.equal((await (await f.webhook(event('evt_unknown', 'cus_unknown'))).json()).ignored, true);
  } finally { await f.close(); }
});

test('webhook authentication rejects tampering, stale signatures, live mode, wrong account, and oversized bodies', async () => {
  const f = await fixture();
  try {
    const payload = event('evt_invalid', 'cus_unknown');
    assert.equal((await f.webhook(payload, { signature: 't=' + Math.floor(Date.now() / 1000) + ',v1=' + '0'.repeat(64) })).status, 400);
    assert.equal((await f.webhook(payload, { timestamp: Math.floor(Date.now() / 1000) - 600 })).status, 400);
    assert.equal((await f.webhook({ ...payload, livemode: true })).status, 400);
    assert.equal((await f.webhook({ ...payload, account: 'acct_other' })).status, 400);
    assert.equal((await f.webhook({ ...payload, api_version: 'old-version' })).status, 400);
    assert.equal((await f.webhook(payload, { body: ' '.repeat(256 * 1024 + 1) })).status, 413);
    assert.equal((await f.webhook(payload, { body: '{' })).status, 400);
    const timestamp = Math.floor(Date.now() / 1000), original = JSON.stringify(payload);
    const signature = createHmac('sha256', SECRET).update(timestamp + '.' + original).digest('hex');
    assert.equal((await f.webhook(payload, { timestamp, body: original + ' ', signature: `t=${timestamp},v1=${signature}` })).status, 400);
    assert.equal(f.calls.length, 0);
  } finally { await f.close(); }
});

test('failed webhook reconciliation is retried and mismatched canonical customers fail closed', async () => {
  const f = await fixture();
  try {
    await f.call('createcheckout', 'account-alice'); const [alice] = f.customers.keys();
    f.subscriptions.set(alice, [subscription(alice)]);
    f.state.failure = { path: '/v1/subscriptions', kind: 'html' };
    const delivery = event('evt_retry', alice);
    assert.equal((await f.webhook(delivery)).status, 503);
    assert.equal((await f.call('status', 'account-alice')).status, 503);
    f.state.failure = null;
    const retried = await f.webhook(delivery); assert.equal(retried.status, 200); assert.equal((await retried.json()).duplicate, undefined);
    assert.equal((await (await f.call('status', 'account-alice')).json()).testSubscriptionActive, true);
    f.subscriptions.set(alice, [subscription('cus_someoneElse')]);
    assert.equal((await f.call('status', 'account-alice')).status, 503);
  } finally { await f.close(); }
});

test('upstream JSON errors and timeouts return unavailable without granting access', async () => {
  for (const kind of ['json', 'hang']) {
    const f = await fixture();
    try {
      f.state.failure = { path: '/v1/account', kind, status: 429 };
      const start = Date.now(), response = await f.call('createcheckout', 'account-alice');
      assert.equal(response.status, 503); assert.equal(f.customers.size, 0);
      assert.ok(Date.now() - start < 7000, 'dependency deadline must bound the request');
      assert.equal((await response.json()).accessAllowed, undefined);
    } finally { await f.close(); }
  }
});
