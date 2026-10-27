import { readLimitedText } from '../legacy-worker.js';

// Account authority lives in the private Durable Object service, which provides
// consistent reads after key rotation, device changes and session revocation.
export function authStore(service, prefix = '', transactional = false) {
  const reads = new Map(), writes = new Map();
  let closed = false;
  async function call(operation, body) {
    const response = await service.fetch('https://auth-store.internal/' + operation, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    });
    if (response.status !== 200) throw new Error('Account storage is unavailable.');
    const result = JSON.parse(await readLimitedText(response, 2 * 1024 * 1024));
    if (!result || typeof result !== 'object') throw new Error('Invalid account storage response.');
    return result;
  }
  return {
    async initialize() {
      const result = await call('initialize', {});
      if (result.ok !== true) throw new Error('Account storage migration is incomplete.');
    },
    async get(key, options) {
      key = prefix + key;
      const pending = writes.get(key);
      const result = pending ? { value: pending.value } : transactional && reads.has(key) ? { value: reads.get(key) } : await call('get', { key });
      if (result.value !== null && typeof result.value !== 'string') throw new Error('Invalid account storage value.');
      if (transactional && !reads.has(key)) reads.set(key, result.value);
      return result.value === null || options?.type !== 'json' ? result.value : JSON.parse(result.value);
    },
    async put(key, value, options) {
      key = prefix + key;
      const entry = { key, value, ...(options?.expirationTtl === undefined ? {} : { expirationTtl: options.expirationTtl }) };
      if (transactional) {
        if (closed) throw new Error('Storage transaction ended.');
        if (!reads.has(key)) reads.set(key, (await call('get', { key })).value);
        writes.set(key, entry); return;
      }
      const result = await call('put', entry);
      if (result.ok !== true) throw new Error('Account storage write failed.');
    },
    async delete(key) {
      key = prefix + key;
      if (transactional) {
        if (closed) throw new Error('Storage transaction ended.');
        if (!reads.has(key)) reads.set(key, (await call('get', { key })).value);
        writes.set(key, { key, value: null }); return;
      }
      const result = await call('delete', { key });
      if (result.ok !== true) throw new Error('Account storage deletion failed.');
    },
    async claim(key, owner) {
      const result = await call('claim', { key, owner });
      if (typeof result.allowed !== 'boolean') throw new Error('Account ownership check failed.');
      return result.allowed;
    },
    async transact(checks, puts, deletes = []) {
      const result = await call('transact', { checks, puts, deletes });
      if (typeof result.committed !== 'boolean') throw new Error('Invalid transaction result.');
      return result.committed;
    },
    async list(options = {}) {
      const result = await call('list', { prefix: prefix + (options.prefix || ''), ...(options.cursor ? { cursor: prefix + options.cursor } : {}), ...(options.limit ? { limit: options.limit } : {}) });
      if (!Array.isArray(result.keys) || typeof result.list_complete !== 'boolean') throw new Error('Invalid storage list.');
      return { ...result, keys: result.keys.map(k => ({ ...k, name: k.name.slice(prefix.length) })), ...(result.cursor ? { cursor: result.cursor.slice(prefix.length) } : {}) };
    },
    async commit() {
      if (!transactional || !writes.size) return true;
      closed = true;
      const result = await call('transact', { checks: [...reads].map(([key, value]) => ({ key, value })), puts: [...writes.values()].filter(v => v.value !== null), deletes: [...writes.values()].filter(v => v.value === null).map(v => v.key) });
      if (typeof result.committed !== 'boolean') throw new Error('Invalid transaction result.');
      return result.committed;
    }
  };
}
