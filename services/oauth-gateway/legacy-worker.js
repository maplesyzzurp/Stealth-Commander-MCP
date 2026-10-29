const TRUSTED_ORIGINS = new Set([
  "https://stealthcommander.pages.dev",
  "http://127.0.0.1:61077"
]);
const MAX_REQUEST_BYTES = 1024 * 1024;
const MAX_RESPONSE_BYTES = 8 * 1024 * 1024;
const SESSION_SECONDS = 43200;
const DEVICE_SECONDS = 2592000;

function validMcpUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password && !url.port &&
      /^[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com$/.test(url.hostname) &&
      /^\/[A-Za-z0-9_-]{32,128}\/mcp$/.test(url.pathname) && !url.search && !url.hash;
  } catch (_) { return false; }
}

async function readLimitedText(message, limit) {
  if (Number(message.headers.get("Content-Length")) > limit) throw new RangeError("Body exceeds limit.");
  const reader = message.body && message.body.getReader();
  if (!reader) return "";
  const decoder = new TextDecoder();
  let size = 0, text = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) return text + decoder.decode();
      size += value.byteLength;
      if (size > limit) {
        await reader.cancel();
        throw new RangeError("Body exceeds limit.");
      }
      text += decoder.decode(value, { stream: true });
    }
  } finally { reader.releaseLock(); }
}

function getStore(env) {
  if (!env?.STEALTH_AUTH) throw new Error("Account storage is unavailable.");
  return env.STEALTH_AUTH;
}

function corsHeaders(req) {
  const origin = req.headers.get("Origin");
  return {
    ...(TRUSTED_ORIGINS.has(origin) ? {
      "Access-Control-Allow-Origin": origin,
      "Access-Control-Allow-Credentials": "true"
    } : {}),
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Stealth-Device-Id",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Cache-Control": "no-store",
    "Vary": "Origin",
    "X-Content-Type-Options": "nosniff"
  };
}

function jsonResp(status, data, req, extraHeaders = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...corsHeaders(req), ...extraHeaders }
  });
}

function randomHex(bytes = 24) {
  const arr = new Uint8Array(bytes);
  crypto.getRandomValues(arr);
  return Array.from(arr, b => b.toString(16).padStart(2, "0")).join("");
}

async function hashPassword(password, saltHex) {
  const enc = new TextEncoder();
  const keyMat = await crypto.subtle.importKey("raw", enc.encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt: enc.encode(saltHex), iterations: 100000, hash: "SHA-256" },
    keyMat,
    256
  );
  return Array.from(new Uint8Array(bits), b => b.toString(16).padStart(2, "0")).join("");
}

async function accountKeyHash(user) {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(user.api_key));
  return Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, "0")).join("");
}

async function createSession(user, store) {
  const current = await readCurrentUser(user.email, store);
  if (!current || !safeEqual(current.api_key, user.api_key)) throw new Error("Account authorization changed.");
  const token = "sc_sess_" + randomHex(24);
  await store.put("session:" + token, JSON.stringify({
    email: user.email, expiresAt: Date.now() + SESSION_SECONDS * 1000,
    accountKeyHash: await accountKeyHash(user)
  }), { expirationTtl: SESSION_SECONDS });
  return token;
}

async function resolveSession(token, store) {
  if (!/^sc_sess_[a-f0-9]{48}$/.test(token)) return null;
  const stored = await store.get("session:" + token);
  if (!stored) return null;
  let session, user;
  try {
    session = JSON.parse(stored);
    if (typeof session.email !== "string" || !Number.isFinite(session.expiresAt) || session.expiresAt <= Date.now()) return null;
    user = await readCurrentUser(session.email, store);
  } catch (_) { return null; }
  if (!user || user.email !== session.email || typeof user.api_key !== "string" ||
      !safeEqual(session.accountKeyHash, await accountKeyHash(user))) return null;
  return user;
}

async function readCurrentUser(email, store) {
  const raw = await store.get("user:" + email);
  if (!raw) return null;
  try {
    const user = JSON.parse(raw);
    if (user.email !== email) return null;
    const authorityRaw = await store.get("accountkey:" + email);
    if (authorityRaw) {
      const authority = JSON.parse(authorityRaw);
      if (!/^sc_live_[a-f0-9]{40}$/.test(authority.api_key)) return null;
      user.api_key = authority.api_key;
    }
    return /^sc_live_[a-f0-9]{40}$/.test(user.api_key) ? user : null;
  } catch (_) { return null; }
}

function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function privateAccountAllowed(env, email) {
  if (String(env.PRIVATE_MODE).toLowerCase() !== 'true') return true;
  try {
    const list = JSON.parse(env.PRIVATE_ACCOUNT_ALLOWLIST);
    return Array.isArray(list) && list.length > 0 && list.every(v => typeof v === 'string' && v.trim().toLowerCase() === v && /^[^\s@]+@[^\s@]+$/.test(v)) && list.includes(email);
  } catch { return false; }
}

function validRegistrationEmail(email) {
  if (email.length > 254) return false;
  const parts = email.split('@');
  if (parts.length !== 2 || parts[0].length > 64) return false;
  const [local, domain] = parts;
  if (!/^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*$/.test(local)) return false;
  const labels = domain.split('.');
  return labels.length >= 2 && !/^\d+$/.test(labels.at(-1)) && labels.every(label =>
    /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label));
}

async function mutateAccount(store, expected, change, credential = null, ownerToken = null) {
  for (let attempt = 0; attempt < 4; attempt++) {
    const key = 'user:' + expected.email, authorityKey = 'accountkey:' + expected.email;
    const raw = await store.get(key), authorityRaw = await store.get(authorityKey);
    if (!raw) return null;
    const user = JSON.parse(raw);
    if (authorityRaw) user.api_key = JSON.parse(authorityRaw).api_key;
    if (user.email !== expected.email || !safeEqual(user.api_key, expected.api_key)) return null;
    const checks = [{ key, value: raw }, { key: authorityKey, value: authorityRaw }];
    if (credential) {
      const credentialKey = 'deviceauth:' + credential.credentialHash;
      const current = await store.get(credentialKey);
      const pc = user.devices?.find(d => d.device_id === credential.pc.device_id);
      if (!current || !safeEqual(pc?.device_credential_hash, credential.credentialHash) || !safeEqual(pc?.oauth_pair_id, credential.pc.oauth_pair_id) || JSON.parse(current).expiresAt <= Date.now()) return null;
      checks.push({ key: credentialKey, value: current });
    }
    if (ownerToken?.startsWith('sc_sess_')) {
      const sessionKey = 'session:' + ownerToken, sessionRaw = await store.get(sessionKey);
      if (!sessionRaw || JSON.parse(sessionRaw).expiresAt <= Date.now()) return null;
      checks.push({ key: sessionKey, value: sessionRaw });
    }
    const changes = await change(user);
    if (!changes) return null;
    if (await store.transact(checks, [{ key, value: JSON.stringify(user) }, ...(changes.puts || [])], changes.deletes || [])) return user;
  }
  return null;
}

async function liveDevice(user, pc, store) {
  if (!pc?.device_credential_hash || !validMcpUrl(pc.mcp_url)) return false;
  const raw = await store.get('deviceauth:' + pc.device_credential_hash);
  const credential = raw && JSON.parse(raw);
  return credential && credential.email === user.email && credential.deviceId === pc.device_id && safeEqual(credential.pairId, pc.oauth_pair_id) &&
    Number.isFinite(credential.expiresAt) && credential.expiresAt > Date.now() && safeEqual(credential.accountKeyHash, await accountKeyHash(user));
}

async function ticketState(store, expected, pc, authorizers) {
  const keys = ['user:' + expected.email, 'accountkey:' + expected.email, 'deviceauth:' + pc.device_credential_hash, ...authorizers];
  const checks = await Promise.all(keys.map(async key => ({ key, value: await store.get(key) })));
  if (!checks[0].value || !checks[2].value || checks.slice(3).some(c => !c.value)) return null;
  const user = JSON.parse(checks[0].value), credential = JSON.parse(checks[2].value);
  if (checks[1].value) user.api_key = JSON.parse(checks[1].value).api_key;
  const current = user.devices?.find(d => d.device_id === pc.device_id);
  if (!safeEqual(user.api_key, expected.api_key) || !safeEqual(current?.device_credential_hash, pc.device_credential_hash) || !safeEqual(current?.oauth_pair_id, pc.oauth_pair_id) || current.mcp_url !== pc.mcp_url ||
      credential.email !== user.email || credential.deviceId !== pc.device_id || !safeEqual(credential.pairId, current.oauth_pair_id) || !safeEqual(credential.accountKeyHash, await accountKeyHash(user)) || !Number.isFinite(credential.expiresAt) || credential.expiresAt <= Date.now()) return null;
  for (const check of checks.slice(3)) {
    const record = JSON.parse(check.value);
    const scale = check.key.startsWith('session:') ? 1 : 1000;
    if (!Number.isFinite(record.expiresAt) || record.expiresAt * scale <= Date.now()) return null;
    if (check.key.startsWith('session:') && (record.email !== user.email || !safeEqual(record.accountKeyHash, await accountKeyHash(user)))) return null;
  }
  return checks;
}

async function issuePcTicket(store, user, pc, method, path, body, authorizers = []) {
  const checks = await ticketState(store, user, pc, authorizers);
  if (!checks) return null;
  const ticket = 'sc_pct_' + randomHex(24), key = 'pcticket:' + await hashText(ticket);
  const value = JSON.stringify({ email: user.email, deviceId: pc.device_id, pairId: pc.oauth_pair_id, credentialHash: pc.device_credential_hash, accountKeyHash: await accountKeyHash(user), method, path, bodyHash: await hashText(body || ''), authorizers: await Promise.all(checks.slice(3).map(async c => ({ key: c.key, hash: await hashText(c.value) }))), expiresAt: Date.now() + 30000 });
  return await store.transact([...checks, { key, value: null }], [{ key, value, expirationTtl: 30 }]) ? ticket : null;
}

async function consumePcTicket(store, resolved, body) {
  if (Object.keys(body).some(k => !['ticket', 'method', 'path', 'body_sha256'].includes(k)) || !/^sc_pct_[a-f0-9]{48}$/.test(body.ticket) || !['GET', 'POST', 'DELETE'].includes(body.method) || typeof body.path !== 'string' || !/^\/(?:mcp|meta|action|stream\/[A-Za-z0-9_.-]{1,128}\.mjpg)$/.test(body.path) || !/^[a-f0-9]{64}$/.test(body.body_sha256)) return false;
  const key = 'pcticket:' + await hashText(body.ticket), raw = await store.get(key);
  if (!raw) return false;
  const ticket = JSON.parse(raw);
  if (ticket.expiresAt <= Date.now() || ticket.email !== resolved.user.email || ticket.deviceId !== resolved.pc.device_id || !safeEqual(ticket.pairId, resolved.pc.oauth_pair_id) || !safeEqual(ticket.credentialHash, resolved.credentialHash) || !safeEqual(ticket.accountKeyHash, await accountKeyHash(resolved.user)) || ticket.method !== body.method || ticket.path !== body.path || !safeEqual(ticket.bodyHash, body.body_sha256) || !Array.isArray(ticket.authorizers)) return false;
  const checks = await ticketState(store, resolved.user, resolved.pc, ticket.authorizers.map(a => a.key));
  if (!checks) return false;
  for (let i = 0; i < ticket.authorizers.length; i++) if (!safeEqual(await hashText(checks[i + 3].value), ticket.authorizers[i].hash)) return false;
  return store.transact([...checks, { key, value: raw }], [], [key]);
}

function sanitizeUser(u) {
  return {
    email: u.email,
    name: u.name || u.email.split("@")[0],
    api_key: u.api_key,
    created_at: u.created_at,
    devices: Array.isArray(u.devices) ? u.devices.map(sanitizeDevice) : []
  };
}

function sanitizeDevice(d) {
  const fields = ["device_id", "hostname", "username", "os", "tunnel_url", "local_portal_url", "mcp_url", "dashboard_url", "updated_at"];
  return Object.fromEntries(fields.filter(k => d[k] !== undefined).map(k => [k, d[k]]));
}

async function hashText(value) {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, "0")).join("");
}

async function claimEndpoint(store, email, endpoint) {
  if (!endpoint) return true;
  if (!validMcpUrl(endpoint) || typeof store.claim !== 'function') return false;
  return store.claim(await hashText(new URL(endpoint).pathname), email);
}

function deviceEntry(body, deviceId) {
  if (body.mcp_url && !validMcpUrl(body.mcp_url)) return null;
  if (body.tunnel_url && (!body.mcp_url || body.tunnel_url !== body.mcp_url.slice(0, -4))) return null;
  for (const field of ["local_portal_url", "dashboard_url"]) {
    if (!body[field]) continue;
    try {
      const link = new URL(body[field]);
      if (link.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(link.hostname) ||
          !link.port || link.username || link.password || link.search || link.hash ||
          !/^\/[A-Za-z0-9_-]{16,128}\/?$/.test(link.pathname)) return null;
    } catch (_) { return null; }
  }
  const entry = { device_id: deviceId, hostname: String(body.hostname || deviceId).slice(0, 128), os: String(body.os || "win32").slice(0, 32), updated_at: new Date().toISOString() };
  for (const field of ["username", "tunnel_url", "local_portal_url", "mcp_url", "dashboard_url"]) {
    if (body[field] !== undefined) {
      if (body[field] !== null && typeof body[field] !== "string") return null;
      if (body[field]?.length > 2048) return null;
      entry[field] = body[field] || null;
    }
  }
  return entry;
}

async function resolveDevice(token, store) {
  if (!/^sc_dev_[a-f0-9]{48}$/.test(token)) return null;
  const credentialHash = await hashText(token);
  const raw = await store.get("deviceauth:" + credentialHash);
  let credential, user;
  try {
    credential = raw && JSON.parse(raw);
    if (!credential || !Number.isFinite(credential.expiresAt) || credential.expiresAt <= Date.now()) return null;
    user = await readCurrentUser(credential.email, store);
  } catch (_) { return null; }
  if (!user || user.email !== credential.email || !safeEqual(credential.accountKeyHash, await accountKeyHash(user))) return null;
  const pc = Array.isArray(user.devices) && user.devices.find(d => d.device_id === credential.deviceId);
  if (!pc || !safeEqual(pc.oauth_pair_id, credential.pairId) || !safeEqual(pc.device_credential_hash, credentialHash)) return null;
  return { user, pc, credentialHash, credential };
}

function parseToken(req) {
  const auth = req.headers.get("Authorization") || "";
  if (auth.startsWith("Bearer ")) return auth.slice(7).trim();
  const cookie = req.headers.get("Cookie") || "";
  const m = cookie.match(/(?:^|;\s*)sc_session=([^;]+)/);
  return m ? decodeURIComponent(m[1].trim()) : "";
}

async function resolveUser(req, store, env = {}) {
  const tok = parseToken(req);
  if (!tok) return null;
  if (tok.startsWith("sc_sess_")) {
    const user = await resolveSession(tok, store);
    return user && privateAccountAllowed(env, user.email) ? { user, token: tok } : null;
  }
  let email = null;
  if (tok.startsWith("sc_live_")) {
    email = await store.get("apikey:" + tok);
  }
  if (!email) return null;
  const user = await readCurrentUser(email, store);
  return user && privateAccountAllowed(env, user.email) && safeEqual(user.api_key, tok) ? { user, token: tok } : null;
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const origin = req.headers.get("Origin");
    if (url.pathname.startsWith("/api/") && origin && !TRUSTED_ORIGINS.has(origin)) {
      return jsonResp(403, { ok: false, error: "Origin is not allowed." }, req);
    }
    if (req.method === "OPTIONS" && url.pathname.startsWith("/api/")) {
      return jsonResp(200, { ok: true }, req);
    }
    if (!url.pathname.startsWith("/api/")) {
      return env.ASSETS.fetch(req);
    }
    const store = getStore(env);
    let body = {};
    if (req.method === "POST" || req.method === "DELETE") {
      try {
        const text = await readLimitedText(req, MAX_REQUEST_BYTES);
        body = text ? JSON.parse(text) : {};
        if (!body || typeof body !== "object" || Array.isArray(body)) throw new SyntaxError();
      } catch (err) {
        return jsonResp(err instanceof RangeError ? 413 : 400, {
          ok: false, error: err instanceof RangeError ? "Request body exceeds 1 MiB." : "Valid JSON object required."
        }, req);
      }
    }

    if (url.pathname.startsWith("/api/devices/")) {
      const path = url.pathname;
      if (path === "/api/devices/pair" && req.method === "POST") {
        const owner = await resolveUser(req, store, env);
        if (!owner) return jsonResp(401, { ok: false, error: "Owner sign-in required." }, req);
        const deviceId = String(body.device_id || "").trim().toLowerCase();
        if (!/^[a-z0-9][a-z0-9._-]{0,127}$/.test(deviceId)) return jsonResp(400, { ok: false, error: "A valid device ID is required." }, req);
        const entry = deviceEntry(body, deviceId);
        if (!entry) return jsonResp(400, { ok: false, error: "Device connection details are invalid." }, req);
        const user = owner.user;
        if (!await claimEndpoint(store, user.email, entry.mcp_url)) return jsonResp(409, { ok: false, error: "This PC connection belongs to another account. Reset its local connection before changing accounts." }, req);
        const token = "sc_dev_" + randomHex(24);
        const credentialHash = await hashText(token);
        entry.oauth_pair_id = randomHex(24);
        entry.device_credential_hash = credentialHash;
        const expiresAt = Date.now() + DEVICE_SECONDS * 1000;
        const updatedUser = await mutateAccount(store, user, async current => {
          current.devices = Array.isArray(current.devices) ? current.devices : [];
          const removed = current.devices.filter(d => d.device_id === deviceId || current.devices.indexOf(d) >= 24);
          current.devices = [entry, ...current.devices.filter(d => d.device_id !== deviceId)].slice(0, 25);
          return { puts: [{ key: 'deviceauth:' + credentialHash, value: JSON.stringify({ email: user.email, deviceId, pairId: entry.oauth_pair_id, accountKeyHash: await accountKeyHash(current), expiresAt }), expirationTtl: DEVICE_SECONDS }], deletes: removed.filter(d => d.device_credential_hash).map(d => 'deviceauth:' + d.device_credential_hash) };
        }, null, owner.token);
        if (!updatedUser) return jsonResp(401, { ok: false, error: 'Account authorization changed. Sign in again.' }, req);
        return jsonResp(200, { ok: true, email: user.email, device_id: deviceId, device_token: token, expires_at: new Date(expiresAt).toISOString(), device: sanitizeDevice(entry) }, req);
      }
      const auth = req.headers.get("Authorization") || "";
      const resolved = await resolveDevice(auth.startsWith("Bearer ") ? auth.slice(7).trim() : "", store);
      if (!resolved || !privateAccountAllowed(env, resolved.user.email)) return jsonResp(401, { ok: false, error: "Valid paired-device credential required." }, req);
      if (body.device_id && body.device_id !== resolved.pc.device_id || body.email && body.email !== resolved.user.email) return jsonResp(403, { ok: false, error: "A device can update only its own connection." }, req);
      if (path === "/api/devices/me" && req.method === "GET") return jsonResp(200, { ok: true, email: resolved.user.email, device_id: resolved.pc.device_id, device: sanitizeDevice(resolved.pc) }, req);
      if (path === '/api/devices/authorize' && req.method === 'POST') {
        if (!await consumePcTicket(store, resolved, body)) return jsonResp(401, { ok: false, error: 'PC request authorization ended.' }, req);
        return jsonResp(200, { ok: true, email: resolved.user.email, device_id: resolved.pc.device_id, device: { device_id: resolved.pc.device_id, mcp_url: resolved.pc.mcp_url } }, req);
      }
      if (path === "/api/devices/heartbeat" && req.method === "POST") {
        const entry = deviceEntry(body, resolved.pc.device_id);
        if (!entry) return jsonResp(400, { ok: false, error: "Device connection details are invalid." }, req);
        if (!await claimEndpoint(store, resolved.user.email, entry.mcp_url)) return jsonResp(409, { ok: false, error: "This PC connection belongs to another account." }, req);
        let updated;
        const user = await mutateAccount(store, resolved.user, current => {
          const pc = current.devices.find(d => d.device_id === resolved.pc.device_id);
          updated = { ...pc, ...entry };
          current.devices = current.devices.map(d => d.device_id === updated.device_id ? updated : d);
          return {};
        }, resolved);
        if (!user) return jsonResp(401, { ok: false, error: 'Pairing ended.' }, req);
        return jsonResp(200, { ok: true, email: resolved.user.email, device_id: updated.device_id, device: sanitizeDevice(updated) }, req);
      }
      if (path === "/api/devices/unpair" && req.method === "POST") {
        const user = await mutateAccount(store, resolved.user, current => {
          current.devices = current.devices.filter(d => d.device_id !== resolved.pc.device_id);
          return { deletes: ['deviceauth:' + resolved.credentialHash] };
        }, resolved);
        if (!user) return jsonResp(401, { ok: false, error: 'Pairing ended.' }, req);
        return jsonResp(200, { ok: true }, req);
      }
      return jsonResp(404, { ok: false, error: "Device route not found." }, req);
    }

    if (url.pathname === "/api/auth/register" && req.method === "POST") {
      if (String(env.PRIVATE_MODE).toLowerCase() === 'true') {
        return jsonResp(403, { ok: false, error: 'Account registration is disabled during private access. Sign in with an existing approved account.' }, req);
      }
      if (typeof body.email !== "string") return jsonResp(400, { ok: false, error: "Email must be a string." }, req);
      const email = String(body.email || "").trim().toLowerCase();
      if (!privateAccountAllowed(env, email)) return jsonResp(403, { ok: false, error: 'Private account access is required.' }, req);
      if (!validRegistrationEmail(email)) return jsonResp(400, { ok: false, error: "Enter a valid email address of at most 254 characters." }, req);
      if (typeof body.password !== 'string' || body.password.length < 12 || body.password.length > 1024) {
        return jsonResp(400, { ok: false, error: "Password must be a string between 12 and 1024 characters." }, req);
      }
      if (body.name !== undefined && (typeof body.name !== 'string' || body.name.length > 128)) {
        return jsonResp(400, { ok: false, error: "Name must be a string of at most 128 characters." }, req);
      }
      const password = body.password;
      const name = (body.name || '').trim() || email.split("@")[0];
      const existing = await store.get("user:" + email);
      if (existing) return jsonResp(409, { ok: false, error: "Account already exists. Please sign in." }, req);
      const salt = randomHex(16);
      const hash = await hashPassword(password, salt);
      const apiKey = "sc_live_" + randomHex(20);
      const user = { email, name, salt, hash, api_key: apiKey, created_at: new Date().toISOString(), devices: [] };
      const created = await store.transact([{ key: 'user:' + email, value: null }], [
        { key: 'user:' + email, value: JSON.stringify(user) },
        { key: 'accountkey:' + email, value: JSON.stringify({ api_key: apiKey }) },
        { key: 'apikey:' + apiKey, value: email }
      ]);
      if (!created) return jsonResp(409, { ok: false, error: 'Account already exists. Please sign in.' }, req);
      const sessionTok = await createSession(user, store);
      const cookie = `sc_session=${sessionTok}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${SESSION_SECONDS}`;
      return jsonResp(200, { ok: true, token: sessionTok, user: sanitizeUser(user) }, req, { "Set-Cookie": cookie });
    }

    if (url.pathname === "/api/auth/login" && req.method === "POST") {
      if (body.email !== undefined && typeof body.email !== "string") return jsonResp(400, { ok: false, error: "Email must be a string." }, req);
      const apiKeyIn = String(body.api_key || "").trim();
      if (apiKeyIn) {
        const email = await store.get("apikey:" + apiKeyIn);
        if (!email) return jsonResp(401, { ok: false, error: "Invalid API key." }, req);
        const user = await readCurrentUser(email, store);
        if (!user || !privateAccountAllowed(env, user.email)) return jsonResp(401, { ok: false, error: "Sign-in failed." }, req);
        if (!safeEqual(user.api_key, apiKeyIn)) return jsonResp(401, { ok: false, error: "Invalid API key." }, req);
        const sessionTok = await createSession(user, store);
        const cookie = `sc_session=${sessionTok}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${SESSION_SECONDS}`;
        return jsonResp(200, { ok: true, token: sessionTok, user: sanitizeUser(user) }, req, { "Set-Cookie": cookie });
      }
      const email = String(body.email || "").trim().toLowerCase();
      const password = String(body.password || "");
      if (!privateAccountAllowed(env, email)) return jsonResp(401, { ok: false, error: 'Sign-in failed.' }, req);
      if (!email || !password) return jsonResp(400, { ok: false, error: "Email and password are required." }, req);
      const user = await readCurrentUser(email, store);
      const hash = await hashPassword(password, user ? user.salt : "00000000000000000000000000000000");
      if (!user || !safeEqual(hash, user.hash)) return jsonResp(401, { ok: false, error: "Incorrect email or password." }, req);
      const sessionTok = await createSession(user, store);
      const cookie = `sc_session=${sessionTok}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${SESSION_SECONDS}`;
      return jsonResp(200, { ok: true, token: sessionTok, user: sanitizeUser(user) }, req, { "Set-Cookie": cookie });
    }

    if (url.pathname === "/api/auth/me" && req.method === "GET") {
      const resolved = await resolveUser(req, store, env);
      if (!resolved) return jsonResp(401, { ok: false, error: "Not signed in." }, req);
      return jsonResp(200, { ok: true, token: resolved.token, user: sanitizeUser(resolved.user) }, req);
    }

    if (url.pathname === "/api/auth/logout" && req.method === "POST") {
      const tok = parseToken(req);
      if (tok && tok.startsWith("sc_sess_")) await store.delete("session:" + tok);
      const cookie = "sc_session=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0";
      return jsonResp(200, { ok: true }, req, { "Set-Cookie": cookie });
    }

    if (url.pathname === "/api/account/rotate-key" && req.method === "POST") {
      const resolved = await resolveUser(req, store, env);
      if (!resolved) return jsonResp(401, { ok: false, error: "Not signed in." }, req);
      const replacement = 'sc_live_' + randomHex(20);
      const user = await mutateAccount(store, resolved.user, current => {
        const oldKey = current.api_key;
        current.api_key = replacement;
        return { puts: [{ key: 'accountkey:' + current.email, value: JSON.stringify({ api_key: replacement }) }, { key: 'apikey:' + replacement, value: current.email }], deletes: ['apikey:' + oldKey] };
      }, null, resolved.token);
      if (!user) return jsonResp(401, { ok: false, error: 'Account authorization changed. Sign in again.' }, req);
      return jsonResp(200, { ok: true, user: sanitizeUser(user) }, req);
    }

    if (url.pathname === "/api/account/devices") {
      const resolved = await resolveUser(req, store, env);
      if (!resolved) return jsonResp(401, { ok: false, error: "Not signed in." }, req);
      const user = resolved.user;
      user.devices = Array.isArray(user.devices) ? user.devices : [];
      if (req.method === "GET") {
        return jsonResp(200, { ok: true, devices: user.devices.map(sanitizeDevice), user: sanitizeUser(user) }, req);
      }
      if (req.method === "POST") {
        if (body.mcp_url && !validMcpUrl(body.mcp_url)) {
          return jsonResp(400, { ok: false, error: "A valid HTTPS device MCP endpoint is required." }, req);
        }
        if (!await claimEndpoint(store, user.email, body.mcp_url)) return jsonResp(409, { ok: false, error: "This PC connection belongs to another account." }, req);
        const devId = String(body.device_id || body.hostname || "windows-pc").trim().toLowerCase();
        const entry = {
          device_id: devId,
          hostname: String(body.hostname || devId),
          username: String(body.username || "user"),
          tunnel_url: body.tunnel_url ? String(body.tunnel_url).replace(/\/+$/, "") : null,
          local_portal_url: body.local_portal_url ? String(body.local_portal_url).replace(/\/+$/, "") : null,
          mcp_url: body.mcp_url ? String(body.mcp_url) : null,
          dashboard_url: body.dashboard_url ? String(body.dashboard_url) : null,
          os: String(body.os || "win32"),
          updated_at: new Date().toISOString()
        };
        const updated = await mutateAccount(store, user, current => {
          const previous = current.devices.find(d => d.device_id === devId);
          const sameCapability = validMcpUrl(previous?.mcp_url) && entry.mcp_url && new URL(previous.mcp_url).pathname === new URL(entry.mcp_url).pathname;
          entry.oauth_pair_id = sameCapability && previous.oauth_pair_id ? previous.oauth_pair_id : randomHex(24);
          const next = { ...previous, ...entry };
          if (!sameCapability) delete next.device_credential_hash;
          current.devices = [next, ...current.devices.filter(d => d.device_id !== devId)].slice(0, 25);
          return { deletes: !sameCapability && previous?.device_credential_hash ? ['deviceauth:' + previous.device_credential_hash] : [] };
        }, null, resolved.token);
        if (!updated) return jsonResp(401, { ok: false, error: 'Account authorization changed.' }, req);
        return jsonResp(200, { ok: true, devices: updated.devices.map(sanitizeDevice), user: sanitizeUser(updated) }, req);
      }
      if (req.method === "DELETE") {
        const devId = String(body.device_id || "").trim().toLowerCase();
        const updated = await mutateAccount(store, user, current => {
          const removed = current.devices.filter(d => (d.device_id || '').toLowerCase() === devId || (d.hostname || '').toLowerCase() === devId);
          current.devices = current.devices.filter(d => !removed.includes(d));
          return { deletes: removed.filter(d => d.device_credential_hash).map(d => 'deviceauth:' + d.device_credential_hash) };
        }, null, resolved.token);
        if (!updated) return jsonResp(401, { ok: false, error: 'Account authorization changed.' }, req);
        return jsonResp(200, { ok: true, devices: updated.devices.map(sanitizeDevice), user: sanitizeUser(updated) }, req);
      }
    }

    if (url.pathname === "/api/mcp" || url.pathname.startsWith("/api/mcp/")) {
      const parts = url.pathname.replace(/^\/api\/mcp\/?/, "").split("/").filter(Boolean);
      if (parts.length && String(env.LEGACY_MCP_URL_KEYS_ENABLED).toLowerCase() !== "true") {
        return jsonResp(410, { ok: false, error: "Credentials in MCP URLs are disabled. Connect with OAuth at /mcp or use an Authorization header and X-Stealth-Device-Id." }, req, { Link: '</mcp>; rel="successor-version"' });
      }
      if (parts.length > 2) return jsonResp(400, { ok: false, error: "Invalid legacy MCP route." }, req);
      const selectedHeader = (req.headers.get("X-Stealth-Device-Id") || "").trim().toLowerCase();
      const targetDev = (parts[1] || selectedHeader).toLowerCase();
      if (parts[1] && selectedHeader && parts[1].toLowerCase() !== selectedHeader) return jsonResp(400, { ok: false, error: "PC selectors do not match." }, req);
      const keyIn = parts[0] || parseToken(req);
      if (!keyIn) return jsonResp(401, { ok: false, error: "Account API key required." }, req);
      const resolved = await resolveUser(new Request(req.url, { headers: { Authorization: "Bearer " + keyIn } }), store, env);
      if (!resolved) return jsonResp(401, { ok: false, error: "Invalid account key." }, req);
      const user = resolved.user;
      if (!/^[a-z0-9][a-z0-9._-]{0,127}$/.test(targetDev)) return jsonResp(400, { ok: false, error: "Select a PC with X-Stealth-Device-Id or an explicitly enabled legacy URL." }, req);
      const devs = Array.isArray(user.devices) ? user.devices : [];
      const dev = devs.find(d => (d.device_id || "").toLowerCase() === targetDev);
      if (!dev) return jsonResp(404, { ok: false, error: "Device is not linked to this account." }, req);
      if (!dev || !dev.mcp_url) return jsonResp(503, { ok: false, error: "No online PC linked to this account." }, req);
      if (!validMcpUrl(dev.mcp_url)) return jsonResp(503, { ok: false, error: "Linked device endpoint is invalid. Reconnect the PC." }, req);
      if (!await liveDevice(user, dev, store)) return jsonResp(401, { ok: false, error: 'Pairing ended. Pair this PC again.' }, req);
      if (!await claimEndpoint(store, user.email, dev.mcp_url)) return jsonResp(403, { ok: false, error: "This PC connection belongs to another account." }, req);
      if (!["GET", "POST", "DELETE"].includes(req.method)) return jsonResp(405, { ok: false, error: "Method is not supported." }, req);
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);
      try {
        const outgoing = (req.method === 'GET' || req.method === 'HEAD') ? undefined : JSON.stringify(body);
        const ticket = await issuePcTicket(store, user, dev, req.method, '/mcp', outgoing, resolved.token.startsWith('sc_sess_') ? ['session:' + resolved.token] : []);
        if (!ticket) return jsonResp(401, { ok: false, error: 'PC request authorization ended.' }, req);
        const upstream = await fetch(dev.mcp_url, {
          method: req.method,
          redirect: "manual",
          signal: controller.signal,
          headers: {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Stealth-PC-Authorization": ticket,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
          },
          body: outgoing
        });
        if (upstream.status >= 300 && upstream.status < 400) {
          return jsonResp(502, { ok: false, error: "Device endpoint redirected. Reconnect the PC." }, req);
        }
        if (upstream.status === 202 || upstream.status === 204) {
          return new Response(null, { status: upstream.status, headers: corsHeaders(req) });
        }
        const text = await readLimitedText(upstream, MAX_RESPONSE_BYTES);
        return new Response(text, { status: upstream.status, headers: { "Content-Type": "application/json; charset=utf-8", ...corsHeaders(req) } });
      } catch (_) {
        return jsonResp(controller.signal.aborted ? 504 : 502, { ok: false, error: controller.signal.aborted ? "Device request timed out." : "Device request failed." }, req);
      } finally { clearTimeout(timer); }
    }

    return jsonResp(404, { ok: false, error: "API route not found" }, req);
  }
};

export { validMcpUrl, readLimitedText, randomHex, hashPassword, safeEqual, createSession, resolveSession, readCurrentUser, claimEndpoint, privateAccountAllowed, liveDevice, issuePcTicket };
import { UPSTREAM_TIMEOUT_MS } from './src/limits.js';
