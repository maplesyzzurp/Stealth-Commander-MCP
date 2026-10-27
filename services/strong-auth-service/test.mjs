import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

const require = createRequire(new URL('../oauth-gateway/package.json', import.meta.url));
const { Miniflare } = require('miniflare');
const options = {
  name: 'auth-store-test',
  modules: true,
  script: readFileSync(new URL('./worker.js', import.meta.url), 'utf8') + `
    export class AuditStore extends AuthStore {
      async fetch(request) {
        if (new URL(request.url).pathname === '/audit-seed') {
          for (const item of await request.json()) await this.ctx.storage.put(item.key, item.record);
          this.ctx.storage.sql.exec('INSERT OR IGNORE INTO migration_state (version) VALUES (1)');
          return Response.json({ ok: true });
        }
        if (new URL(request.url).pathname === '/audit-count') return Response.json({ count: (await this.ctx.storage.list()).size });
        return super.fetch(request);
      }
    }`,
  compatibilityDate: '2026-07-30',
  durableObjects: { STORE: { className: 'AuditStore', useSQLite: true } },
  kvNamespaces: ['LEGACY_AUTH'],
  outboundService: () => { throw new Error('External network is disabled.'); },
};
let mf, legacy;
before(async () => {
  mf = new Miniflare(options);
  legacy = await mf.getKVNamespace('LEGACY_AUTH');
  await legacy.put('accountkey:fixture', 'old');
  assert.equal((await call('/initialize', {})).status, 200);
});
after(async () => { await mf?.dispose(); });

async function call(path, body, target = mf) {
  return target.dispatchFetch('https://auth-store.internal' + path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
}
async function get(key) {
  const response = await call('/get', { key });
  assert.equal(response.status, 200);
  return (await response.json()).value;
}

test('reads legacy state once and serves immediately committed replacements', async () => {
  await legacy.put('accountkey:fixture', 'old');
  assert.equal(await get('accountkey:fixture'), 'old');
  await legacy.put('accountkey:fixture', 'stale-overwrite');
  assert.equal(await get('accountkey:fixture'), 'old');
  assert.equal((await call('/put', { key: 'accountkey:fixture', value: 'new' })).status, 200);
  assert.deepEqual(await Promise.all(Array.from({ length: 20 }, () => get('accountkey:fixture'))), Array(20).fill('new'));
});

test('deleting a legacy key before its first read persists a tombstone', async () => {
  await legacy.put('session:deleted', 'legacy-session');
  assert.equal((await call('/delete', { key: 'session:deleted' })).status, 200);
  assert.equal(await get('session:deleted'), null);
  await legacy.put('session:deleted', 'late-legacy-write');
  assert.equal(await get('session:deleted'), null);
  assert.equal((await call('/put', { key: 'session:deleted', value: 'explicit-new-value' })).status, 200);
  assert.equal(await get('session:deleted'), 'explicit-new-value');
});

test('expiration and imported explicit expiry cannot resurrect legacy credentials', async () => {
  await legacy.put('session:ttl', 'legacy-session');
  assert.equal((await call('/put', { key: 'session:ttl', value: 'short-lived', expirationTtl: 1 })).status, 200);
  assert.equal(await get('session:ttl'), 'short-lived');
  await new Promise(resolve => setTimeout(resolve, 1100));
  assert.equal(await get('session:ttl'), null);
  await legacy.put('session:ttl', 'late-legacy-write');
  assert.equal(await get('session:ttl'), null);
  await legacy.put('deviceauth:expired', JSON.stringify({ expiresAt: Date.now() - 1000, email: 'synthetic@example.invalid' }));
  assert.equal(await get('deviceauth:expired'), null);
  await legacy.put('deviceauth:expired', JSON.stringify({ expiresAt: Date.now() + 100000 }));
  assert.equal(await get('deviceauth:expired'), null);
});

test('parallel first reads and deletes cannot restore a revoked record', async () => {
  for (let i = 0; i < 12; i++) {
    const key = 'deviceauth:race-' + i;
    await legacy.put(key, 'legacy-device');
    const [read, deleted] = await Promise.all([call('/get', { key }), call('/delete', { key })]);
    assert.equal(read.status, 200);
    assert.equal(deleted.status, 200);
    assert.equal(await get(key), null);
  }
});

test('SQLite retains records and tombstones after object eviction', async () => {
  const value = 'é'.repeat(131073);
  assert.equal((await call('/put', { key: 'large-record', value })).status, 200);
  await legacy.put('session:evicted', 'legacy-session');
  assert.equal((await call('/delete', { key: 'session:evicted' })).status, 200);
  await mf.unsafeEvictDurableObject('auth-store-test', 'AuditStore', { name: 'account-authority' });
  assert.equal(await get('large-record'), value);
  assert.equal(await get('session:evicted'), null);
});

test('a capability has one immutable owner even with concurrent contenders', async () => {
  const contenders = await Promise.all(['alice', 'bob'].map(owner => call('/claim', { key: 'same-capability', owner })));
  for (const response of contenders) assert.equal(response.status, 200);
  const results = await Promise.all(contenders.map(response => response.json()));
  assert.equal(results.filter(result => result.allowed === true).length, 1);
  assert.equal(results.filter(result => result.allowed === false).length, 1);
  const winner = results[0].allowed ? 'alice' : 'bob';
  const loser = winner === 'alice' ? 'bob' : 'alice';
  assert.deepEqual(await (await call('/claim', { key: 'same-capability', owner: winner })).json(), { allowed: true });
  assert.equal((await call('/delete', { key: 'same-capability' })).status, 200);
  assert.equal((await call('/put', { key: 'same-capability', value: loser })).status, 200);
  await mf.unsafeEvictDurableObject('auth-store-test', 'AuditStore', { name: 'account-authority' });
  assert.deepEqual(await (await call('/claim', { key: 'same-capability', owner: loser })).json(), { allowed: false });
  assert.deepEqual(await (await call('/claim', { key: 'same-capability', owner: winner })).json(), { allowed: true });
  assert.deepEqual(await (await call('/claim', { key: 'new-capability', owner: loser })).json(), { allowed: true });
});

test('malformed, oversized, and unsupported requests fail closed', async () => {
  const bad = [
    ['/get', {}, 400], ['/get', { key: '' }, 400], ['/get', { key: 'é'.repeat(257) }, 400],
    ['/get', { key: 'fixture', extra: true }, 400], ['/put', { key: 'fixture', value: 1 }, 400],
    ['/put', { key: 'fixture', value: 'x', expirationTtl: 0 }, 400],
    ['/put', { key: 'fixture', value: 'x', expirationTtl: '60' }, 400],
    ['/put', { key: 'fixture', value: 'x', expirationTtl: 1.5 }, 400],
    ['/put', { key: 'fixture', value: 'x'.repeat(1024 * 1024 + 1) }, 413],
    ['/delete', { key: 'fixture', value: 'ignored' }, 400], ['/list', {}, 400],
    ['/claim', { key: 'fixture' }, 400], ['/claim', { key: 'fixture', owner: '' }, 400],
    ['/claim', { key: 'fixture', owner: ['alice'] }, 400],
    ['/claim', { key: 'fixture', owner: 'é'.repeat(257) }, 400],
    ['/initialize', { key: 'ignored' }, 400],
  ];
  for (const [path, body, status] of bad) assert.equal((await call(path, body)).status, status);
  assert.equal((await mf.dispatchFetch('https://auth-store.internal/get')).status, 404);
  assert.equal((await mf.dispatchFetch('https://unexpected.example/get', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"key":"fixture"}' })).status, 404);
  assert.equal((await mf.dispatchFetch('https://auth-store.internal/get', { method: 'POST', body: '{}' })).status, 415);
  assert.equal((await mf.dispatchFetch('https://auth-store.internal/get', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '[' })).status, 400);
  assert.equal((await mf.dispatchFetch('https://auth-store.internal/get', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: ' '.repeat(2 * 1024 * 1024 + 1) })).status, 413);
});

test('unavailable migration storage blocks initialization and every state operation', async () => {
  const isolated = new Miniflare({ ...options, kvNamespaces: [] });
  try {
    assert.equal((await call('/initialize', {}, isolated)).status, 503);
    assert.equal((await call('/get', { key: 'user:unavailable' }, isolated)).status, 503);
    assert.equal((await call('/put', { key: 'user:unavailable', value: 'new' }, isolated)).status, 503);
    assert.equal((await call('/claim', { key: 'capability', owner: 'synthetic' }, isolated)).status, 503);
  } finally { await isolated.dispose(); }
});

test('initialization claims existing PCs before their owners next sign in', async () => {
  const isolated = new Miniflare(options);
  try {
    const kv = await isolated.getKVNamespace('LEGACY_AUTH');
    const path = '/' + 'a'.repeat(48) + '/mcp';
    const key = createHash('sha256').update(path).digest('hex');
    await kv.put('user:alice@example.invalid', JSON.stringify({ email: 'alice@example.invalid', devices: [{ mcp_url: 'https://alice.trycloudflare.com' + path }] }));
    assert.equal((await call('/claim', { key, owner: 'bob@example.invalid' }, isolated)).status, 503);
    assert.deepEqual(await (await call('/initialize', {}, isolated)).json(), { ok: true });
    assert.deepEqual(await (await call('/initialize', {}, isolated)).json(), { ok: true });
    assert.deepEqual(await (await call('/claim', { key, owner: 'bob@example.invalid' }, isolated)).json(), { allowed: false });
    assert.deepEqual(await (await call('/claim', { key, owner: 'alice@example.invalid' }, isolated)).json(), { allowed: true });
  } finally { await isolated.dispose(); }
});

test('conflicting legacy owners fail initialization without activating the store', async () => {
  const isolated = new Miniflare(options);
  try {
    const kv = await isolated.getKVNamespace('LEGACY_AUTH');
    const path = '/' + 'b'.repeat(48) + '/mcp';
    const key = createHash('sha256').update(path).digest('hex');
    for (const name of ['alice', 'bob']) await kv.put('user:' + name + '@example.invalid', JSON.stringify({ email: name + '@example.invalid', devices: [{ mcp_url: 'https://' + name + '.trycloudflare.com' + path }] }));
    assert.equal((await call('/initialize', {}, isolated)).status, 503);
    assert.equal((await call('/claim', { key, owner: 'bob@example.invalid' }, isolated)).status, 503);
    await kv.delete('user:bob@example.invalid');
    assert.equal((await call('/initialize', {}, isolated)).status, 200);
    assert.deepEqual(await (await call('/claim', { key, owner: 'alice@example.invalid' }, isolated)).json(), { allowed: true });
    assert.deepEqual(await (await call('/claim', { key, owner: 'bob@example.invalid' }, isolated)).json(), { allowed: false });
  } finally { await isolated.dispose(); }
});

test('migration rejects more than 1000 users and malformed account profiles', async () => {
  const isolated = new Miniflare(options);
  try {
    const kv = await isolated.getKVNamespace('LEGACY_AUTH');
    for (let offset = 0; offset < 1001; offset += 50) {
      await Promise.all(Array.from({ length: Math.min(50, 1001 - offset) }, (_, i) => {
        const email = 'fixture-' + (offset + i) + '@example.invalid';
        return kv.put('user:' + email, JSON.stringify({ email, devices: [] }));
      }));
    }
    assert.equal((await call('/initialize', {}, isolated)).status, 503);
    assert.equal((await call('/get', { key: 'user:fixture-0@example.invalid' }, isolated)).status, 503);
    await kv.delete('user:fixture-1000@example.invalid');
    await kv.put('user:fixture-0@example.invalid', JSON.stringify({ email: 'different@example.invalid', devices: [] }));
    assert.equal((await call('/initialize', {}, isolated)).status, 503);
  } finally { await isolated.dispose(); }
});


test('unknown reads, unknown logout deletions and expiry create no persistent rows after eviction', async () => {
  const namespace = await mf.getDurableObjectNamespace('STORE');
  const stub = namespace.get(namespace.idFromName('account-authority'));
  const count = async () => (await (await stub.fetch('https://fixture.internal/audit-count')).json()).count;
  const beforeCount = await count();
  for (let i = 0; i < 40; i++) assert.equal(await get('session:unknown-' + i), null);
  for (let i = 0; i < 10; i++) assert.equal((await call('/delete', { key: 'session:logout-miss-' + i })).status, 200);
  assert.equal(await count(), beforeCount);
  await call('/put', { key: 'session:expiry-cleanup', value: '{}', expirationTtl: 1 });
  assert.equal(await count(), beforeCount + 1);
  await new Promise(resolve => setTimeout(resolve, 1100));
  assert.equal(await get('session:expiry-cleanup'), null);
  assert.equal(await count(), beforeCount);
  await mf.unsafeEvictDurableObject('auth-store-test', 'AuditStore', { name: 'account-authority' });
  assert.equal(await count(), beforeCount);
});

test('a completed full migration imports OAuth and closes all fallback without losing newer overrides', async () => {
  const isolated = new Miniflare({ ...options, kvNamespaces: ['LEGACY_AUTH', 'LEGACY_OAUTH'], bindings: { OAUTH_MIGRATION_REQUIRED: 'true' } });
  try {
    const oldAuth = await isolated.getKVNamespace('LEGACY_AUTH'), oldOauth = await isolated.getKVNamespace('LEGACY_OAUTH');
    await oldAuth.put('accountkey:before', 'legacy-authority');
    await oldOauth.put('grant:synthetic:id', JSON.stringify({ expiresAt: Math.floor(Date.now() / 1000) + 3600 }));
    assert.equal((await call('/initialize', {}, isolated)).status, 200);
    const read = async key => (await (await call('/get', { key }, isolated)).json()).value;
    assert.equal(await read('accountkey:before'), 'legacy-authority');
    assert.ok(await read('oauth:grant:synthetic:id'));
    await oldAuth.put('session:late', 'must-not-import');
    assert.equal(await read('session:late'), null);
    await call('/delete', { key: 'accountkey:before' }, isolated);
    assert.equal(await read('accountkey:before'), null);
    await oldAuth.put('accountkey:before', 'stale-resurrection');
    await isolated.unsafeEvictDurableObject('auth-store-test', 'AuditStore', { name: 'account-authority' });
    assert.equal(await read('accountkey:before'), null);
  } finally { await isolated.dispose(); }
});

test('required OAuth migration binding and conditional transaction validation fail closed', async () => {
  const isolated = new Miniflare({ ...options, bindings: { OAUTH_MIGRATION_REQUIRED: 'true' } });
  try { assert.equal((await call('/initialize', {}, isolated)).status, 503); }
  finally { await isolated.dispose(); }
  for (const body of [{ checks: [], puts: 'invalid', deletes: [] }, { checks: [{ key: 'a' }], puts: [], deletes: [] }, { checks: [], puts: [{ key: 'a', value: 'v', expirationTtl: -1 }], deletes: [] }]) assert.equal((await call('/transact', body)).status, 400);
  const key = 'cas-fixture';
  const outcomes = await Promise.all(['one', 'two'].map(value => call('/transact', { checks: [{ key, value: null }], puts: [{ key, value }], deletes: [] })));
  assert.equal((await Promise.all(outcomes.map(r => r.json()))).filter(v => v.committed).length, 1);
});


test('upgrading an already migrated v1 object preserves committed overrides and revoked tombstones', async () => {
  const isolated = new Miniflare(options);
  try {
    const old = await isolated.getKVNamespace('LEGACY_AUTH');
    await old.put('accountkey:v1-override', 'stale-legacy-value');
    await old.put('session:v1-revoked', 'stale-legacy-session');
    const namespace = await isolated.getDurableObjectNamespace('STORE');
    await namespace.get(namespace.idFromName('account-authority')).fetch('https://fixture.internal/audit-seed', { method: 'POST', body: JSON.stringify([{ key: 'accountkey:v1-override', record: { value: 'committed-replacement', expiresAt: null } }, { key: 'session:v1-revoked', record: { value: null, expiresAt: null } }]) });
    assert.equal((await call('/initialize', {}, isolated)).status, 200);
    assert.equal((await (await call('/get', { key: 'accountkey:v1-override' }, isolated)).json()).value, 'committed-replacement');
    assert.equal((await (await call('/get', { key: 'session:v1-revoked' }, isolated)).json()).value, null);
    await isolated.unsafeEvictDurableObject('auth-store-test', 'AuditStore', { name: 'account-authority' });
    assert.equal((await (await call('/get', { key: 'session:v1-revoked' }, isolated)).json()).value, null);
  } finally { await isolated.dispose(); }
});


test('interruption after import commit cannot resurrect a v1 revoked credential on migration retry', async () => {
  const marker = "this.ctx.storage.sql.exec('INSERT INTO migration_state (version) VALUES (2)');";
  const isolated = new Miniflare({ ...options, script: 'let stopMigrationOnce = true;\n' + options.script.replace(marker, "if (stopMigrationOnce) { stopMigrationOnce = false; throw new Error('Synthetic interruption before migration marker'); } " + marker) });
  try {
    const old = await isolated.getKVNamespace('LEGACY_AUTH');
    await old.put('session:interrupted-revoked', 'stale-legacy-session');
    const namespace = await isolated.getDurableObjectNamespace('STORE');
    await namespace.get(namespace.idFromName('account-authority')).fetch('https://fixture.internal/audit-seed', { method: 'POST', body: JSON.stringify([{ key: 'session:interrupted-revoked', record: { value: null, expiresAt: null } }]) });
    assert.equal((await call('/initialize', {}, isolated)).status, 503);
    assert.equal((await call('/get', { key: 'session:interrupted-revoked' }, isolated)).status, 503);
    assert.equal((await call('/initialize', {}, isolated)).status, 200);
    assert.equal((await (await call('/get', { key: 'session:interrupted-revoked' }, isolated)).json()).value, null);
    await isolated.unsafeEvictDurableObject('auth-store-test', 'AuditStore', { name: 'account-authority' });
    assert.equal((await (await call('/get', { key: 'session:interrupted-revoked' }, isolated)).json()).value, null);
  } finally { await isolated.dispose(); }
});
