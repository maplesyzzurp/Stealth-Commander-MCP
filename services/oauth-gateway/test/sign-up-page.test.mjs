import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runInNewContext } from 'node:vm';
import { signUpPage } from '../src/sign-up-page.js';

const html = await signUpPage().text();
const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert.ok(script);

function browser(fetchResult, values = {}) {
  let submitHandler, timeoutCallback, cleared = false;
  const requests = [], destinations = [];
  const result = { textContent: '' }, submit = { disabled: false }, signInLink = { hidden: true };
  const form = { addEventListener(event, callback) { assert.equal(event, 'submit'); submitHandler = callback; } };
  runInNewContext(script, {
    document: { getElementById(id) { return { signup: form, result, submit, 'sign-in-link': signInLink }[id]; } },
    FormData: class { constructor(value) { assert.equal(value, form); } get(key) { return {
      email: 'new@example.test', password: 'synthetic-long-password', confirm: 'synthetic-long-password', ...values
    }[key]; } },
    AbortController,
    setTimeout(callback, delay) { assert.equal(delay, 15000); timeoutCallback = callback; return 1; },
    clearTimeout(id) { assert.equal(id, 1); cleared = true; },
    location: { assign(path) { destinations.push(path); } },
    fetch: async (path, options) => { requests.push({ path, options }); return fetchResult({ expire: () => timeoutCallback(), options }); }
  });
  return { requests, destinations, result, submit, signInLink,
    run: () => submitHandler({ preventDefault() {} }), get cleared() { return cleared; } };
}

test('successful registration posts same-origin credentials and opens the cookie-authenticated dashboard without reading secrets', async () => {
  const b = browser(() => ({ ok: true, status: 201,
    json() { throw new Error('Account response must not be read.'); },
    text() { throw new Error('Account response must not be read.'); } }), { email: ' new@example.test ' });
  await b.run();
  assert.deepEqual(b.destinations, ['/dashboard']);
  assert.equal(b.requests.length, 1);
  const { path, options } = b.requests[0];
  assert.equal(path, '/api/auth/register'); assert.equal(options.method, 'POST');
  assert.equal(options.credentials, 'same-origin'); assert.equal(options.redirect, 'error'); assert.equal(options.cache, 'no-store');
  assert.equal(options.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(options.body), { email: 'new@example.test', password: 'synthetic-long-password' });
  assert.equal(b.submit.disabled, false); assert.equal(b.cleared, true);
});

test('password mismatch and invalid bounded inputs never send a registration request', async () => {
  const cases = [
    [{ confirm: 'a-different-password' }, /Passwords do not match/],
    [{ password: 'too-short', confirm: 'too-short' }, /12 to 1,024/],
    [{ password: 'p'.repeat(1025), confirm: 'p'.repeat(1025) }, /12 to 1,024/],
    [{ email: 'a'.repeat(242) + '@example.test' }, /valid email/],
    [{ email: 'missing-domain@' }, /valid email/],
    [{ email: 'two words@example.test' }, /valid email/]
  ];
  for (const [values, message] of cases) {
    const b = browser(() => { throw new Error('Invalid input must not be sent.'); }, values);
    await b.run();
    assert.equal(b.requests.length, 0); assert.deepEqual(b.destinations, []);
    assert.match(b.result.textContent, message); assert.equal(b.submit.disabled, false);
  }
});

test('duplicate accounts, rejected input, throttling and service failures show safe distinct messages', async () => {
  const cases = [[409, /already exists/], [400, /Check your email/], [429, /Too many account creation attempts/],
    [503, /temporarily unavailable/], [502, /temporarily unavailable/]];
  for (const [status, message] of cases) {
    const b = browser(() => ({ ok: false, status,
      json() { throw new Error('Never read raw server payloads.'); },
      text() { throw new Error('Never read raw server payloads.'); } }));
    await b.run();
    assert.match(b.result.textContent, message); assert.deepEqual(b.destinations, []);
    assert.equal(b.signInLink.hidden, status !== 409);
    assert.equal(b.submit.disabled, false); assert.equal(b.cleared, true);
  }
});

test('network errors and bounded timeouts release the form without exposing internal errors', async () => {
  for (const timeout of [false, true]) {
    const b = browser(({ expire }) => { if (timeout) expire(); throw new Error('synthetic private network detail'); });
    await b.run();
    assert.match(b.result.textContent, timeout ? /timed out/ : /Could not reach account creation/);
    assert.doesNotMatch(b.result.textContent, /synthetic|private/);
    assert.equal(b.submit.disabled, false); assert.equal(b.cleared, true);
  }
});

test('an in-flight registration cannot send a duplicate request and retry clears the duplicate-account link', async () => {
  let complete;
  const b = browser(() => new Promise(resolve => { complete = resolve; }));
  const pending = b.run();
  assert.equal(b.submit.disabled, true);
  await b.run();
  assert.equal(b.requests.length, 1);
  complete({ ok: false, status: 409 });
  await pending;
  assert.equal(b.signInLink.hidden, false);
  const retry = b.run();
  assert.equal(b.signInLink.hidden, true);
  complete({ ok: false, status: 503 });
  await retry;
  assert.equal(b.requests.length, 2); assert.equal(b.submit.disabled, false);
});

test('account creation has matching bounds, sign-in navigation, UTF-8 and restrictive no-store headers', async () => {
  assert.match(html, /id="email"[^>]*maxlength="254"/);
  assert.match(html, /id="password"[^>]*minlength="12"[^>]*maxlength="1024"/);
  assert.match(html, /id="confirm"[^>]*minlength="12"[^>]*maxlength="1024"/);
  assert.match(html, /href="\/sign-in">Sign in/);
  assert.doesNotMatch(html, /approved|approval|nexusmap\.ai|password reset|verify your email|localStorage|sessionStorage|document\.cookie|\ufffd/i);
  const head = signUpPage('HEAD');
  assert.equal(await head.text(), '');
  assert.equal(head.headers.get('Content-Type'), 'text/html; charset=utf-8');
  assert.equal(head.headers.get('Cache-Control'), 'no-store');
  assert.match(head.headers.get('Content-Security-Policy'), /connect-src 'self'/);
  assert.match(head.headers.get('Content-Security-Policy'), /form-action 'self'/);
});
