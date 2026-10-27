import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runInNewContext } from 'node:vm';
import { signInPage } from '../src/sign-in-page.js';

const html = await signInPage().text();
const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert.ok(script);

function browser(fetchResult) {
  let submitHandler, timeoutCallback, cleared = false;
  const requests = [], destinations = [];
  const result = { textContent: '' }, submit = { disabled: false };
  const form = { addEventListener(event, callback) { assert.equal(event, 'submit'); submitHandler = callback; } };
  runInNewContext(script, {
    document: { getElementById(id) { return { signin: form, result, submit }[id]; } },
    FormData: class { constructor(value) { assert.equal(value, form); } get(key) { return { email: 'approved@example.test', password: 'synthetic-long-password' }[key]; } },
    AbortController,
    setTimeout(callback, delay) { assert.equal(delay, 15000); timeoutCallback = callback; return 1; },
    clearTimeout(id) { assert.equal(id, 1); cleared = true; },
    location: { assign(path) { destinations.push(path); } },
    fetch: async (path, options) => { requests.push({ path, options }); return fetchResult({ expire: () => timeoutCallback(), options }); }
  });
  return { requests, destinations, result, submit, run: () => submitHandler({ preventDefault() {} }), get cleared() { return cleared; } };
}

test('successful sign-in uses same-origin JSON and the protected dashboard without reading or storing account secrets', async () => {
  const b = browser(() => ({ ok: true, status: 200, json() { throw new Error('Account response must not be read.'); } }));
  await b.run();
  assert.deepEqual(b.destinations, ['/dashboard']);
  assert.equal(b.requests.length, 1);
  const { path, options } = b.requests[0];
  assert.equal(path, '/api/auth/login'); assert.equal(options.method, 'POST');
  assert.equal(options.credentials, 'same-origin'); assert.equal(options.redirect, 'error'); assert.equal(options.cache, 'no-store');
  assert.deepEqual(JSON.parse(options.body), { email: 'approved@example.test', password: 'synthetic-long-password' });
  assert.equal(b.submit.disabled, false); assert.equal(b.cleared, true);
});

test('credential rejection, throttling and service failure have distinct safe messages without displaying server payloads', async () => {
  const cases = [[401, /approved Stealth Commander account email and Stealth Commander password/], [403, /approved Stealth Commander account email and Stealth Commander password/], [429, /Wait one minute/], [503, /temporarily unavailable/], [502, /temporarily unavailable/], [400, /could not be completed/]];
  for (const [status, message] of cases) {
    const b = browser(() => ({ ok: false, status, json() { throw new Error('Do not display a raw response.'); }, text() { throw new Error('Do not display a raw response.'); } }));
    await b.run();
    assert.match(b.result.textContent, message);
    assert.deepEqual(b.destinations, []);
    assert.equal(b.submit.disabled, false); assert.equal(b.cleared, true);
  }
});

test('network failures and bounded timeouts release the submit button and allow retry', async () => {
  for (const timeout of [false, true]) {
    const b = browser(({ expire }) => { if (timeout) expire(); throw new Error('synthetic network detail must not be exposed'); });
    await b.run();
    assert.match(b.result.textContent, timeout ? /timed out/ : /Could not reach sign-in/);
    assert.ok(!b.result.textContent.includes('synthetic'));
    assert.equal(b.submit.disabled, false); assert.equal(b.cleared, true);
    assert.deepEqual(b.destinations, []);
  }
});

test('an in-flight sign-in does not send duplicate credential submissions', async () => {
  let complete;
  const b = browser(() => new Promise(resolve => { complete = resolve; }));
  const pending = b.run();
  assert.equal(b.submit.disabled, true);
  await b.run();
  assert.equal(b.requests.length, 1);
  complete({ ok: false, status: 429 });
  await pending;
  assert.equal(b.submit.disabled, false);
});
