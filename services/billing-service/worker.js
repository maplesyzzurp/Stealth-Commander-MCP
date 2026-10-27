import { DurableObject } from 'cloudflare:workers';

const VERSION = '2026-09-30.endive';
const MAX_EVENT = 256 * 1024;
const RETRY_WINDOW = 23 * 60 * 60 * 1000;
const EVENT_RETENTION = 31 * 24 * 60 * 60 * 1000;
const encoder = new TextEncoder();
const supportedEvents = new Set(['customer.subscription.created', 'customer.subscription.updated', 'customer.subscription.deleted', 'checkout.session.completed', 'checkout.session.async_payment_succeeded', 'checkout.session.async_payment_failed', 'invoice.paid', 'invoice.payment_failed']);
const subscriptionStatuses = new Set(['incomplete', 'incomplete_expired', 'trialing', 'active', 'past_due', 'canceled', 'unpaid', 'paused']);
class Problem extends Error { constructor(status, message) { super(message); this.status = status; } }
const reply = (status, value) => Response.json(value, { status, headers: { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' } });

function configuration(env) {
  if (env.BILLING_TEST_ENABLED !== 'true' || (env.BILLING_MODE || 'test') !== 'test' ||
      !/^(sk|rk)_test_[A-Za-z0-9_]+$/.test(env.STRIPE_SECRET_KEY || '') ||
      !/^whsec_[A-Za-z0-9_]+$/.test(env.STRIPE_WEBHOOK_SECRET || '') ||
      !/^acct_[A-Za-z0-9]+$/.test(env.STRIPE_ACCOUNT_ID || '') ||
      !/^price_[A-Za-z0-9]+$/.test(env.STRIPE_PRICE_ID || '') ||
      env.STRIPE_PORTAL_CONFIGURATION_ID !== undefined && !/^bpc_[A-Za-z0-9]+$/.test(env.STRIPE_PORTAL_CONFIGURATION_ID)) throw new Problem(503, 'Test billing is not configured.');
  const origin = new URL(env.WEB_ORIGIN);
  if (origin.protocol !== 'https:' || origin.origin !== env.WEB_ORIGIN || origin.port || origin.username || origin.password) throw new Problem(503, 'Billing return origin is invalid.');
  return { secret: env.STRIPE_SECRET_KEY, webhookSecret: env.STRIPE_WEBHOOK_SECRET, account: env.STRIPE_ACCOUNT_ID, price: env.STRIPE_PRICE_ID, portalConfiguration: env.STRIPE_PORTAL_CONFIGURATION_ID, origin: origin.origin };
}

async function bytes(message, limit) {
  if (Number(message.headers.get('Content-Length')) > limit) throw new Problem(413, 'Body exceeds limit.');
  if (!message.body) return new Uint8Array();
  const reader = message.body.getReader(), chunks = []; let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read(); if (done) break;
      size += value.byteLength;
      if (size > limit) { await reader.cancel(); throw new Problem(413, 'Body exceeds limit.'); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const result = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.byteLength; }
  return result;
}

function json(data) {
  try { return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(data)); }
  catch { throw new Problem(400, 'Valid JSON required.'); }
}

async function verifyEvent(request, cfg) {
  const header = request.headers.get('Stripe-Signature') || '';
  if (header.length > 2048) throw new Problem(400, 'Invalid webhook signature.');
  const fields = header.split(',').map(part => part.trim().split('='));
  const timestamps = fields.filter(([key]) => key === 't');
  const signatures = fields.filter(([key, value]) => key === 'v1' && /^[a-f0-9]{64}$/.test(value)).map(([, value]) => value);
  const timestamp = timestamps[0]?.[1];
  if (timestamps.length !== 1 || !/^\d{10}$/.test(timestamp || '') || Math.abs(Date.now() / 1000 - Number(timestamp)) > 300 || !signatures.length || signatures.length > 10) throw new Problem(400, 'Invalid webhook signature.');
  const raw = await bytes(request, MAX_EVENT), prefix = encoder.encode(timestamp + '.');
  const signed = new Uint8Array(prefix.length + raw.length); signed.set(prefix); signed.set(raw, prefix.length);
  const key = await crypto.subtle.importKey('raw', encoder.encode(cfg.webhookSecret), { name: 'HMAC', hash: 'SHA-256' }, false, ['verify']);
  let valid = false;
  for (const signature of signatures) {
    const digest = Uint8Array.from(signature.match(/../g), pair => parseInt(pair, 16));
    valid = await crypto.subtle.verify('HMAC', key, digest, signed) || valid;
  }
  if (!valid) throw new Problem(400, 'Invalid webhook signature.');
  const event = json(raw);
  if (!event || event.object !== 'event' || !/^evt_[A-Za-z0-9]+$/.test(event.id || '') || event.livemode !== false || event.account !== undefined || event.context !== undefined || event.api_version !== VERSION) throw new Problem(400, 'Unsupported webhook account, mode, or version.');
  if (!supportedEvents.has(event.type)) return null;
  const customer = event.data?.object?.customer;
  if (!/^cus_[A-Za-z0-9]+$/.test(customer || '')) throw new Problem(400, 'Webhook customer is invalid.');
  return { kind: 'webhook', eventId: event.id, customerId: customer };
}

async function stripe(cfg, path, fields, idempotencyKey) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 4000);
  try {
    const response = await fetch('https://api.stripe.com' + path, {
      method: fields ? 'POST' : 'GET', redirect: 'manual', signal: controller.signal,
      headers: { Authorization: 'Bearer ' + cfg.secret, 'Stripe-Version': VERSION, ...(fields ? { 'Content-Type': 'application/x-www-form-urlencoded', 'Idempotency-Key': idempotencyKey } : {}) },
      ...(fields ? { body: new URLSearchParams(fields).toString() } : {}),
    });
    if (!response.ok || !response.headers.get('Content-Type')?.toLowerCase().startsWith('application/json')) throw new Error();
    return json(await bytes(response, 2 * 1024 * 1024));
  } catch { throw new Problem(503, 'Stripe is unavailable or returned an invalid response.'); }
  finally { clearTimeout(timer); }
}

function hostedUrl(value, host) {
  try {
    const url = new URL(value);
    if (url.protocol === 'https:' && url.hostname === host && !url.port && !url.username && !url.password) return url.href;
  } catch {}
  throw new Problem(503, 'Stripe returned an invalid hosted URL.');
}

function checkCustomer(customer, record) {
  if (!customer || customer.object !== 'customer' || !/^cus_[A-Za-z0-9]+$/.test(customer.id || '') || customer.deleted || customer.livemode !== false ||
      record.customerId && customer.id !== record.customerId || customer.metadata?.billing_binding_id !== record.bindingId) throw new Problem(503, 'Billing customer ownership could not be verified.');
}

export class BillingLedger extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    ctx.blockConcurrencyWhile(async () => {
      ctx.storage.sql.exec('CREATE TABLE IF NOT EXISTS accounts (account_id TEXT PRIMARY KEY, customer_id TEXT UNIQUE, record TEXT NOT NULL)');
      ctx.storage.sql.exec('CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, processed_at INTEGER NOT NULL)');
    });
  }

  save(accountId, record) {
    this.ctx.storage.sql.exec('INSERT INTO accounts (account_id, customer_id, record) VALUES (?, ?, ?) ON CONFLICT(account_id) DO UPDATE SET customer_id = excluded.customer_id, record = excluded.record', accountId, record.customerId || null, JSON.stringify(record));
  }

  async verifyConfiguration(cfg) {
    if (this.verified) return;
    const account = await stripe(cfg, '/v1/account');
    if (account?.object !== 'account' || account.id !== cfg.account) throw new Problem(503, 'Stripe account does not match.');
    const price = await stripe(cfg, '/v1/prices/' + cfg.price);
    if (price?.id !== cfg.price || price.object !== 'price' || price.livemode !== false || price.active !== true || price.type !== 'recurring' || price.recurring?.usage_type !== 'licensed') throw new Problem(503, 'A valid test subscription price is required.');
    this.verified = true;
  }

  async customer(cfg, accountId, record, create) {
    if (!record && !create) return null;
    if (!record) {
      record = { bindingId: crypto.randomUUID(), customerIntent: { key: 'billing-customer-' + crypto.randomUUID(), startedAt: Date.now() } };
      this.save(accountId, record);
    }
    if (!record.customerId) {
      if (Date.now() - record.customerIntent.startedAt >= RETRY_WINDOW) throw new Problem(409, 'Customer setup needs manual reconciliation.');
      const customer = await stripe(cfg, '/v1/customers', { description: 'Stealth Commander test customer', 'metadata[billing_binding_id]': record.bindingId }, record.customerIntent.key);
      checkCustomer(customer, record);
      record.customerId = customer.id; delete record.customerIntent;
      this.save(accountId, record);
    } else checkCustomer(await stripe(cfg, '/v1/customers/' + record.customerId), record);
    return record;
  }

  async reconcile(cfg, record) {
    const list = await stripe(cfg, '/v1/subscriptions?' + new URLSearchParams({ customer: record.customerId, status: 'all', limit: '100' }));
    if (list?.object !== 'list' || !Array.isArray(list.data) || list.has_more !== false) throw new Problem(503, 'Subscription state could not be verified.');
    let active = false, ongoing = false;
    for (const sub of list.data) {
      if (sub.object !== 'subscription' || sub.customer !== record.customerId || sub.livemode !== false || !subscriptionStatuses.has(sub.status) || !Array.isArray(sub.items?.data) || sub.items.has_more !== false) throw new Problem(503, 'Subscription ownership could not be verified.');
      if (!['canceled', 'incomplete_expired'].includes(sub.status)) ongoing = true;
      if (sub.status === 'active' && !sub.pause_collection && sub.items.data.some(item => item.price?.id === cfg.price && item.price.livemode === false && item.quantity === 1)) active = true;
    }
    record.status = { mode: 'test', billingStatus: active ? 'active' : ongoing ? 'pending_or_unpaid' : 'none', testSubscriptionActive: active, accessAllowed: false };
    return { ...record.status, ongoing };
  }

  async checkout(cfg, accountId, record) {
    if (record.checkout && (record.checkout.fields['line_items[0][price]'] !== cfg.price || record.checkout.fields.success_url !== cfg.origin + '/billing?checkout=returned')) throw new Problem(409, 'Existing checkout needs reconciliation after a configuration change.');
    if (record.checkout?.id) {
      const existing = await stripe(cfg, '/v1/checkout/sessions/' + record.checkout.id);
      this.checkCheckout(existing, record, record.checkout.id);
      if (existing.status === 'open' && existing.expires_at > Date.now() / 1000) return { mode: 'test', url: hostedUrl(existing.url, 'checkout.stripe.com') };
      if (existing.status !== 'expired') throw new Problem(409, 'Checkout is pending reconciliation.');
      delete record.checkout;
    }
    if (!record.checkout) {
      record.checkout = { key: 'billing-checkout-' + crypto.randomUUID(), startedAt: Date.now(), fields: { customer: record.customerId, mode: 'subscription', 'line_items[0][price]': cfg.price, 'line_items[0][quantity]': '1', success_url: cfg.origin + '/billing?checkout=returned', cancel_url: cfg.origin + '/billing?checkout=cancelled', expires_at: String(Math.floor(Date.now() / 1000) + 3600) } };
      this.save(accountId, record);
    }
    if (Date.now() - record.checkout.startedAt >= RETRY_WINDOW) throw new Problem(409, 'Checkout setup needs manual reconciliation.');
    const session = await stripe(cfg, '/v1/checkout/sessions', record.checkout.fields, record.checkout.key);
    this.checkCheckout(session, record);
    record.checkout.id = session.id; this.save(accountId, record);
    if (session.status !== 'open' || session.expires_at <= Date.now() / 1000) throw new Problem(409, 'Checkout is no longer open. Try again.');
    return { mode: 'test', url: hostedUrl(session.url, 'checkout.stripe.com') };
  }

  checkCheckout(session, record, expectedId) {
    if (session?.object !== 'checkout.session' || !/^cs_test_[A-Za-z0-9]+$/.test(session.id || '') || expectedId && session.id !== expectedId || session.livemode !== false || session.customer !== record.customerId || session.mode !== 'subscription' || !Number.isSafeInteger(session.expires_at)) throw new Problem(503, 'Checkout ownership could not be verified.');
  }

  async fetch(request) {
    try {
      const cfg = configuration(this.env), input = await request.json();
      return await this.ctx.blockConcurrencyWhile(async () => {
        try {
        await this.verifyConfiguration(cfg);
        if (input.kind === 'webhook') {
          this.ctx.storage.sql.exec('DELETE FROM events WHERE processed_at < ?', Date.now() - EVENT_RETENTION);
          if (this.ctx.storage.sql.exec('SELECT event_id FROM events WHERE event_id = ?', input.eventId).toArray().length) return reply(200, { received: true, duplicate: true });
          const row = this.ctx.storage.sql.exec('SELECT account_id, record FROM accounts WHERE customer_id = ?', input.customerId).toArray()[0];
          if (!row) return reply(200, { received: true, ignored: true });
          const record = await this.customer(cfg, row.account_id, JSON.parse(row.record), false);
          await this.reconcile(cfg, record);
          this.ctx.storage.transactionSync(() => {
            this.save(row.account_id, record);
            this.ctx.storage.sql.exec('INSERT INTO events (event_id, processed_at) VALUES (?, ?)', input.eventId, Date.now());
          });
          return reply(200, { received: true });
        }
        const row = this.ctx.storage.sql.exec('SELECT record FROM accounts WHERE account_id = ?', input.accountId).toArray()[0];
        const record = await this.customer(cfg, input.accountId, row && JSON.parse(row.record), input.kind === 'createcheckout');
        if (!record) {
          if (input.kind === 'createportal') throw new Problem(404, 'No billing customer exists.');
          return reply(200, { mode: 'test', billingStatus: 'none', testSubscriptionActive: false, accessAllowed: false });
        }
        if (input.kind === 'createportal') {
          const portal = await stripe(cfg, '/v1/billing_portal/sessions', { customer: record.customerId, return_url: cfg.origin + '/billing', ...(cfg.portalConfiguration ? { configuration: cfg.portalConfiguration } : {}) }, 'billing-portal-' + crypto.randomUUID());
          if (portal?.object !== 'billing_portal.session' || portal.customer !== record.customerId || portal.livemode !== false || portal.return_url !== cfg.origin + '/billing' || cfg.portalConfiguration && portal.configuration !== cfg.portalConfiguration) throw new Problem(503, 'Portal ownership could not be verified.');
          return reply(200, { mode: 'test', url: hostedUrl(portal.url, 'billing.stripe.com') });
        }
        const status = await this.reconcile(cfg, record); this.save(input.accountId, record);
        if (input.kind === 'status') { delete status.ongoing; return reply(200, status); }
        if (status.ongoing) throw new Problem(409, 'Manage the existing subscription in the billing portal.');
        return reply(200, await this.checkout(cfg, input.accountId, record));
        } catch (error) { return reply(error instanceof Problem ? error.status : 503, { error: error instanceof Problem ? error.message : 'Billing is unavailable.' }); }
      });
    } catch (error) { return reply(error instanceof Problem ? error.status : 503, { error: error instanceof Problem ? error.message : 'Billing is unavailable.' }); }
  }
}

export default {
  async fetch(request, env) {
    try {
      const cfg = configuration(env), url = new URL(request.url);
      if (request.method !== 'POST' || url.origin !== 'https://billing.internal' || url.search || !['/createcheckout', '/createportal', '/status', '/webhook'].includes(url.pathname)) return reply(404, { error: 'Not found.' });
      if (request.headers.has('Origin')) return reply(403, { error: 'Use the authenticated website billing route.' });
      if (request.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase() !== 'application/json') return reply(415, { error: 'JSON required.' });
      let input;
      if (url.pathname === '/webhook') { input = await verifyEvent(request, cfg); if (!input) return reply(200, { received: true, ignored: true }); }
      else {
        const body = json(await bytes(request, 2048));
        if (!body || typeof body !== 'object' || Array.isArray(body) || Object.keys(body).length) throw new Problem(400, 'Billing request body must be empty.');
        const accountId = request.headers.get('X-Stealth-Account-Id');
        if (!/^[A-Za-z0-9][A-Za-z0-9@._:-]{0,253}$/.test(accountId || '')) throw new Problem(401, 'Verified account identity is required.');
        input = { kind: url.pathname.slice(1), accountId };
      }
      return await env.BILLING.get(env.BILLING.idFromName('test:' + cfg.account)).fetch('https://billing-ledger.internal', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input) });
    } catch (error) { return reply(error instanceof Problem ? error.status : 503, { error: error instanceof Problem ? error.message : 'Billing is unavailable.' }); }
  },
};
