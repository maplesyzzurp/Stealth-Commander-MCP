import { readFileSync } from 'node:fs';
import { authStore } from '../src/auth-store.js';
import { createServer } from 'node:http';
import { randomBytes, pbkdf2Sync } from 'node:crypto';
import { Miniflare, Response } from 'miniflare';

const origin = 'https://stealthcommander.pages.dev';
const mf = new Miniflare({ workers: [{
  name: 'gateway', modules: true, scriptPath: 'build/worker.js', compatibilityDate: '2026-07-30', compatibilityFlags: ['global_fetch_strictly_public'],
  bindings: { GATEWAY_ORIGIN: origin }, kvNamespaces: ['STEALTH_AUTH', 'OAUTH_KV'],
  ratelimits: { AUTH_RATE_LIMITER: { namespace_id: '1', simple: { limit: 10, period: 60 } }, REGISTRATION_RATE_LIMITER: { namespace_id: '2', simple: { limit: 10, period: 60 } } },
  serviceBindings: { ASSETS: () => new Response('synthetic contract website'), AUTH_STORE: 'auth-store' },
  outboundService: request => {
    if (!/^https:\/\/[a-z0-9-]+\.trycloudflare\.com\/[A-Za-z0-9_-]{32,128}\/mcp$/.test(request.url)) throw new Error('External network is disabled.');
    return Response.json({ jsonrpc: '2.0', id: 1, result: { synthetic: true } });
  }
}, { name: 'auth-store', modules: true, script: readFileSync(new URL('../../strong-auth-service/worker.js', import.meta.url), 'utf8'), compatibilityDate: '2026-07-30', durableObjects: { STORE: { className: 'AuthStore', useSQLite: true } }, kvNamespaces: ['LEGACY_AUTH'] }] });
const store = authStore(await mf.getWorker('auth-store'));
await store.initialize();
for (const name of ['alice', 'bob']) {
  const email = `${name}@example.test`;
  const salt = randomBytes(16).toString('hex');
  const user = { email, name, salt, hash: pbkdf2Sync('synthetic-contract-password', salt, 100000, 32, 'sha256').toString('hex'), api_key: 'sc_live_' + randomBytes(20).toString('hex'), devices: [] };
  await store.put('user:' + email, JSON.stringify(user));
  await store.put('apikey:' + user.api_key, email);
}
const server = createServer(async (incoming, outgoing) => {
  try {
    const chunks = []; let size = 0;
    for await (const chunk of incoming) { size += chunk.length; if (size > 2 * 1024 * 1024) throw new Error('Local request limit exceeded.'); chunks.push(chunk); }
    const headers = new Headers();
    for (const [name, value] of Object.entries(incoming.headers)) if (!['host', 'content-length', 'cf-connecting-ip'].includes(name) && value) headers.set(name, Array.isArray(value) ? value.join(', ') : value);
    headers.set('CF-Connecting-IP', '192.0.2.10');
    const response = await mf.dispatchFetch(origin + incoming.url, { method: incoming.method, headers, ...(!['GET', 'HEAD'].includes(incoming.method) ? { body: Buffer.concat(chunks) } : {}), redirect: 'manual' });
    outgoing.statusCode = response.status;
    for (const [name, value] of response.headers) if (name !== 'set-cookie') outgoing.setHeader(name, value);
    if (response.headers.getSetCookie().length) outgoing.setHeader('Set-Cookie', response.headers.getSetCookie());
    outgoing.end(Buffer.from(await response.arrayBuffer()));
  } catch {
    outgoing.writeHead(503, { 'Content-Type': 'application/json' }); outgoing.end('{"error":"Synthetic contract adapter failed."}');
  }
});
server.listen(Number(process.env.CONTRACT_PORT || 0), '127.0.0.1', () => process.stdout.write(JSON.stringify({ base_url: `http://127.0.0.1:${server.address().port}`, synthetic: true }) + '\n'));
let stopping = false;
async function stop() { if (stopping) return; stopping = true; server.close(); await mf.dispose(); process.exit(0); }
process.on('SIGTERM', stop); process.on('SIGINT', stop);
if (process.env.CONTRACT_STOP_ON_STDIN_END === '1') { process.stdin.resume(); process.stdin.once('end', stop); }
