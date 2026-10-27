import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';

const require = createRequire(new URL('../oauth-gateway/package.json', import.meta.url));
const { Miniflare } = require('miniflare');

export function sandboxRuntime(bindings) {
  if (bindings.BILLING_TEST_ENABLED !== 'true' || bindings.BILLING_MODE !== 'test' || !/^(sk|rk)_test_/.test(bindings.STRIPE_SECRET_KEY || '')) throw new Error('Explicit test-mode bindings are required.');
  const observed = [];
  const source = readFileSync(new URL('./worker.js', import.meta.url), 'utf8');
  const mf = new Miniflare({
    name: 'billing-sandbox-smoke', modules: true,
    script: source, compatibilityDate: '2026-07-30',
    bindings, durableObjects: { BILLING: { className: 'BillingLedger', useSQLite: true } },
    outboundService: async request => {
      const url = new URL(request.url);
      if (url.origin !== 'https://api.stripe.com') throw new Error('Unexpected upstream origin.');
      const entry = { method: request.method, path: url.pathname };
      observed.push(entry);
      const requestHeaders = new Headers();
      for (const name of ['Authorization', 'Stripe-Version', 'Content-Type', 'Idempotency-Key']) {
        const value = request.headers.get(name); if (value) requestHeaders.set(name, value);
      }
      let response;
      try {
        response = await fetch(request.url, { method: request.method, redirect: 'manual', headers: requestHeaders, ...(request.method === 'POST' ? { body: await request.text() } : {}), signal: request.signal });
      } catch (error) {
        entry.bridgeErrorName = error.name; entry.bridgeCauseCode = error.cause?.code;
        throw error;
      }
      const body = await response.arrayBuffer();
      let object;
      try { object = JSON.parse(new TextDecoder().decode(body)); } catch {}
      Object.assign(entry, { status: response.status, contentType: response.headers.get('Content-Type'), ...(typeof object?.id === 'string' ? { id: object.id } : {}), ...(typeof object?.customer === 'string' ? { customer: object.customer } : {}) });
      const headers = new Headers(response.headers);
      for (const name of ['Content-Encoding', 'Content-Length', 'Transfer-Encoding', 'Connection']) headers.delete(name);
      return new Response(body, { status: response.status, headers });
    },
  });
  return {
    observed,
    call(path, accountId) {
      if (!['createcheckout', 'createportal', 'status'].includes(path)) throw new Error('Unsupported smoke-test route.');
      return mf.dispatchFetch('https://billing.internal/' + path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Stealth-Account-Id': accountId }, body: '{}' });
    },
    close: () => mf.dispose(),
  };
}
