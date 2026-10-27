import { test } from 'node:test';
import assert from 'node:assert/strict';
import { authStore } from '../src/auth-store.js';

test('account storage adapter preserves values, JSON reads, TTL and deletion', async () => {
  const data = new Map(); let written;
  const store = authStore({ async fetch(url, init) {
    const body = JSON.parse(init.body);
    if (url.endsWith('/get')) return Response.json({ value: data.get(body.key) ?? null });
    if (url.endsWith('/put')) { written = body; data.set(body.key, body.value); }
    else if (url.endsWith('/delete')) data.delete(body.key);
    return Response.json({ ok: true });
  } });
  await store.put('session:synthetic', '{"expires":123}', { expirationTtl: 60 });
  assert.equal(written.expirationTtl, 60);
  assert.deepEqual(await store.get('session:synthetic', { type: 'json' }), { expires: 123 });
  assert.equal(await store.get('session:synthetic'), '{"expires":123}');
  await store.delete('session:synthetic');
  assert.equal(await store.get('session:synthetic', { type: 'json' }), null);
});

test('account storage errors and malformed responses fail closed', async () => {
  for (const response of [new Response('{}', { status: 503 }), Response.json({ value: false }), Response.json({}), new Response('invalid')]) {
    await assert.rejects(authStore({ fetch: () => response }).get('synthetic'));
  }
  for (const operation of ['put', 'delete']) {
    await assert.rejects(authStore({ fetch: () => Response.json({ ok: false }) })[operation]('synthetic', 'value'));
  }
});

test('endpoint ownership adapter requires an explicit boolean decision', async () => {
  assert.equal(await authStore({ fetch: () => Response.json({ allowed: false }) }).claim('digest', 'owner@example.test'), false);
  assert.equal(await authStore({ fetch: () => Response.json({ allowed: true }) }).claim('digest', 'owner@example.test'), true);
  await assert.rejects(authStore({ fetch: () => Response.json({ allowed: 'true' }) }).claim('digest', 'owner@example.test'));
});
