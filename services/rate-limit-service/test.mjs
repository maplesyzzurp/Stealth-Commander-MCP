import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import worker from './worker.js';

const require = createRequire(new URL('../oauth-gateway/package.json', import.meta.url));
const { Miniflare } = require('miniflare');
const request = key => new Request('https://rate-limit.internal/check', {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }),
});

test('real Worker limiter allows, denies, and keeps purpose keys separate', async () => {
  const mf = new Miniflare({
    modules: true, scriptPath: fileURLToPath(new URL('./worker.js', import.meta.url)),
    compatibilityDate: '2026-07-30',
    ratelimits: { LIMITS: { namespace_id: '803847591', simple: { limit: 1, period: 60 } } },
  });
  try {
    const dispatch = key => mf.dispatchFetch('https://rate-limit.internal/check', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }),
    });
    let response = await dispatch('login:synthetic-client');
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { success: true });
    response = await dispatch('login:synthetic-client');
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { success: false });
    response = await dispatch('registration:synthetic-client');
    assert.deepEqual(await response.json(), { success: true });
  } finally { await mf.dispose(); }
});

test('missing or failed limiter cannot authorize a request', async () => {
  assert.equal((await worker.fetch(request('login:fixture'), {})).status, 503);
  assert.equal((await worker.fetch(request('login:fixture'), { LIMITS: { limit: async () => ({}) } })).status, 503);
  assert.equal((await worker.fetch(request('login:fixture'), { LIMITS: { limit: async () => { throw Error('synthetic'); } } })).status, 503);
});

test('malformed, oversized, and unsupported requests are rejected', async () => {
  const env = { LIMITS: { limit: async () => ({ success: true }) } };
  assert.equal((await worker.fetch(new Request('https://rate-limit.internal/check'), env)).status, 404);
  assert.equal((await worker.fetch(new Request('https://rate-limit.internal/check', { method: 'POST', body: '{}' }), env)).status, 415);
  assert.equal((await worker.fetch(request(''), env)).status, 400);
  assert.equal((await worker.fetch(request('x'.repeat(4096)), env)).status, 413);
  assert.equal((await worker.fetch(new Request('https://rate-limit.internal/check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '[' }), env)).status, 400);
});
