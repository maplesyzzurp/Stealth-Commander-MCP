import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

const html = await readFile(new URL('../assets/index.html', import.meta.url), 'utf8');
const script = html.match(/<script id="site-connection">([\s\S]*?)<\/script>/)?.[1];

const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
function browser(reply, { online = true, visible = true } = {}) {
  assert.ok(script, 'Dashboard must check authenticated site connectivity independently of PC polling.');
  const attributes = new Map(), requests = [], timers = new Map(), intervals = [];
  const listeners = new Map();
  const dot = { className: 'dot', setAttribute(name, value) { attributes.set(name, value); }, getAttribute(name) { return attributes.get(name); } };
  const document = { visibilityState: visible ? 'visible' : 'hidden', getElementById(id) { assert.equal(id, 'hdot'); return dot; },
    addEventListener(type, callback) { listeners.set(type, callback); } };
  const navigator = { onLine: online };
  let timerId = 0, active = 0, maxActive = 0;
  runInNewContext(script, {
    document, navigator, AbortController,
    window: { addEventListener(type, callback) { listeners.set(type, callback); } },
    setTimeout(callback, delay) { timers.set(++timerId, { callback, delay }); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(callback, delay) { intervals.push({ callback, delay }); return intervals.length; },
    fetch: async (path, options) => {
      requests.push({ path, options }); active++; maxActive = Math.max(maxActive, active);
      try { return await reply(requests.length, options); } finally { active--; }
    }
  });
  return { dot, requests, navigator, document, timers, intervals,
    get maxActive() { return maxActive; },
    async event(type) { listeners.get(type)?.(); await flush(); },
    async interval() { assert.equal(intervals.length, 1); assert.equal(intervals[0].delay, 30000); intervals[0].callback(); await flush(); },
    async timeout() { const timer = [...timers.values()][0]; assert.ok(timer.delay > 0 && timer.delay <= 15000); timer.callback(); await flush(); } };
}
const signedIn = b => assert.equal(b.dot.getAttribute('data-state'), 'connected');
const signedOut = b => assert.equal(b.dot.getAttribute('data-state'), 'disconnected');
const statusOnly = status => ({ status, json() { throw new Error('Do not read the session or CSRF body.'); }, text() { throw new Error('Do not read the session body.'); } });

test('site status stays red until authenticated and checks only same-origin session response status', async () => {
  let complete;
  const b = browser(() => new Promise(resolve => { complete = resolve; }));
  signedOut(b);
  assert.equal(b.requests.length, 1);
  const { path, options } = b.requests[0];
  assert.equal(path, '/api/account/session'); assert.equal(options.method, 'GET');
  assert.equal(options.credentials, 'same-origin'); assert.equal(options.cache, 'no-store'); assert.equal(options.redirect, 'error');
  assert.equal(options.body, undefined);
  complete(statusOnly(200)); await flush(); signedIn(b);
  assert.match(b.dot.getAttribute('aria-label'), /Site connected.*signed in/);
});

test('session expiry, service failures and network failures turn an authenticated site red', async () => {
  for (const response of [401, 403, 204, 503, 'network']) {
    const b = browser(call => {
      if (call === 1) return statusOnly(200);
      if (response === 'network') throw new Error('private connection details');
      return statusOnly(response);
    });
    await flush(); signedIn(b);
    await b.interval(); signedOut(b);
    assert.doesNotMatch(b.dot.getAttribute('aria-label'), /private/);
  }
});

test('offline and ended-session events cannot be undone by stale successful responses', async () => {
  for (const event of ['offline', 'stealth-site-session-ended']) {
    let complete;
    const b = browser(() => new Promise(resolve => { complete = resolve; }));
    if (event === 'offline') b.navigator.onLine = false;
    await b.event(event); signedOut(b);
    assert.equal(b.requests[0].options.signal.aborted, true);
    complete(statusOnly(200)); await flush(); signedOut(b);
  }
});

test('focus, online and visible refreshes recover without overlapping status checks', async () => {
  const completions = [];
  const b = browser(() => new Promise(resolve => completions.push(resolve)));
  await b.event('focus'); await b.event('online'); await b.event('visibilitychange'); await b.interval();
  assert.equal(b.requests.length, 1);
  completions.shift()(statusOnly(200)); await flush();
  while (completions.length) { completions.shift()(statusOnly(200)); await flush(); }
  signedIn(b); assert.equal(b.maxActive, 1);
  b.navigator.onLine = false; await b.event('offline'); signedOut(b);
  b.navigator.onLine = true; await b.event('online');
  completions.shift()(statusOnly(200)); await flush(); signedIn(b);
  b.document.visibilityState = 'hidden'; const count = b.requests.length;
  await b.interval(); await b.event('focus'); assert.equal(b.requests.length, count);
  b.document.visibilityState = 'visible'; await b.event('visibilitychange');
  completions.shift()(statusOnly(401)); await flush(); signedOut(b);
});

test('a timed-out status request stays red even if a late response reports success', async () => {
  let complete;
  const b = browser(() => new Promise(resolve => { complete = resolve; }));
  await b.timeout(); signedOut(b);
  assert.equal(b.requests[0].options.signal.aborted, true);
  complete(statusOnly(200)); await flush(); signedOut(b);
});

test('anonymous offline startup stays red without a request and the ninja is an accessible home link', async () => {
  const b = browser(() => { throw new Error('Offline startup must not fetch.'); }, { online: false });
  await flush(); signedOut(b); assert.equal(b.requests.length, 0);
  assert.match(html, /<a[^>]*id="brandHome"[^>]*href="\/"[^>]*aria-label="Stealth Commander home"[^>]*><img[^>]*src="\/icon\.png"/);
  assert.doesNotMatch(html, /getElementById\('hdot'\)\.className\s*=/);
});
