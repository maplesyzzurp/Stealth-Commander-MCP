const PRIVATE_FIELDS = new Set([
  'api_key', 'device_token', 'session_token', 'access_token', 'refresh_token', 'token',
  'dashboard_url', 'local_dashboard_url', 'mcp_url', 'local_mcp_url', 'direct_tunnel_url',
  'tunnel_url', 'portal_url', 'local_portal_url', 'public_origin'
]);
const PRIVATE_URL = /https?:\/\/(?:[a-z0-9-]+\.trycloudflare\.com|(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?)\/[A-Za-z0-9_-]{16,128}(?:\/[A-Za-z0-9_/?=&.%-]*)?/gi;

function metadata(value, depth = 0) {
  if (depth > 40) throw new Error('Device metadata is too deeply nested.');
  if (Array.isArray(value)) return value.map(v => metadata(v, depth + 1));
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).filter(([key]) => !PRIVATE_FIELDS.has(key)).map(([key, v]) => [key,
      key === 'stdout' || key === 'stderr' ? v : metadata(v, depth + 1)
    ]));
  }
  if (typeof value === 'string') return value.replace(PRIVATE_URL, '[private connection URL omitted]');
  return value;
}

export function blockedToolResponse(request) {
  if (request.method !== 'tools/call' || request.params?.name !== 'start_tunnel') return null;
  return { jsonrpc: '2.0', id: request.id ?? null, error: { code: -32601, message: 'Start remote access locally before connecting. This tool is unavailable through OAuth.' } };
}

const OAUTH_SCHEMES = [{ type: 'oauth2', scopes: ['pc:control'] }];
const LOCAL_OBSERVATION = new Set(['doctor', 'dashboard_status', 'list_hidden_desktops', 'start_observer', 'observer_action']);

function publicTool(tool) {
  const capture = tool.name === 'capture_observation';
  const observation = LOCAL_OBSERVATION.has(tool.name);
  const readOnly = capture || tool.name === 'doctor';
  const meta = tool._meta && typeof tool._meta === 'object' && !Array.isArray(tool._meta) ? tool._meta : {};
  return {
    ...tool,
    securitySchemes: OAUTH_SCHEMES.map(scheme => ({ ...scheme, scopes: [...scheme.scopes] })),
    _meta: { ...meta, securitySchemes: OAUTH_SCHEMES.map(scheme => ({ ...scheme, scopes: [...scheme.scopes] })) },
    annotations: {
      ...(tool.annotations && typeof tool.annotations === 'object' ? tool.annotations : {}),
      readOnlyHint: readOnly,
      destructiveHint: !(capture || observation),
      openWorldHint: !(capture || observation)
    }
  };
}

export function publicMcpResponse(request, response) {
  if (!response || typeof response !== 'object' || Array.isArray(response)) throw new Error('Invalid MCP response.');
  if (request.method === 'tools/list' && Array.isArray(response.result?.tools)) {
    return { ...response, result: { ...response.result, tools: response.result.tools.filter(tool => tool.name !== 'start_tunnel').map(publicTool) } };
  }
  if (request.method !== 'tools/call' || request.params?.name === 'run_command' || !response.result) return response;
  const result = metadata({ ...response.result, content: undefined });
  if (Array.isArray(response.result.content)) {
    result.content = response.result.content.map(block => {
      if (block?.type !== 'text' || typeof block.text !== 'string') return block;
      let parsed;
      try { parsed = JSON.parse(block.text); } catch { return block; }
      return parsed && typeof parsed === 'object' ? { ...block, text: JSON.stringify(metadata(parsed)) } : block;
    });
  }
  return { ...response, result };
}
