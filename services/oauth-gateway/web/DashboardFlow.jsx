import React, { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Background, BackgroundVariant, Handle, MarkerType, Position, ReactFlow, ReactFlowProvider, useNodesState, useReactFlow } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import './dashboard-flow.css';

const stages = [
  { id: 'account', number: '01', label: 'ACCOUNT', title: 'Your account', detail: 'Identity and paired machines', position: { x: 20, y: 118 } },
  { id: 'pc', number: '02', label: 'WINDOWS CLIENT', title: 'Your Windows PC', detail: 'One selected device', position: { x: 306, y: 118 } },
  { id: 'tools', number: '03', label: 'MCP CONNECTION', title: 'Your AI tools', detail: 'Local clients and remote OAuth', position: { x: 592, y: 14 } },
  { id: 'sessions', number: '04', label: 'HIDDEN WORKSPACE', title: 'Your desktops', detail: 'Owned sessions and observations', position: { x: 592, y: 232 } },
];
const edges = [ ['account', 'pc'], ['pc', 'tools'], ['pc', 'sessions'] ].map(([source, target]) => ({ id: `${source}-${target}`, source, target, type: 'smoothstep', markerEnd: { type: MarkerType.ArrowClosed, width: 15, height: 15 } }));
const emptyState = { signedIn: false, pairedCount: 0, selectedDevice: false, checked: false, liveCount: null, connectedClients: null, totalClients: null };
function snapshot() { return { ...emptyState, ...window.dashboardSnapshot?.() }; }
function openTab(name) { window.showTab?.(name, document.querySelector(`.tab[data-tab="${name}"]`)); }
function tag(id, state) {
  if (id === 'account') return state.signedIn ? `${state.pairedCount} paired ${state.pairedCount === 1 ? 'PC' : 'PCs'}` : 'Sign in required';
  if (id === 'pc') return !state.selectedDevice ? 'No PC selected' : state.checked ? 'Device responding' : 'Connection not confirmed';
  if (id === 'tools') return state.checked ? `${state.connectedClients} / ${state.totalClients} local clients` : 'Not checked';
  return state.checked ? `${state.liveCount} live ${state.liveCount === 1 ? 'desktop' : 'desktops'}` : 'Not checked';
}
function NetworkNode({ data, selected }) {
  return <div className={`network-node${selected ? ' is-selected' : ''}`}>
    {data.id !== 'account' && <Handle type="target" position={Position.Left} />}
    <div className="network-node-heading"><span>{data.number}</span>{data.label}<span className="network-node-select" aria-hidden="true">{selected ? '−' : '+'}</span></div>
    <strong>{data.title}</strong><p>{data.detail}</p>
    <div className="network-node-footer"><span>{data.status}</span><span aria-hidden="true">↗</span></div>
    {['account', 'pc'].includes(data.id) && <Handle type="source" position={Position.Right} />}
  </div>;
}
const nodeTypes = { network: NetworkNode };
function DashboardMap() {
  const [state, setState] = useState(snapshot), [selected, setSelected] = useState('pc'), [copyStatus, setCopyStatus] = useState('');
  const initialNodes = useMemo(() => stages.map(stage => ({ id: stage.id, type: 'network', position: { ...stage.position }, data: { ...stage, status: tag(stage.id, state) }, selected: stage.id === 'pc' })), []);
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const { fitView, zoomIn, zoomOut } = useReactFlow();
  useEffect(() => {
    const update = () => setState(snapshot());
    const show = event => { if (event.detail === 'devices') window.requestAnimationFrame(() => fitView({ padding: .2, duration: 220 })); };
    window.addEventListener('stealth-dashboard-state', update);
    window.addEventListener('stealth-dashboard-tab', show);
    update();
    return () => { window.removeEventListener('stealth-dashboard-state', update); window.removeEventListener('stealth-dashboard-tab', show); };
  }, [fitView]);
  useEffect(() => { setNodes(current => current.map(node => ({ ...node, selected: node.id === selected, data: { ...node.data, status: tag(node.id, state) } }))); }, [state, selected, setNodes]);
  async function copySetup() {
    try { await navigator.clipboard.writeText('stealth-commander setup --enable-remote'); setCopyStatus('Setup command copied'); }
    catch { setCopyStatus('Select and copy the command below'); }
  }
  function reset() { setSelected('pc'); setNodes(initialNodes.map(node => ({ ...node, position: { ...node.position }, selected: node.id === 'pc', data: { ...node.data, status: tag(node.id, state) } }))); window.requestAnimationFrame(() => fitView({ padding: .2, duration: 220 })); }
  const stage = stages.find(item => item.id === selected);
  return <div className="dashboard-network">
    <div className="network-canvas-column">
      <div className="network-section-heading"><span>CONNECTION MAP</span><span>SELECT A NODE TO MANAGE IT</span></div>
      <div className="network-canvas" aria-label="Your account and device connection map">
        <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} onNodesChange={onNodesChange} onNodeClick={(_, node) => setSelected(node.id)} onNodeDoubleClick={(_, node) => openTab(({ account: 'account', pc: 'account', tools: 'connect', sessions: 'live' })[node.id])} fitView fitViewOptions={{ padding: .2 }} minZoom={.3} maxZoom={1.5} nodesConnectable={false} deleteKeyCode={null} panOnScroll zoomOnScroll={false} ariaLabelConfig={{ 'node.a11yDescription.default': 'Select a node to see its controls. Use arrow keys to move it.' }}>
          <Background variant={BackgroundVariant.Dots} color="#382447" gap={20} size={1} />
        </ReactFlow>
        <div className="network-selector" aria-label="Select connection stage">{stages.map(item => <button type="button" key={item.id} title={item.title} aria-pressed={selected === item.id} onClick={() => setSelected(item.id)}><span>{item.number}</span><span className="network-selector-label">{item.label}</span></button>)}</div>
        <span className="network-canvas-hint">Drag nodes to arrange your workspace.</span>
        <div className="network-controls"><button type="button" aria-label="Zoom connection map in" onClick={() => zoomIn({ duration: 180 })}>+</button><button type="button" aria-label="Zoom connection map out" onClick={() => zoomOut({ duration: 180 })}>−</button><button type="button" onClick={() => fitView({ padding: .2, duration: 220 })}>Fit</button><button type="button" onClick={reset}>Reset</button></div>
      </div>
      <div className="network-caption"><span className="network-caption-mark" aria-hidden="true" /><span>Layout shows the connection path. Status comes from your account and the selected PC.</span><button type="button" onClick={() => { window.loadAccountMe?.(); window.poll?.(); }}>Refresh status</button></div>
    </div>
    <aside className="network-inspector" aria-label="Selected connection actions">
      <div className="network-inspector-label"><span>{stage.label}</span><span>{stage.number} / 04</span></div>
      <h2>{stage.title}</h2><div className="network-state">{tag(selected, state)}</div>
      {selected === 'account' && <><p>Manage your account, paired Windows PCs, and access settings.</p><button type="button" onClick={() => openTab('account')}>Open account <span aria-hidden="true">↗</span></button><button type="button" className="sec" onClick={() => window.loadAccountMe?.()}>Refresh paired PCs</button></>}
      {selected === 'pc' && <><p>{state.selectedDevice ? 'A PC is selected. Open its account record or inspect the installed client from Settings.' : 'Pair a Windows PC, then select it to use its installed tools and hidden desktops.'}</p><button type="button" onClick={() => openTab('account')}>{state.pairedCount ? 'Choose a paired PC' : 'Pair your first PC'}<span aria-hidden="true">↗</span></button><button type="button" className="sec" onClick={copySetup}>Copy setup command</button><code className="network-command">stealth-commander setup --enable-remote</code><span className="network-copy-status" role="status">{copyStatus}</span>{state.selectedDevice && <button type="button" className="sec" onClick={() => openTab('settings')}>Open diagnostics</button>}</>}
      {selected === 'tools' && <><p>Configure supported local clients or connect through the OAuth MCP endpoint.</p><button type="button" onClick={() => openTab('connect')}>Manage connections <span aria-hidden="true">↗</span></button><button type="button" className="sec" onClick={() => openTab('usage')}>View usage and logs</button><div className="network-detail-note">Local client status is available after the selected PC responds.</div></>}
      {selected === 'sessions' && <><p>Launch and inspect supported hidden sessions on your selected Windows PC.</p><button type="button" onClick={() => openTab('live')}>Open live desktops <span aria-hidden="true">↗</span></button><button type="button" className="sec" onClick={() => openTab('usage')}>View session activity</button><div className="network-detail-note">Session controls use the tools provided by your installed Windows client.</div></>}
      <div className="network-roadmap"><span>UP NEXT</span><p>Mac, Linux, and more stealth tools coming soon.</p></div>
    </aside>
  </div>;
}

const root = document.getElementById('dashboard-flow-root');
if (root) createRoot(root).render(<ReactFlowProvider><DashboardMap /></ReactFlowProvider>);
