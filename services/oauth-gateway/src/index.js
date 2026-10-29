import { OAuthProvider, OAuthError, AuthorizationError, CimdFetchError, insufficientScope } from '@cloudflare/workers-oauth-provider';
import legacy, { validMcpUrl, readLimitedText, hashPassword, safeEqual, createSession, resolveSession, readCurrentUser, claimEndpoint, privateAccountAllowed, liveDevice, issuePcTicket } from '../legacy-worker.js';
import { UPSTREAM_TIMEOUT_MS } from './limits.js';
import { blockedToolResponse, publicMcpResponse } from './public-mcp.js';
import { authStore } from './auth-store.js';
import { handleBilling } from './billing.js';
import { signInPage } from './sign-in-page.js';
import { signUpPage } from './sign-up-page.js';

const CONTROL = 'pc:control';
const SESSION_SECONDS = 43200;
const PUBLIC_INFORMATION_PATHS = new Map([['/', '/landing'], ['/about', '/about'], ['/support', '/support'], ['/icon.png', '/icon.png'], ['/app.js', '/app.js'], ['/app.css', '/app.css'], ['/robots.txt', '/robots.txt'], ['/sitemap.xml', '/sitemap.xml'], ['/social-card.png', '/social-card.png'], ['/jdm-stickerwall.svg', '/jdm-stickerwall.svg']]);
const htmlEscape = value => String(value).replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
const digest = async value => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value))), b => b.toString(16).padStart(2, '0')).join('');
const json = (status, error, headers = {}) => Response.json({ error }, { status, headers: { 'Cache-Control': 'no-store', ...headers } });

async function checkLimit(binding, key) {
  if (typeof binding?.fetch === 'function') {
    const response = await binding.fetch('https://rate-limit.internal/check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }) });
    if (response.status !== 200) throw new Error('Rate-limit service failed.');
    const result = JSON.parse(await readLimitedText(response, 1024));
    if (typeof result?.success !== 'boolean') throw new Error('Invalid rate-limit response.');
    return result;
  }
  if (typeof binding?.limit === 'function') {
    const result = await binding.limit({ key });
    if (typeof result?.success !== 'boolean') throw new Error('Invalid rate-limit response.');
    return result;
  }
  throw new Error('Rate limiting is unavailable.');
}

async function authRateLimit(request, env, path, body) {
  let binding, kind, email;
  if (path === '/oauth/register') { binding = env.REGISTRATION_RATE_LIMITER; kind = 'dcr'; }
  else if (path === '/api/auth/login' || path === '/api/auth/register') {
    binding = env.AUTH_RATE_LIMITER; kind = 'signin';
    try {
      email = JSON.parse(body).email;
      if (email !== undefined && typeof email !== 'string') return json(400, 'Email must be a string.');
    } catch { /* The route validates its body. */ }
  } else if (path === '/authorize') {
    const form = new URLSearchParams(body);
    if (form.get('stage') !== 'signin') return null;
    binding = env.AUTH_RATE_LIMITER; kind = 'signin'; email = form.get('email');
  } else return null;
  if (!binding || typeof binding.limit !== 'function' && typeof binding.fetch !== 'function') return json(503, 'Authentication abuse protection is not configured.');
  const source = request.headers.get('CF-Connecting-IP');
  if (!source) return json(503, 'Client address is unavailable.');
  const sourceAllowed = await checkLimit(binding, `${kind}:source:${await digest(source)}`);
  if (!sourceAllowed.success) return json(429, 'Too many authentication requests. Try again later.', { 'Retry-After': '60' });
  if (typeof email === 'string' && email.trim()) {
    const accountAllowed = await checkLimit(binding, `${kind}:account:${await digest(email.trim().toLowerCase())}`);
    if (!accountAllowed.success) return json(429, 'Too many authentication requests. Try again later.', { 'Retry-After': '60' });
  }
  return null;
}

function cookie(request, name) {
  const values = (request.headers.get('Cookie') || '').split(';').map(v => v.trim()).filter(v => v.startsWith(name + '='));
  return values.length === 1 ? values[0].slice(name.length + 1) : '';
}

async function account(request, env) {
  const session = cookie(request, 'sc_session');
  const user = await resolveSession(session, env.STEALTH_AUTH);
  return user && privateAccountAllowed(env, user.email) ? { user, session } : null;
}

function device(user, id) {
  return Array.isArray(user.devices) && user.devices.find(d => d.device_id === id && validMcpUrl(d.mcp_url));
}

async function pairFingerprint(user, pc) {
  return digest(JSON.stringify([user.email, user.api_key, pc.device_id, pc.oauth_pair_id || 'legacy', new URL(pc.mcp_url).pathname]));
}

async function currentDevice(env, props) {
  if (!props || typeof props.email !== 'string' || typeof props.deviceId !== 'string' || typeof props.pairFingerprint !== 'string') return null;
  const user = await readCurrentUser(props.email, env.STEALTH_AUTH);
  if (!user || !privateAccountAllowed(env, user.email)) return null;
  const pc = device(user, props.deviceId);
  if (!pc?.device_credential_hash) return null;
  if (!await claimEndpoint(env.STEALTH_AUTH, user.email, pc.mcp_url)) return null;
  {
    const credential = await env.STEALTH_AUTH.get('deviceauth:' + pc.device_credential_hash, { type: 'json' });
    if (!credential || credential.email !== user.email || credential.deviceId !== pc.device_id ||
        !safeEqual(credential.pairId, pc.oauth_pair_id) || !Number.isFinite(credential.expiresAt) || credential.expiresAt <= Date.now() ||
        !safeEqual(credential.accountKeyHash, await digest(user.api_key))) return null;
  }
  return pc && safeEqual(await pairFingerprint(user, pc), props.pairFingerprint) ? pc : null;
}

async function grantStillActive(env, auth, props) {
  const token = await env.OAUTH_PROVIDER.unwrapToken(auth.token);
  if (!token || token.userId !== props.email) return false;
  const grant = await env.OAUTH_KV.get(`grant:${token.userId}:${token.grantId}`, { type: 'json' });
  return grant && grant.id === token.grantId && grant.userId === token.userId &&
    grant.clientId === auth.clientId && grant.scope.includes(CONTROL) &&
    grant.resource === auth.audience && Number.isFinite(grant.expiresAt) && grant.expiresAt > Date.now() / 1000;
}

function page(body, headers = new Headers(), status = 200) {
  headers.set('Content-Type', 'text/html; charset=utf-8');
  headers.set('Content-Security-Policy', "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'");
  headers.set('X-Frame-Options', 'DENY');
  headers.set('Cache-Control', 'no-store');
  headers.set('Referrer-Policy', 'no-referrer');
  headers.set('X-Content-Type-Options', 'nosniff');
  return new Response(`<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Stealth Commander access</title><main>${body}</main></html>`, { status, headers });
}

async function consentPage(request, env, oauthRequest, signedIn, extraHeaders) {
  const oauth = env.OAUTH_PROVIDER;
  const details = await oauth.describeConsent(oauthRequest);
  const consent = await oauth.beginConsent(oauthRequest);
  const headers = consent.headers;
  if (extraHeaders) for (const [k, v] of extraHeaders) headers.append(k, v);
  let fields;
  if (!signedIn) {
    fields = `<h1>Sign in to Stealth Commander</h1><p>Use your existing account to choose a paired PC.</p><input type="hidden" name="stage" value="signin"><p><label>Email <input name="email" type="email" autocomplete="username" required maxlength="254"></label></p><p><label>Password <input name="password" type="password" autocomplete="current-password" required maxlength="1024"></label></p><button name="decision" value="signin">Sign in</button>`;
  } else {
    await env.OAUTH_KV.put('sc:consent:' + await digest(consent.handle), JSON.stringify({ email: signedIn.user.email, sessionHash: await digest(signedIn.session), expires: Date.now() + 600000 }), { expirationTtl: 600 });
    const devices = (signedIn.user.devices || []).filter(pc => typeof pc.device_id === 'string' && pc.device_credential_hash && validMcpUrl(pc.mcp_url));
    const choices = devices.map(pc => `<option value="${htmlEscape(pc.device_id)}">${htmlEscape(pc.hostname || pc.device_id)}</option>`).join('');
    fields = `<h1>Allow access to one PC?</h1><p>Signed in as ${htmlEscape(signedIn.user.email)}.</p><p><strong>${htmlEscape(details.clientName)}</strong> requests access. ${details.clientDomain ? `Client domain: ${htmlEscape(details.clientDomain)}.` : 'This client registered itself; its name is not verified.'}</p><p>Access will be returned to <strong>${htmlEscape(details.redirectHost)}</strong>.</p>${details.redirectIsLoopback ? '<p>This sends access to an app on your computer. Continue only if you started this connection.</p>' : ''}<p>Scope <strong>pc:control</strong> gives full control of the selected PC through Stealth Commander: view its screen, send input, run commands, and read or change files. Direct requests through this connection go to the selected PC. Full PC control can also expose any credentials and data present on that PC, including browser sessions. There is no operating-system sandbox. The client can act while its access remains valid. Approve only a client you trust.</p><p>Access lasts up to 5 minutes per token and may renew for up to 12 hours. Unlinking the PC, pairing it again, or rotating your account key ends this grant once account changes reach this server.</p><input type="hidden" name="stage" value="consent">${choices ? `<p><label>Paired PC <select name="device_id" required><option value="">Choose a PC</option>${choices}</select></label></p><button name="decision" value="approve">Allow full control of this PC</button>` : '<p>No paired PC is available. Pair a PC in Stealth Commander, then start again.</p>'}`;
  }
  return page(`<form method="post" action="/authorize"><input type="hidden" name="handle" value="${htmlEscape(consent.handle)}">${fields}<button name="decision" value="deny">Cancel</button></form>`, headers);
}

async function authorize(request, env) {
  const oauth = env.OAUTH_PROVIDER;
  if (request.method === 'GET') {
    let parsed;
    try { parsed = await oauth.parseAuthRequest(request); }
    catch (error) {
      if (error instanceof AuthorizationError && error.redirectTo) return Response.redirect(error.redirectTo, 302);
      throw error;
    }
    if (parsed.scope.some(s => s !== CONTROL)) return page('<p>This server supports only pc:control.</p>', undefined, 400);
    return consentPage(request, env, parsed, await account(request, env));
  }
  if (request.method !== 'POST') return json(405, 'Method is not supported.', { Allow: 'GET, POST' });
  if (request.headers.get('Origin') !== env.GATEWAY_ORIGIN || request.headers.get('Sec-Fetch-Site') === 'cross-site') return json(403, 'Origin is not allowed.');
  if (!(request.headers.get('Content-Type') || '').startsWith('application/x-www-form-urlencoded')) return json(415, 'Form encoding required.');
  const form = new URLSearchParams(await readLimitedText(request, 16384));
  const handle = form.get('handle') || '';
  if (form.get('decision') === 'deny') {
    const denied = await oauth.denyConsent(request, handle);
    return new Response(null, { status: 302, headers: denied.headers });
  }
  if (form.get('stage') === 'signin' && form.get('decision') === 'signin') {
    const approved = await oauth.approveConsent(request, handle, { scope: [CONTROL] });
    const email = (form.get('email') || '').trim().toLowerCase();
    const password = form.get('password') || '';
    const user = await readCurrentUser(email, env.STEALTH_AUTH);
    const passwordHash = await hashPassword(password, user ? user.salt : '00000000000000000000000000000000');
    if (!user || !privateAccountAllowed(env, email) || user.email !== email || !safeEqual(passwordHash, user.hash)) return page('<p>Sign-in failed. Start the connection again.</p>', approved.headers, 401);
    const session = await createSession(user, env.STEALTH_AUTH);
    const headers = new Headers();
    headers.append('Set-Cookie', `sc_session=${session}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${SESSION_SECONDS}`);
    return consentPage(request, env, approved.request, { user, session }, headers);
  }
  if (form.get('stage') !== 'consent' || form.get('decision') !== 'approve') return json(400, 'Consent decision required.');
  const signedIn = await account(request, env);
  const markerKey = 'sc:consent:' + await digest(handle);
  const markerRaw = await env.OAUTH_KV.get(markerKey);
  const marker = markerRaw && JSON.parse(markerRaw);
  if (!signedIn || !marker || marker.expires < Date.now() || marker.email !== signedIn.user.email || !safeEqual(marker.sessionHash, await digest(signedIn.session))) return json(403, 'Sign-in changed or consent expired. Start again.');
  const pc = device(signedIn.user, form.get('device_id'));
  if (!pc) return json(403, 'Choose a PC linked to this account.');
  const props = { email: signedIn.user.email, deviceId: pc.device_id, pairFingerprint: await pairFingerprint(signedIn.user, pc) };
  if (!await currentDevice(env, props)) return json(403, 'Pair this PC again before authorizing access.');
  const approved = await oauth.approveConsent(request, handle, { scope: [CONTROL] });
  await env.OAUTH_KV.delete(markerKey);
  const { redirectTo } = await oauth.completeAuthorization({ request: approved.request, userId: signedIn.user.email, metadata: { deviceId: pc.device_id }, scope: [CONTROL], props });
  approved.headers.set('Location', redirectTo);
  return new Response(null, { status: 302, headers: approved.headers });
}

const apiHandler = {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname !== '/mcp' || url.search) return json(404, 'MCP route not found.');
    if (!ctx.auth.scope.includes(CONTROL)) return insufficientScope(ctx.auth, [CONTROL]);
    const pc = await currentDevice(env, ctx.props);
    if (!pc || !await grantStillActive(env, ctx.auth, ctx.props)) return json(401, 'Access to this PC ended. Connect again.', { 'WWW-Authenticate': `Bearer error="invalid_token", resource_metadata="${env.GATEWAY_ORIGIN}/.well-known/oauth-protected-resource/mcp"` });
    if (!['POST', 'DELETE'].includes(request.method)) return json(405, 'This endpoint supports JSON requests. Streaming is not supported.', { Allow: 'POST, DELETE' });
    let body, rpc;
    if (request.method === 'POST') {
      if (!(request.headers.get('Content-Type') || '').toLowerCase().startsWith('application/json')) return json(415, 'JSON required.');
      const text = await readLimitedText(request, 1024 * 1024);
      try {
        const parsed = JSON.parse(text);
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error();
        rpc = parsed;
        body = text;
      } catch { return json(400, 'JSON object required.'); }
      const blocked = blockedToolResponse(rpc);
      if (blocked) return Response.json(blocked, { headers: { 'Cache-Control': 'no-store' } });
    }
    const headers = new Headers({ 'Content-Type': 'application/json', Accept: 'application/json' });
    for (const name of ['Mcp-Session-Id', 'MCP-Protocol-Version', 'Last-Event-ID']) {
      const value = request.headers.get(name);
      if (value && value.length <= 1024 && !/[\r\n]/.test(value)) headers.set(name, value);
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);
    try {
      const token = await env.OAUTH_PROVIDER.unwrapToken(ctx.auth.token);
      const user = await readCurrentUser(ctx.props.email, env.STEALTH_AUTH);
      const ticket = token && user && await issuePcTicket(env.STEALTH_AUTH, user, pc, request.method, '/mcp', body, [`oauth:grant:${token.userId}:${token.grantId}`, `oauth:token:${token.userId}:${token.grantId}:${token.id}`]);
      if (!ticket) return json(401, 'PC request authorization ended.');
      headers.set('X-Stealth-PC-Authorization', ticket);
      const upstream = await fetch(pc.mcp_url, { method: request.method, headers, body, redirect: 'manual', signal: controller.signal });
      if (upstream.status >= 300 && upstream.status < 400) return json(502, 'Device endpoint redirected. Reconnect the PC.');
      const outputHeaders = new Headers({ 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
      for (const name of ['Mcp-Session-Id', 'MCP-Protocol-Version']) if (upstream.headers.has(name)) outputHeaders.set(name, upstream.headers.get(name));
      if (upstream.status === 202 || upstream.status === 204) return new Response(null, { status: upstream.status, headers: outputHeaders });
      const contentType = (upstream.headers.get('Content-Type') || '').split(';')[0].toLowerCase();
      if (contentType !== 'application/json') {
        controller.abort();
        return json(502, 'This gateway requires a JSON device response. Streaming is not supported.');
      }
      outputHeaders.set('Content-Type', contentType + '; charset=utf-8');
      let text = await readLimitedText(upstream, 8 * 1024 * 1024);
      if (rpc) text = JSON.stringify(publicMcpResponse(rpc, JSON.parse(text)));
      return new Response(text, { status: upstream.status, headers: outputHeaders });
    } catch { return json(controller.signal.aborted ? 504 : 502, controller.signal.aborted ? 'Device request timed out.' : 'Device request failed.'); }
    finally { clearTimeout(timer); }
  }
};

async function controlCsrf(env, session) {
  if (typeof env.CONTROL_CSRF_SECRET !== 'string' || env.CONTROL_CSRF_SECRET.length < 32) throw new Error('Control protection is unavailable.');
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey('raw', encoder.encode(env.CONTROL_CSRF_SECRET), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return Array.from(new Uint8Array(await crypto.subtle.sign('HMAC', key, encoder.encode('stealth-control-v1:' + session))), b => b.toString(16).padStart(2, '0')).join('');
}

function publishes(value) {
  if (!value || typeof value !== 'object') return false;
  return Object.entries(value).some(([key, item]) => key === 'kind' && item === 'publish_pages' || key === 'auto_publish_pages' && item === true || item && typeof item === 'object' && publishes(item));
}

async function deviceControl(request, env, signedIn) {
  if (!signedIn) return json(401, 'Private sign-in is required.');
  if (request.method !== 'POST') return json(405, 'POST is required.');
  if (new URL(request.url).search) return json(400, 'Control route parameters are not accepted.');
  if (request.headers.get('Origin') !== env.GATEWAY_ORIGIN || request.headers.get('Sec-Fetch-Site') === 'cross-site' || !safeEqual(request.headers.get('X-Stealth-CSRF'), await controlCsrf(env, signedIn.session))) return json(403, 'Request could not be verified.');
  if (!(request.headers.get('Content-Type') || '').startsWith('application/json')) return json(415, 'JSON is required.');
  let body;
  try { body = JSON.parse(await readLimitedText(request, 1024 * 1024)); }
  catch (error) { return json(error instanceof RangeError ? 413 : 400, 'Valid bounded JSON is required.'); }
  if (!body || typeof body !== 'object' || Array.isArray(body) || Object.keys(body).some(key => !['device_id', 'path', 'method', 'payload'].includes(key)) || typeof body.device_id !== 'string') return json(400, 'Choose a paired PC.');
  const snapshot = body.method === 'GET' && typeof body.path === 'string' && /^\/stream\/[A-Za-z0-9_.-]{1,128}\.mjpg$/.test(body.path);
  if (!(body.method === 'GET' && body.path === '/meta' || body.method === 'POST' && body.path === '/action' || snapshot)) return json(400, 'Control route is not allowed.');
  if (body.method === 'GET' && body.payload !== undefined || body.method === 'POST' && (!body.payload || typeof body.payload !== 'object' || Array.isArray(body.payload))) return json(400, 'Invalid control payload.');
  if (publishes(body.payload)) return json(403, 'Publishing is disabled in private mode.');
  const pc = device(signedIn.user, body.device_id);
  if (!pc || !await liveDevice(signedIn.user, pc, env.STEALTH_AUTH)) return json(401, 'Access to this PC ended. Pair it again.');
  if (!await claimEndpoint(env.STEALTH_AUTH, signedIn.user.email, pc.mcp_url)) return json(403, 'PC ownership could not be verified.');
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);
  try {
    const outgoing = body.method === 'POST' ? JSON.stringify(body.payload) : undefined;
    const ticket = await issuePcTicket(env.STEALTH_AUTH, signedIn.user, pc, body.method, body.path, outgoing, ['session:' + signedIn.session]);
    if (!ticket) return json(401, 'PC request authorization ended.');
    const response = await fetch(pc.mcp_url.slice(0, -4) + body.path, { method: body.method, headers: { Accept: snapshot ? 'multipart/x-mixed-replace' : 'application/json', 'Content-Type': 'application/json', 'X-Stealth-PC-Authorization': ticket }, ...(outgoing === undefined ? {} : { body: outgoing }), redirect: 'manual', signal: controller.signal });
    if (response.status >= 300 && response.status < 400) return json(502, 'PC connection redirected.');
    if (snapshot) {
      if (!response.ok || !response.body) return json(502, 'PC screen is unavailable.');
      const reader = response.body.getReader();
      let bytes = new Uint8Array(), start = -1;
      try {
        while (bytes.length <= 2 * 1024 * 1024) {
          const next = await reader.read();
          if (next.done) break;
          if (bytes.length + next.value.length > 2 * 1024 * 1024) return json(502, 'PC screen frame exceeds the limit.');
          const joined = new Uint8Array(bytes.length + next.value.length); joined.set(bytes); joined.set(next.value, bytes.length); bytes = joined;
          for (let i = Math.max(0, bytes.length - next.value.length - 1); i < bytes.length - 1; i++) {
            if (start < 0 && bytes[i] === 255 && bytes[i + 1] === 216) start = i;
            if (start >= 0 && bytes[i] === 255 && bytes[i + 1] === 217) return new Response(bytes.slice(start, i + 2), { headers: { 'Content-Type': 'image/jpeg', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' } });
          }
        }
        return json(502, 'PC screen frame is unavailable.');
      } finally { await reader.cancel(); }
    }
    if (!(response.headers.get('Content-Type') || '').startsWith('application/json')) return json(502, 'Invalid PC response.');
    return new Response(await readLimitedText(response, 8 * 1024 * 1024), { status: response.status, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' } });
  } catch { return json(controller.signal.aborted ? 504 : 502, 'PC request failed.'); }
  finally { controller.abort(); clearTimeout(timer); }
}

function provider(env) {
  return new OAuthProvider({
    apiRoute: '/mcp', apiHandler,
    defaultHandler: { async fetch(request, environment) {
      if (new URL(request.url).pathname === '/authorize') {
        try { return await authorize(request, environment); }
        catch (error) {
          if (error instanceof AuthorizationError || error instanceof CimdFetchError) return page('<p>This connection expired or could not be verified. Start again.</p>', undefined, 400);
          if (error instanceof RangeError) return json(413, 'Form body is too large.');
          throw error;
        }
      }
      return legacy.fetch(request, environment);
    } },
    authorizeEndpoint: '/authorize', tokenEndpoint: '/oauth/token', clientRegistrationEndpoint: '/oauth/register',
    resourceMetadata: { resource: env.GATEWAY_ORIGIN + '/mcp', authorization_servers: [env.GATEWAY_ORIGIN], resource_name: 'Stealth Commander' },
    scopesSupported: [CONTROL], requiredScopes: [CONTROL], clientIdMetadataDocumentEnabled: true,
    accessTokenTTL: 300, refreshTokenTTL: SESSION_SECONDS, clientRegistrationTTL: 86400,
    tokenExchangeCallback: async options => {
      if (!await currentDevice(options.env, options.props)) throw new OAuthError('invalid_grant', { description: 'Access to this PC ended. Connect again.' });
      return {};
    },
    onError: () => undefined
  });
}

export default {
  async fetch(request, env, ctx) {
    if (!env.ASSETS || !env.GATEWAY_ORIGIN) return json(503, 'Gateway is not configured.');
    if (new URL(request.url).origin !== env.GATEWAY_ORIGIN) return json(421, 'Gateway origin does not match.');
    const path = new URL(request.url).pathname;
    if (path === '/.well-known/openai-apps-challenge') {
      const challenge = env.OPENAI_APPS_CHALLENGE;
      if (new URL(request.url).search || typeof challenge !== 'string' || !challenge.length || challenge.length > 4096 || /[^\x21-\x7e]/.test(challenge)) return json(404, 'Verification challenge is not configured.');
      if (request.method !== 'GET' && request.method !== 'HEAD') return json(405, 'Method is not supported.', { Allow: 'GET, HEAD' });
      return new Response(request.method === 'HEAD' ? null : challenge, { headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' } });
    }
    if (path === '/mcp' && request.headers.get('Origin') && request.headers.get('Origin') !== env.GATEWAY_ORIGIN) return json(403, 'Origin is not allowed.');
    try {
      if (PUBLIC_INFORMATION_PATHS.has(path)) {
        if (new URL(request.url).search) return json(404, 'Public page not found.');
        if (request.method !== 'GET' && request.method !== 'HEAD') return json(405, 'Method is not supported.', { Allow: 'GET, HEAD' });
        // Preserve clean URLs for Cloudflare's HTML handling; forward no account credentials.
        const assetUrl = new URL(PUBLIC_INFORMATION_PATHS.get(path), request.url);
        const asset = await env.ASSETS.fetch(new Request(assetUrl, { method: request.method, redirect: 'manual' }));
        if (asset.status !== 200) return json(404, 'Public page not found.');
        const headers = new Headers(asset.headers);
        headers.delete('Set-Cookie');
        const homepage = path === '/', bundle = path === '/app.js' || path === '/app.css';
        if (path === '/app.js') headers.set('Content-Type', 'text/javascript; charset=utf-8');
        if (path === '/app.css') headers.set('Content-Type', 'text/css; charset=utf-8');
        if (path === '/robots.txt') headers.set('Content-Type', 'text/plain; charset=utf-8');
        if (path === '/sitemap.xml') headers.set('Content-Type', 'application/xml; charset=utf-8');
        if (path === '/social-card.png') headers.set('Content-Type', 'image/png');
        if (path === '/jdm-stickerwall.svg') headers.set('Content-Type', 'image/svg+xml; charset=utf-8');
        headers.set('Cache-Control', homepage || bundle ? 'no-store' : 'public, max-age=300');
        headers.set('Content-Security-Policy', homepage
          ? "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
          : "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'");
        headers.set('Referrer-Policy', 'no-referrer');
        headers.set('X-Content-Type-Options', 'nosniff');
        headers.set('X-Frame-Options', 'DENY');
        return new Response(request.method === 'HEAD' ? null : asset.body, { status: 200, headers });
      }
      if (path === '/sign-in') {
        if (new URL(request.url).search) return json(404, 'Page not found.');
        if (request.method !== 'GET' && request.method !== 'HEAD') return json(405, 'Method is not supported.', { Allow: 'GET, HEAD' });
        return signInPage(request.method);
      }
      if (path === '/sign-up') {
        if (new URL(request.url).search) return json(404, 'Page not found.');
        if (request.method !== 'GET' && request.method !== 'HEAD') return json(405, 'Method is not supported.', { Allow: 'GET, HEAD' });
        return signUpPage(request.method);
      }
      if (typeof env.AUTH_STORE?.fetch !== 'function') return json(503, 'Gateway is not configured.');
      env = { ...env, STEALTH_AUTH: authStore(env.AUTH_STORE), OAUTH_KV: authStore(env.AUTH_STORE, 'oauth:', true) };
      await env.STEALTH_AUTH.initialize();
      if (path === '/dashboard') {
        if (new URL(request.url).search) return json(404, 'Page not found.');
        if (request.method !== 'GET' && request.method !== 'HEAD') return json(405, 'Method is not supported.', { Allow: 'GET, HEAD' });
        if (!await account(request, env)) return new Response(null, { status: 303, headers: { Location: '/sign-in', 'Cache-Control': 'no-store', Vary: 'Cookie' } });
        // The existing index remains private; fetch its canonical asset URL only after session verification.
        const asset = await env.ASSETS.fetch(new Request(new URL('/', request.url), { method: request.method, redirect: 'manual' }));
        if (asset.status !== 200) return json(503, 'Dashboard is temporarily unavailable.');
        const headers = new Headers(asset.headers);
        headers.delete('Set-Cookie');
        headers.set('Cache-Control', 'private, no-store');
        headers.set('Vary', 'Cookie');
        headers.set('X-Content-Type-Options', 'nosniff');
        return new Response(request.method === 'HEAD' ? null : asset.body, { status: 200, headers });
      }
      if (path.startsWith('/api/billing/')) {
        const billing = await handleBilling(request, env, path === '/api/billing/webhook' ? null : await account(request, env));
        if (billing) return billing;
      }
      if (path === '/api/account/session') {
        if (request.method !== 'GET' || new URL(request.url).search || request.headers.get('Origin') && request.headers.get('Origin') !== env.GATEWAY_ORIGIN || request.headers.get('Sec-Fetch-Site') === 'cross-site') return json(403, 'Request could not be verified.');
        const signedIn = await account(request, env);
        return signedIn ? Response.json({ csrfToken: await controlCsrf(env, signedIn.session) }, { headers: { 'Cache-Control': 'no-store' } }) : json(401, 'Private sign-in is required.');
      }
      if (path === '/api/device/control') return await deviceControl(request, env, await account(request, env));
      const publicProtocol = ['/authorize', '/mcp', '/oauth/token', '/oauth/register', '/.well-known/oauth-authorization-server', '/.well-known/oauth-protected-resource/mcp'].includes(path);
      if (!path.startsWith('/api/') && !publicProtocol) {
        if (!await account(request, env)) return json(403, 'Private sign-in is required.');
        const asset = await env.ASSETS.fetch(request), headers = new Headers(asset.headers);
        headers.set('Cache-Control', 'private, no-store');
        headers.set('Vary', 'Cookie');
        headers.set('X-Content-Type-Options', 'nosniff');
        return new Response(asset.body, { status: asset.status, headers });
      }
      if (request.method === 'POST' && (['/oauth/token', '/oauth/register', '/authorize', '/api/auth/login', '/api/auth/register'].includes(path) || path.startsWith('/api/devices/'))) {
        const body = await readLimitedText(request, 16384);
        const denied = await authRateLimit(request, env, path, body);
        if (denied) return denied;
        request = new Request(request.url, { method: request.method, headers: request.headers, body });
      }
      const pending = [];
      const requestContext = { ...ctx, waitUntil(promise) { pending.push(Promise.resolve(promise)); }, passThroughOnException() { ctx.passThroughOnException?.(); } };
      const response = await provider(env).fetch(request, env, requestContext);
      await Promise.all(pending);
      if (!await env.OAUTH_KV.commit()) return json(409, 'Authorization changed. Start the connection again.');
      return response;
    }
    catch (error) {
      if (error instanceof RangeError) return json(413, 'Request body is too large.');
      return json(503, 'Gateway is temporarily unavailable.');
    }
  }
};
