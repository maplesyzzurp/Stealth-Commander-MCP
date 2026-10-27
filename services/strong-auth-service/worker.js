import { DurableObject } from 'cloudflare:workers';

const MAX_BODY = 2 * 1024 * 1024;
const MAX_VALUE = 1024 * 1024;
const encoder = new TextEncoder();
const paths = new Set(['/get', '/put', '/delete', '/claim', '/initialize', '/transact', '/list']);

function reply(status, value) {
  return Response.json(value, { status, headers: { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' } });
}

function route(request) {
  const url = new URL(request.url);
  return request.method === 'POST' && url.origin === 'https://auth-store.internal' && !url.search && paths.has(url.pathname) ? url.pathname : null;
}

async function parse(request, path) {
  if (request.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase() !== 'application/json') return reply(415, { error: 'JSON required.' });
  if (!request.body) return reply(400, { error: 'Body required.' });
  if (Number(request.headers.get('Content-Length')) > MAX_BODY) return reply(413, { error: 'Body too large.' });
  const reader = request.body.getReader();
  let size = 0, text = '';
  const decoder = new TextDecoder('utf-8', { fatal: true });
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) { text += decoder.decode(); break; }
      size += value.byteLength;
      if (size > MAX_BODY) {
        await reader.cancel();
        return reply(413, { error: 'Body too large.' });
      }
      text += decoder.decode(value, { stream: true });
    }
  } catch { return reply(400, { error: 'Invalid request body.' }); }
  finally { reader.releaseLock(); }
  let body;
  try { body = JSON.parse(text); }
  catch { return reply(400, { error: 'Invalid JSON.' }); }
  const allowed = path === '/initialize' ? [] : path === '/transact' ? ['checks', 'puts', 'deletes'] : path === '/list' ? ['prefix', 'cursor', 'limit'] : path === '/put' ? ['key', 'value', 'expirationTtl'] : path === '/claim' ? ['key', 'owner'] : ['key'];
  if (!body || typeof body !== 'object' || Array.isArray(body) || Object.keys(body).some(key => !allowed.includes(key))) return reply(400, { error: 'Invalid operation.' });
  if (path === '/initialize') return body;
  if (path === '/list') {
    if (typeof body.prefix !== 'string' || body.prefix.length > 512 || body.cursor !== undefined && typeof body.cursor !== 'string' || body.limit !== undefined && (!Number.isInteger(body.limit) || body.limit < 1 || body.limit > 1000)) return reply(400, { error: 'Invalid list.' });
    return body;
  }
  if (path === '/transact') {
    if (!Array.isArray(body.checks) || !Array.isArray(body.puts) || !Array.isArray(body.deletes) || body.checks.length + body.puts.length + body.deletes.length > 200) return reply(400, { error: 'Invalid transaction.' });
    const validKey = key => typeof key === 'string' && key && encoder.encode(key).byteLength <= 512;
    if (body.checks.some(c => !c || !validKey(c.key) || c.value !== null && typeof c.value !== 'string') ||
        body.puts.some(p => !p || !validKey(p.key) || typeof p.value !== 'string' || encoder.encode(p.value).byteLength > MAX_VALUE || p.expirationTtl !== undefined && (!Number.isInteger(p.expirationTtl) || p.expirationTtl < 1 || p.expirationTtl > 315360000)) || body.deletes.some(key => !validKey(key))) return reply(400, { error: 'Invalid transaction.' });
    return body;
  }
  if (typeof body.key !== 'string' || !body.key || encoder.encode(body.key).byteLength > 512) return reply(400, { error: 'Invalid key.' });
  if (path === '/claim' && (typeof body.owner !== 'string' || !body.owner || encoder.encode(body.owner).byteLength > 512)) return reply(400, { error: 'Invalid owner.' });
  if (path === '/put') {
    if (typeof body.value !== 'string' || body.expirationTtl !== undefined &&
        (!Number.isSafeInteger(body.expirationTtl) || body.expirationTtl < 1 || body.expirationTtl > 315360000)) return reply(400, { error: 'Invalid value or expiry.' });
    if (encoder.encode(body.value).byteLength > MAX_VALUE) return reply(413, { error: 'Value too large.' });
  }
  return body;
}

function importedExpiry(key, value) {
  if (value === null || !/^(session|deviceauth):/.test(key)) return null;
  try {
    const expiresAt = JSON.parse(value)?.expiresAt;
    return Number.isFinite(expiresAt) ? expiresAt : null;
  } catch { return null; }
}

function capabilityPath(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password && !url.port &&
      /^[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com$/.test(url.hostname) &&
      /^\/[A-Za-z0-9_-]{32,128}\/mcp$/.test(url.pathname) && !url.search && !url.hash ? url.pathname : null;
  } catch { return null; }
}

async function digest(value) {
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(value))), byte => byte.toString(16).padStart(2, '0')).join('');
}

export class AuthStore extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    ctx.blockConcurrencyWhile(async () => {
      ctx.storage.sql.exec('CREATE TABLE IF NOT EXISTS capability_claims (key TEXT PRIMARY KEY, owner TEXT NOT NULL)');
      ctx.storage.sql.exec('CREATE TABLE IF NOT EXISTS migration_state (version INTEGER PRIMARY KEY)');
    });
  }

  async initialize() {
    if (this.ctx.storage.sql.exec('SELECT version FROM migration_state WHERE version = 1').toArray().length) return;
    const legacy = this.env.LEGACY_AUTH;
    if (typeof legacy?.get !== 'function' || typeof legacy.list !== 'function') throw new Error('Migration storage unavailable.');
    const names = new Set(), cursors = new Set();
    let cursor;
    do {
      const page = await legacy.list({ prefix: 'user:', limit: 1000, ...(cursor ? { cursor } : {}) });
      if (!Array.isArray(page.keys) || typeof page.list_complete !== 'boolean') throw new Error('Invalid migration page.');
      for (const entry of page.keys) {
        if (typeof entry.name !== 'string' || !entry.name.startsWith('user:') || encoder.encode(entry.name).byteLength > 512) throw new Error('Invalid account key.');
        names.add(entry.name);
        if (names.size > 1000) throw new Error('Migration exceeds account limit.');
      }
      if (page.list_complete) break;
      if (typeof page.cursor !== 'string' || !page.cursor || cursors.has(page.cursor) || cursors.size >= 32) throw new Error('Invalid migration cursor.');
      cursor = page.cursor;
      cursors.add(cursor);
    } while (true);
    const claims = new Map(), keys = [...names];
    for (let offset = 0; offset < keys.length; offset += 10) {
      const batch = keys.slice(offset, offset + 10);
      const values = await Promise.all(batch.map(key => legacy.get(key)));
      for (let i = 0; i < batch.length; i++) {
        if (typeof values[i] !== 'string' || encoder.encode(values[i]).byteLength > MAX_VALUE) throw new Error('Invalid account record.');
        const user = JSON.parse(values[i]);
        if (!user || typeof user.email !== 'string' || user.email !== batch[i].slice(5) || user.email !== user.email.trim().toLowerCase() ||
            !user.email.includes('@') || !Array.isArray(user.devices) || user.devices.length > 25) throw new Error('Invalid account profile.');
        for (const device of user.devices) {
          if (!device || typeof device !== 'object' || Array.isArray(device)) throw new Error('Invalid device record.');
          const path = capabilityPath(device.mcp_url);
          if (!path) continue;
          const key = await digest(path);
          if (claims.has(key) && claims.get(key) !== user.email) throw new Error('Legacy capability ownership conflict.');
          claims.set(key, user.email);
        }
      }
    }
    this.ctx.storage.transactionSync(() => {
      for (const [key, owner] of claims) {
        const existing = this.ctx.storage.sql.exec('SELECT owner FROM capability_claims WHERE key = ?', key).toArray()[0];
        if (existing && existing.owner !== owner) throw new Error('Legacy capability ownership conflict.');
        if (!existing) this.ctx.storage.sql.exec('INSERT INTO capability_claims (key, owner) VALUES (?, ?)', key, owner);
      }
      this.ctx.storage.sql.exec('INSERT INTO migration_state (version) VALUES (1)');
    });
  }

  async migrateRecords() {
    if (this.ctx.storage.sql.exec('SELECT version FROM migration_state WHERE version = 2').toArray().length) return;
    if (typeof this.env.LEGACY_AUTH?.get !== 'function' || typeof this.env.LEGACY_AUTH.list !== 'function') throw new Error('Account migration storage unavailable.');
    if (String(this.env.OAUTH_MIGRATION_REQUIRED) === 'true' && (typeof this.env.LEGACY_OAUTH?.get !== 'function' || typeof this.env.LEGACY_OAUTH.list !== 'function')) throw new Error('OAuth migration storage unavailable.');
    const records = new Map();
    for (const [legacy, prefix] of [[this.env.LEGACY_AUTH, ''], [this.env.LEGACY_OAUTH, 'oauth:']]) {
      if (!legacy) continue;
      let cursor;
      const seen = new Set();
      do {
        const page = await legacy.list({ limit: 1000, ...(cursor ? { cursor } : {}) });
        if (!Array.isArray(page.keys) || typeof page.list_complete !== 'boolean') throw new Error('Invalid migration page.');
        for (const item of page.keys) {
          const key = prefix + item.name;
          if (typeof item.name !== 'string' || !item.name || encoder.encode(key).byteLength > 512 || records.size >= 20000) throw new Error('Invalid migration record.');
          // Existing committed values and revocation tombstones win over legacy KV.
          if (await this.ctx.storage.get(key) !== undefined) continue;
          const value = await legacy.get(item.name);
          if (value === null) continue;
          if (typeof value !== 'string' || encoder.encode(value).byteLength > MAX_VALUE) throw new Error('Invalid migration value.');
          const expiresAt = Number.isFinite(item.expiration) ? item.expiration * 1000 : importedExpiry(item.name, value);
          if (expiresAt === null || expiresAt > Date.now()) records.set(key, { value, expiresAt });
        }
        if (page.list_complete) break;
        if (!page.cursor || seen.has(page.cursor) || seen.size >= 64) throw new Error('Invalid migration cursor.');
        cursor = page.cursor; seen.add(cursor);
      } while (true);
    }
    await this.ctx.storage.transaction(async txn => {
      for (const [key, record] of records) await txn.put(key, record);
    });
    // Close fallback durably before pruning tombstones, including across a restart.
    this.ctx.storage.sql.exec('INSERT INTO migration_state (version) VALUES (2)');
    for (const [key, record] of await this.ctx.storage.list()) if (record.value === null || record.expiresAt !== null && record.expiresAt <= Date.now()) await this.ctx.storage.delete(key);
    await this.ctx.storage.setAlarm(Date.now() + 3600000);
  }

  async alarm() {
    await this.ctx.blockConcurrencyWhile(async () => {
      let startAfter;
      do {
        const batch = await this.ctx.storage.list({ limit: 1000, ...(startAfter ? { startAfter } : {}) });
        for (const [key, stored] of batch) if (stored.value === null || stored.expiresAt !== null && stored.expiresAt <= Date.now()) await this.ctx.storage.delete(key);
        if (batch.size < 1000) break;
        startAfter = [...batch.keys()].at(-1);
      } while (true);
      await this.ctx.storage.setAlarm(Date.now() + 3600000);
    });
  }

  async fetch(request) {
    const path = route(request);
    if (!path) return reply(404, { error: 'Not found.' });
    const body = await parse(request, path);
    if (body instanceof Response) return body;
    try {
      // ponytail: one serialized store; partition only when measured throughput requires it.
      return await this.ctx.blockConcurrencyWhile(async () => {
        if (path === '/initialize') {
          await this.initialize();
          await this.migrateRecords();
          return reply(200, { ok: true });
        }
        if (!this.ctx.storage.sql.exec('SELECT version FROM migration_state WHERE version = 2').toArray().length) return reply(503, { error: 'Account migration is required.' });
        if (path === '/transact') {
          const committed = await this.ctx.storage.transaction(async txn => {
            const now = Date.now();
            for (const check of body.checks) {
              const stored = await txn.get(check.key);
              const value = stored && (stored.expiresAt === null || stored.expiresAt > now) ? stored.value : null;
              if (value !== check.value) return false;
            }
            for (const put of body.puts) await txn.put(put.key, { value: put.value, expiresAt: put.expirationTtl === undefined ? null : now + put.expirationTtl * 1000 });
            for (const key of body.deletes) await txn.delete(key);
            return true;
          });
          return reply(200, { committed });
        }
        if (path === '/list') {
          const records = await this.ctx.storage.list({ prefix: body.prefix, limit: body.limit || 1000, ...(body.cursor ? { startAfter: body.cursor } : {}) });
          const keys = [];
          for (const [name, record] of records) {
            if (record.value === null || record.expiresAt !== null && record.expiresAt <= Date.now()) { await this.ctx.storage.delete(name); continue; }
            keys.push({ name, ...(record.expiresAt === null ? {} : { expiration: Math.floor(record.expiresAt / 1000) }) });
          }
          const complete = records.size < (body.limit || 1000);
          return reply(200, { keys, list_complete: complete, ...(complete ? {} : { cursor: [...records.keys()].at(-1) }) });
        }
        if (path === '/claim') {
          const existing = this.ctx.storage.sql.exec('SELECT owner FROM capability_claims WHERE key = ?', body.key).toArray()[0];
          if (existing) return reply(200, { allowed: existing.owner === body.owner });
          this.ctx.storage.sql.exec('INSERT INTO capability_claims (key, owner) VALUES (?, ?)', body.key, body.owner);
          return reply(200, { allowed: true });
        }
        if (path === '/put') {
          await this.ctx.storage.put(body.key, {
            value: body.value,
            expiresAt: body.expirationTtl === undefined ? null : Date.now() + body.expirationTtl * 1000,
          });
          return reply(200, { ok: true });
        }
        if (path === '/delete') {
          await this.ctx.storage.delete(body.key);
          return reply(200, { ok: true });
        }
        let stored = await this.ctx.storage.get(body.key);
        if (stored === undefined) return reply(200, { value: null });
        if (stored.expiresAt !== null && stored.expiresAt <= Date.now()) {
          await this.ctx.storage.delete(body.key);
          return reply(200, { value: null });
        }
        return reply(200, { value: stored.value });
      });
    } catch { return reply(503, { error: 'Account storage unavailable.' }); }
  }
}

export default {
  async fetch(request, env) {
    if (!route(request)) return reply(404, { error: 'Not found.' });
    try {
      const id = env.STORE.idFromName('account-authority');
      return await env.STORE.get(id).fetch(request);
    } catch { return reply(503, { error: 'Account storage unavailable.' }); }
  },
};
