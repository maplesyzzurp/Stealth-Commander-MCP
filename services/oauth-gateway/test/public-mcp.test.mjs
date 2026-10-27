import assert from 'node:assert/strict';
import { test } from 'node:test';
import { publicMcpResponse } from '../src/public-mcp.js';

test('hosted tool discovery declares OAuth and distinguishes capture from executable actions', () => {
  const response = publicMcpResponse({ method: 'tools/list' }, { result: { tools: [
    { name: 'capture_observation', description: 'Read an already-published observer frame.' },
    { name: 'run_command' }, { name: 'start_observer' }, { name: 'start_tunnel' }
  ] } });
  assert.deepEqual(response.result.tools.map(tool => tool.name), ['capture_observation', 'run_command', 'start_observer']);
  const [capture, command, producer] = response.result.tools;
  assert.deepEqual(capture.securitySchemes, [{ type: 'oauth2', scopes: ['pc:control'] }]);
  assert.deepEqual(capture._meta.securitySchemes, capture.securitySchemes);
  assert.equal(capture.annotations.readOnlyHint, true);
  assert.equal(capture.annotations.destructiveHint, false);
  assert.equal(capture.annotations.openWorldHint, false);
  assert.equal(command.annotations.readOnlyHint, false);
  assert.equal(command.annotations.destructiveHint, true);
  assert.equal(producer.annotations.readOnlyHint, false);
});

test('capture returns one unchanged image and removes private connection URLs from provenance', () => {
  const image = { type: 'image', mimeType: 'image/png', data: 'synthetic-png-base64' };
  const provenance = { sha256: 'a'.repeat(64), mcp_url: 'https://synthetic.trycloudflare.com/' + 'b'.repeat(48) + '/mcp', captured_at: '2026-10-25T12:00:00Z' };
  const response = publicMcpResponse({ method: 'tools/call', params: { name: 'capture_observation' } }, {
    result: { content: [image, { type: 'text', text: JSON.stringify(provenance) }], structuredContent: provenance }
  });
  assert.deepEqual(response.result.content[0], image);
  assert.equal(response.result.content.filter(block => block.type === 'image').length, 1);
  assert.deepEqual(response.result.structuredContent, { sha256: 'a'.repeat(64), captured_at: '2026-10-25T12:00:00Z' });
  assert.deepEqual(JSON.parse(response.result.content[1].text), response.result.structuredContent);
});


test('all forwarded declarations preserve schemas and carry truthful control scope', () => {
  const schema = { type: 'object', properties: { session: { type: 'string' } }, required: ['session'] };
  const tools = ['doctor', 'dashboard_status', 'list_hidden_desktops', 'start_observer', 'observer_action', 'session_action', 'future_tool']
    .map(name => ({ name, inputSchema: schema, _meta: { retained: 'yes' }, annotations: { title: 'Owned operation' } }));
  const exposed = publicMcpResponse({ method: 'tools/list' }, { result: { tools } }).result.tools;
  for (const tool of exposed) {
    assert.deepEqual(tool.inputSchema, schema);
    assert.equal(tool._meta.retained, 'yes');
    assert.equal(tool.annotations.title, 'Owned operation');
    assert.deepEqual(tool.securitySchemes, [{ type: 'oauth2', scopes: ['pc:control'] }]);
    assert.deepEqual(tool._meta.securitySchemes, tool.securitySchemes);
    for (const key of ['readOnlyHint', 'destructiveHint', 'openWorldHint']) assert.equal(typeof tool.annotations[key], 'boolean');
  }
  assert.equal(exposed.find(tool => tool.name === 'doctor').annotations.readOnlyHint, true);
  for (const name of ['dashboard_status', 'list_hidden_desktops', 'start_observer', 'observer_action']) {
    assert.equal(exposed.find(tool => tool.name === name).annotations.readOnlyHint, false);
    assert.equal(exposed.find(tool => tool.name === name).annotations.destructiveHint, false);
  }
  assert.equal(exposed.find(tool => tool.name === 'future_tool').annotations.destructiveHint, true);
  assert.equal(exposed.find(tool => tool.name === 'future_tool').annotations.openWorldHint, true);
});
