import { test } from 'node:test';
import assert from 'node:assert/strict';
import legacy, { hashPassword } from '../legacy-worker.js';

const origin = 'https://stealthcommander.pages.dev';
const approvedEmail = 'approved-owner@example.test';
const password = 'synthetic-account-password';

function fixture(privateMode = 'true') {
  const records = new Map();
  const store = {
    async get(key) { return records.get(key) ?? null; },
    async put(key, value) { records.set(key, value); },
    async delete(key) { records.delete(key); },
    async transact(checks, puts, deletes = []) {
      if (checks.some(check => (records.get(check.key) ?? null) !== check.value)) return false;
      for (const entry of puts) records.set(entry.key, entry.value);
      for (const key of deletes) records.delete(key);
      return true;
    }
  };
  const env = { STEALTH_AUTH: store, PRIVATE_MODE: privateMode, PRIVATE_ACCOUNT_ALLOWLIST: JSON.stringify([approvedEmail]) };
  const call = (path, body, token) => legacy.fetch(new Request(origin + path, {
    method: body ? 'POST' : 'GET',
    headers: { Origin: origin, 'Content-Type': 'application/json', ...(token ? { Authorization: 'Bearer ' + token } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {})
  }), env);
  return { records, call };
}

test('private registration cannot claim an unregistered allowlisted identity or issue credentials', async () => {
  const f = fixture();
  const response = await f.call('/api/auth/register', { email: approvedEmail, password });
  assert.equal(response.status, 403);
  assert.equal(response.headers.get('Set-Cookie'), null);
  const body = await response.json();
  assert.equal(body.ok, false);
  assert.equal(body.token, undefined);
  assert.equal(body.user, undefined);
  assert.equal(f.records.size, 0);
  assert.equal((await f.call('/api/auth/login', { email: approvedEmail, password })).status, 401);
});

test('private registration does not disclose account existence or change an approved account', async () => {
  const f = fixture();
  const salt = 'a'.repeat(32);
  const user = { email: approvedEmail, api_key: 'sc_live_' + 'a'.repeat(40), salt, hash: await hashPassword(password, salt), devices: [] };
  const saved = JSON.stringify(user);
  f.records.set('user:' + approvedEmail, saved);
  f.records.set('apikey:' + user.api_key, approvedEmail);
  const responses = [];
  for (const email of [approvedEmail, 'unapproved@example.test']) {
    const response = await f.call('/api/auth/register', { email, password: 'different-synthetic-password' });
    assert.equal(response.status, 403);
    assert.equal(response.headers.get('Set-Cookie'), null);
    responses.push(await response.json());
  }
  assert.deepEqual(responses[0], responses[1]);
  assert.equal(f.records.get('user:' + approvedEmail), saved);
  assert.equal(f.records.size, 2);
  const login = await f.call('/api/auth/login', { email: approvedEmail, password });
  assert.equal(login.status, 200);
  const session = (await login.json()).token;
  const me = await f.call('/api/auth/me', undefined, session);
  assert.equal(me.status, 200);
  assert.equal((await me.json()).user.email, approvedEmail);
});

test('public registration retains its existing password and authenticated-session behavior', async () => {
  const f = fixture('false');
  assert.equal((await f.call('/api/auth/register', { email: approvedEmail, password: 'short' })).status, 400);
  const registered = await f.call('/api/auth/register', { email: approvedEmail, password });
  assert.equal(registered.status, 200);
  const session = (await registered.json()).token;
  assert.equal((await f.call('/api/auth/me', undefined, session)).status, 200);
  assert.equal((await f.call('/api/auth/login', { email: approvedEmail, password })).status, 200);
});
