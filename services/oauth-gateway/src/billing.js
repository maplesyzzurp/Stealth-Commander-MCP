const encoder = new TextEncoder();
const routes = { '/api/billing/status': { method: 'GET', internal: '/status' }, '/api/billing/checkout': { method: 'POST', internal: '/createcheckout', host: 'checkout.stripe.com' }, '/api/billing/portal': { method: 'POST', internal: '/createportal', host: 'billing.stripe.com' }, '/api/billing/webhook': { method: 'POST', internal: '/webhook' } };
const billingStatuses = new Set(['none', 'active', 'pending_or_unpaid']);
class BillingProblem extends Error { constructor(status, message, headers = {}) { super(message); this.status = status; this.headers = headers; } }
const reply = (status, value, headers = {}) => Response.json(value, { status, headers: { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', Vary: 'Cookie', ...headers } });
const unavailable = () => new BillingProblem(503, 'Test billing is unavailable.');
const hex = bytes => Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, '0')).join('');
const digest = async value => hex(await crypto.subtle.digest('SHA-256', encoder.encode(value)));

async function bytes(message, limit) {
  const length = message.headers.get('Content-Length');
  if (length && (!/^\d+$/.test(length) || Number(length) > limit)) throw new BillingProblem(413, 'Billing request body is too large.');
  if (!message.body) return new Uint8Array();
  const reader = message.body.getReader(), parts = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) { await reader.cancel(); throw new BillingProblem(413, 'Billing request body is too large.'); }
      parts.push(value);
    }
  } finally { reader.releaseLock(); }
  const result = new Uint8Array(size);
  let offset = 0;
  for (const part of parts) { result.set(part, offset); offset += part.byteLength; }
  return result;
}

function json(raw) {
  try { return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(raw)); }
  catch { throw new BillingProblem(400, 'Valid JSON required.'); }
}

function configured(env, webhook) {
  if (env.BILLING_TEST_ENABLED !== 'true' || env.BILLING_MODE !== 'test' || typeof env.BILLING_SERVICE?.fetch !== 'function') throw unavailable();
  let origin;
  try { origin = new URL(env.GATEWAY_ORIGIN); }
  catch { throw unavailable(); }
  if (origin.origin !== env.GATEWAY_ORIGIN || origin.protocol !== 'https:' || origin.port || origin.username || origin.password) throw unavailable();
  if (!webhook && (typeof env.BILLING_CSRF_SECRET !== 'string' || env.BILLING_CSRF_SECRET.length < 32 || env.BILLING_CSRF_SECRET.length > 1024 || !env.AUTH_RATE_LIMITER)) throw unavailable();
}

function websiteSession(request, context) {
  const sessions = (request.headers.get('Cookie') || '').split(';').map(part => part.trim()).filter(part => part.startsWith('sc_session='));
  if (!context?.user || !/^sc_sess_[a-f0-9]{48}$/.test(context.session || '') || sessions.length !== 1 || sessions[0].slice('sc_session='.length) !== context.session) throw new BillingProblem(401, 'Sign in to use billing.');
}

async function accountId(user) {
  let identity;
  if (user.id !== undefined) {
    if (typeof user.id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(user.id)) throw unavailable();
    identity = ['stealth-billing-v1', 'id', user.id];
  } else {
    if (typeof user.email !== 'string' || user.email.length > 254 || user.email !== user.email.trim().toLowerCase() || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(user.email) || typeof user.created_at !== 'string') throw unavailable();
    const created = Date.parse(user.created_at);
    if (!Number.isFinite(created) || created <= 0 || created > Date.now() || new Date(created).toISOString() !== user.created_at) throw unavailable();
    identity = ['stealth-billing-v1', 'profile', user.email, user.created_at];
  }
  return 'scb_' + await digest(JSON.stringify(identity));
}

function sameOrigin(request, env) {
  const origin = request.headers.get('Origin'), site = request.headers.get('Sec-Fetch-Site'), mode = request.headers.get('Sec-Fetch-Mode');
  if ((request.method === 'POST' ? origin !== env.GATEWAY_ORIGIN : origin !== null && origin !== env.GATEWAY_ORIGIN) || site !== 'same-origin' || mode !== null && !['cors', 'same-origin'].includes(mode)) throw new BillingProblem(403, 'Billing origin is not allowed.');
}

async function csrfToken(env, context, identity) {
  const key = await crypto.subtle.importKey('raw', encoder.encode(env.BILLING_CSRF_SECRET), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return 'v1.' + hex(await crypto.subtle.sign('HMAC', key, encoder.encode(JSON.stringify(['stealth-billing-csrf-v1', env.GATEWAY_ORIGIN, identity, context.session]))));
}

function equal(a, b) {
  if (typeof a !== 'string' || a.length !== b.length) return false;
  let mismatch = 0;
  for (let index = 0; index < a.length; index++) mismatch |= a.charCodeAt(index) ^ b.charCodeAt(index);
  return mismatch === 0;
}

async function limited(env, key) {
  const binding = env.AUTH_RATE_LIMITER;
  let result;
  if (typeof binding?.fetch === 'function') {
    const response = await binding.fetch('https://rate-limit.internal/check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }), signal: AbortSignal.timeout(5000) });
    if (response.status !== 200) throw unavailable();
    try { result = json(await bytes(response, 1024)); }
    catch { throw unavailable(); }
  } else if (typeof binding?.limit === 'function') result = await binding.limit({ key });
  else throw unavailable();
  if (typeof result?.success !== 'boolean') throw unavailable();
  if (!result.success) throw new BillingProblem(429, 'Too many billing requests. Try again later.', { 'Retry-After': '60' });
}

async function abuseLimit(request, env, identity, kind) {
  const source = request.headers.get('CF-Connecting-IP');
  if (!source || !/^[0-9a-fA-F:.]{3,64}$/.test(source)) throw unavailable();
  await limited(env, `billing:${kind}:source:${await digest(source)}`);
  await limited(env, `billing:${kind}:account:${await digest(identity)}`);
}

async function service(request, env, route, identity, raw) {
  const headers = new Headers({ 'Content-Type': 'application/json' });
  if (identity) headers.set('X-Stealth-Account-Id', identity);
  else headers.set('Stripe-Signature', request.headers.get('Stripe-Signature'));
  const response = await env.BILLING_SERVICE.fetch('https://billing.internal' + route.internal, { method: 'POST', headers, body: raw, redirect: 'manual', signal: AbortSignal.timeout(30000) });
  if (response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase() !== 'application/json') throw unavailable();
  let result;
  try { result = json(await bytes(response, 4096)); }
  catch { throw unavailable(); }
  if (!response.ok) {
    if (![400, 401, 403, 404, 409, 413, 415, 429, 503].includes(response.status) || typeof result?.error !== 'string' || result.error.length > 256 || /[\u0000-\u001f]/.test(result.error)) throw unavailable();
    throw new BillingProblem(response.status, result.error);
  }
  if (response.status !== 200) throw unavailable();
  return result;
}

export async function handleBilling(request, env, context) {
  try {
    const url = new URL(request.url), route = routes[url.pathname];
    if (!route) return reply(404, { error: 'Billing route not found.' });
    if (request.method !== route.method) return reply(405, { error: 'Billing method is not supported.' }, { Allow: route.method });
    if (url.search) throw new BillingProblem(400, 'Billing route does not accept query parameters.');
    const webhook = route.internal === '/webhook';
    configured(env, webhook);
    if (url.origin !== env.GATEWAY_ORIGIN) throw new BillingProblem(421, 'Billing origin does not match.');
    if (webhook) {
      const signature = request.headers.get('Stripe-Signature');
      if (!signature || signature.length > 2048 || /[\r\n]/.test(signature)) throw new BillingProblem(400, 'Webhook signature required.');
      if (request.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase() !== 'application/json') throw new BillingProblem(415, 'JSON required.');
      const result = await service(request, env, route, null, await bytes(request, 256 * 1024));
      if (result?.received !== true || result.duplicate !== undefined && typeof result.duplicate !== 'boolean' || result.ignored !== undefined && typeof result.ignored !== 'boolean') throw unavailable();
      return reply(200, { received: true, ...(result.duplicate === true ? { duplicate: true } : {}), ...(result.ignored === true ? { ignored: true } : {}) });
    }
    websiteSession(request, context);
    sameOrigin(request, env);
    const identity = await accountId(context.user), token = await csrfToken(env, context, identity);
    if (request.method === 'POST') {
      if (!equal(request.headers.get('X-CSRF-Token'), token)) throw new BillingProblem(403, 'Billing request expired. Refresh and try again.');
      if (request.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase() !== 'application/json') throw new BillingProblem(415, 'JSON required.');
      const body = json(await bytes(request, 2048));
      if (!body || typeof body !== 'object' || Array.isArray(body) || Object.keys(body).length) throw new BillingProblem(400, 'Billing request body must be empty.');
    }
    await abuseLimit(request, env, identity, request.method === 'GET' ? 'status' : 'action');
    const result = await service(request, env, route, identity, '{}');
    if (result?.mode !== 'test' || result.accessAllowed !== undefined && result.accessAllowed !== false) throw unavailable();
    if (request.method === 'GET') {
      if (result.accessAllowed !== false || !billingStatuses.has(result.billingStatus) || typeof result.testSubscriptionActive !== 'boolean' || result.testSubscriptionActive !== (result.billingStatus === 'active')) throw unavailable();
      return reply(200, { mode: 'test', billingStatus: result.billingStatus, testSubscriptionActive: result.testSubscriptionActive, accessAllowed: false, csrfToken: token });
    }
    let hosted;
    try { hosted = new URL(result.url); } catch { throw unavailable(); }
    if (hosted.protocol !== 'https:' || hosted.hostname !== route.host || hosted.port || hosted.username || hosted.password) throw unavailable();
    return reply(200, { mode: 'test', url: hosted.href, accessAllowed: false });
  } catch (error) {
    return reply(error instanceof BillingProblem ? error.status : 503, { error: error instanceof BillingProblem ? error.message : 'Test billing is unavailable.' }, error instanceof BillingProblem ? error.headers : {});
  }
}
