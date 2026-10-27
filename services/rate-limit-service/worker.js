function reply(status, value) {
  return Response.json(value, {
    status,
    headers: { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method !== "POST" || url.pathname !== "/check") return reply(404, { error: "Not found" });
    if (request.headers.get("Content-Type")?.split(";")[0].trim() !== "application/json") return reply(415, { error: "JSON required" });
    if (!request.body) return reply(400, { error: "Body required" });
    const reader = request.body.getReader();
    let size = 0;
    const parts = [];
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > 2048) {
          await reader.cancel();
          return reply(413, { error: "Body too large" });
        }
        parts.push(value);
      }
      const bytes = new Uint8Array(size);
      let offset = 0;
      for (const part of parts) { bytes.set(part, offset); offset += part.byteLength; }
      let body;
      try { body = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)); }
      catch { return reply(400, { error: "Invalid JSON" }); }
      if (!body || typeof body.key !== "string" || body.key.length < 1 || body.key.length > 256) return reply(400, { error: "Invalid key" });
      if (typeof env.LIMITS?.limit !== "function") return reply(503, { error: "Limiter unavailable" });
      const result = await env.LIMITS.limit({ key: body.key });
      if (typeof result?.success !== "boolean") return reply(503, { error: "Limiter unavailable" });
      return reply(200, { success: result.success });
    } catch {
      return reply(503, { error: "Limiter unavailable" });
    } finally {
      reader.releaseLock();
    }
  },
};
