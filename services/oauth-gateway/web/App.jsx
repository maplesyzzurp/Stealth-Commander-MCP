import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { addEdge, Background, BackgroundVariant, Handle, MarkerType, Position, ReactFlow, ReactFlowProvider, useEdgesState, useNodesState, useReactFlow } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import './app.css';

const steps = [
  { id: 'account', number: '01', category: 'ACCOUNT', title: 'Your account', summary: 'Create an account or sign in.', tag: 'Public sign-up', position: { x: 30, y: 36 } },
  { id: 'client', number: '02', category: 'WINDOWS CLIENT', title: 'Prepare your PC', summary: 'Install the Windows runtime.', tag: 'Private distribution', position: { x: 30, y: 266 } },
  { id: 'pair', number: '03', category: 'DEVICE PAIRING', title: 'Pair a Windows PC', summary: 'Choose the machine you control.', tag: 'Local setup', position: { x: 362, y: 150 } },
  { id: 'connect', number: '04', category: 'MCP CONNECTION', title: 'Connect your tools', summary: 'Sign in through OAuth.', tag: 'Select one paired PC', position: { x: 694, y: 36 } },
  { id: 'sessions', number: '05', category: 'WORKSPACE', title: 'Open your sessions', summary: 'Inspect the PC in the dashboard.', tag: 'Status checked there', position: { x: 694, y: 266 } },
];
const initialEdges = [
  { id: 'account-pair', source: 'account', target: 'pair' }, { id: 'client-pair', source: 'client', target: 'pair' },
  { id: 'pair-connect', source: 'pair', target: 'connect' }, { id: 'connect-sessions', source: 'connect', target: 'sessions' },
].map(edge => ({ ...edge, type: 'smoothstep', markerEnd: { type: MarkerType.ArrowClosed, width: 17, height: 17 } }));
const installCommands = ['node ./bin/stealth-commander.js install', 'node ./bin/stealth-commander.js doctor'];
const pairingCommands = ['node ./bin/stealth-commander.js login', 'node ./bin/stealth-commander.js setup --enable-remote'];
const stepById = Object.fromEntries(steps.map(step => [step.id, step]));

function StepNode({ data, selected }) {
  return <div className={`step-node${selected ? ' is-selected' : ''}`}>
    <Handle type="target" position={Position.Left} aria-label={`Connect to ${data.title}`} />
    <div className="node-heading"><span className="node-number">{data.number}</span><span>{data.category}</span><span className="node-selection" aria-hidden="true">{selected ? '−' : '+'}</span></div>
    <strong>{data.title}</strong><p>{data.summary}</p><div className="node-footer"><span>{data.tag}</span><span aria-hidden="true">↗</span></div>
    <Handle type="source" position={Position.Right} aria-label={`Connect from ${data.title}`} />
  </div>;
}
const nodeTypes = { step: StepNode };

function CopyButton({ value, label = 'Copy', onActivity, className = 'button button-quiet' }) {
  const [feedback, setFeedback] = useState('');
  const timer = useRef();
  useEffect(() => () => window.clearTimeout(timer.current), []);
  async function copy() {
    window.clearTimeout(timer.current);
    try { await navigator.clipboard.writeText(value); setFeedback('Copied'); onActivity(`${label} copied to clipboard.`); }
    catch { setFeedback('Copy unavailable — select the text'); onActivity('Clipboard access was unavailable.'); }
    timer.current = window.setTimeout(() => setFeedback(''), 4000);
  }
  return <span className="copy-control"><button type="button" className={className} onClick={copy}>{feedback === 'Copied' ? 'Copied ✓' : label}</button><span className="copy-feedback" role="status">{feedback.startsWith('Copy unavailable') ? feedback : ''}</span></span>;
}
function CommandBlock({ commands, onActivity, label }) {
  return <div className="command-block"><div className="command-label"><span>{label}</span><CopyButton value={commands.join('\n')} label="Copy commands" onActivity={onActivity} /></div><pre tabIndex="0" aria-label={label}>{commands.map((command, index) => <code key={command}><span className="command-prompt" aria-hidden="true">&gt; </span>{command}{index < commands.length - 1 ? '\n' : ''}</code>)}</pre></div>;
}
function SetupDialog({ open, onClose, onActivity }) {
  const dialog = useRef(null);
  useEffect(() => { if (open && !dialog.current.open) dialog.current.showModal(); if (!open && dialog.current.open) dialog.current.close(); }, [open]);
  return <dialog ref={dialog} className="setup-dialog" aria-labelledby="setup-dialog-title" onCancel={event => { event.preventDefault(); onClose(); }}>
    <div className="dialog-heading"><div><span className="eyebrow">WINDOWS X64 / SOURCE INSTALL</span><h2 id="setup-dialog-title">Prepare your Windows client</h2></div><button type="button" className="icon-button" onClick={onClose} aria-label="Close setup instructions">×</button></div>
    <p>Use a reviewed source archive supplied by the operator. The Windows client is currently distributed privately.</p>
    <div className="requirement-strip"><span>Windows x64</span><span>Node.js 18+</span><span>Python 3.11+ x64</span></div>
    <p className="instruction">Extract the archive. Open PowerShell in the folder containing <code>package.json</code>.</p>
    <CommandBlock commands={installCommands} label="1 / INSTALL AND CHECK" onActivity={onActivity} />
    <p className="small-note">Install adds dependencies and updates supported existing local client configurations. Read each readiness result before starting a workflow.</p>
    <CommandBlock commands={pairingCommands} label="2 / SIGN IN AND PAIR" onActivity={onActivity} />
    <p className="small-note">Login prompts for your account key without echoing it. The setup command pairs this PC and explicitly enables remote access. Commands use the Windows account’s file and network permissions.</p>
    <div className="dialog-bottom"><span>Pair only a PC you own or administer.</span><button type="button" className="button button-primary" onClick={onClose}>Done</button></div>
  </dialog>;
}
function DetailPanel({ selected, navigate, openSetup, endpoint, onActivity }) {
  const step = stepById[selected];
  return <aside className="detail-panel" aria-label="Selected step actions">
    <div className="detail-header"><span className="eyebrow">STEP {step.number} / {step.category}</span><span className="detail-index">{step.number}<span> / 05</span></span></div>
    <div className="detail-content" key={selected}>
      <h2>{step.title}</h2>
      {selected === 'account' && <>
        <p>Create your account, then sign in to manage your paired PCs. Account creation is open.</p>
        <div className="action-stack"><button type="button" className="button button-primary" onClick={() => navigate('/sign-up', 'Create account')}>Create account <span aria-hidden="true">↗</span></button><button type="button" className="button" onClick={() => navigate('/sign-in', 'Sign in')}>Sign in to an existing account</button></div>
        <div className="detail-note"><span className="eyebrow">BEFORE CONNECTING</span><p>An account and the Windows client are separate. The client is currently distributed privately; the ChatGPT plugin is not publicly listed.</p></div>
      </>}
      {selected === 'client' && <>
        <p>The client runs on your Windows PC. If you have an approved source archive, install it from the extracted folder.</p>
        <CommandBlock commands={installCommands} label="POWERSHELL / SOURCE FOLDER" onActivity={onActivity} />
        <div className="action-stack"><button type="button" className="button button-primary" onClick={openSetup}>Open setup instructions <span aria-hidden="true">↗</span></button><button type="button" className="button" onClick={() => navigate('/about', 'About the Windows client')}>Requirements and availability</button></div>
        <p className="small-note">Windows x64 · Node.js 18+ · Python 3.11+ x64. Additional applications have their own prerequisites.</p>
      </>}
      {selected === 'pair' && <>
        <p>Run local setup on a Windows PC you own or administer. Sign in with your account key and explicitly enable remote access.</p>
        <CommandBlock commands={pairingCommands} label="POWERSHELL / SOURCE FOLDER" onActivity={onActivity} />
        <div className="action-stack"><button type="button" className="button button-primary" onClick={() => navigate('/dashboard', 'Dashboard')}>Manage paired PCs <span aria-hidden="true">↗</span></button><button type="button" className="button" onClick={openSetup}>Read the setup steps</button></div>
        <p className="small-note">The client must be installed first. Hidden desktops separate GUI input; Windows account permissions still apply.</p>
      </>}
      {selected === 'connect' && <>
        <p>Use a compatible MCP client’s OAuth connection flow. Sign in and explicitly choose the paired PC you want to authorize.</p>
        <div className="endpoint-box"><span className="eyebrow">OAUTH MCP ENDPOINT</span><code tabIndex="0">{endpoint}</code><CopyButton value={endpoint} label="Copy endpoint" onActivity={onActivity} /></div>
        <div className="action-stack"><button type="button" className="button button-primary" onClick={() => navigate('/dashboard', 'Dashboard')}>Open connection dashboard <span aria-hidden="true">↗</span></button><button type="button" className="button" onClick={() => navigate('/support', 'Connection help')}>Connection help</button></div>
        <p className="small-note">The ChatGPT plugin is not publicly listed. The PC must stay online and have its Windows client running.</p>
      </>}
      {selected === 'sessions' && <>
        <p>The dashboard shows your paired PCs and the tools each installed client provides.</p>
        <div className="device-status"><span className="status-square is-unknown" aria-hidden="true"/><div><strong>PC status unknown</strong><p>Open the dashboard to inspect a paired PC.</p></div></div>
        <div className="action-stack"><button type="button" className="button button-primary" onClick={() => navigate('/dashboard', 'Dashboard')}>Open dashboard <span aria-hidden="true">↗</span></button><button type="button" className="button" onClick={() => navigate('/support', 'Session help')}>Session and capture help</button></div>
        <div className="detail-note"><span className="eyebrow">WINDOW OBSERVATIONS</span><p>Supported sessions can publish cached window images. Availability depends on the application, capture method, and installed runtime.</p></div>
      </>}
    </div>
    <div className="roadmap"><span className="eyebrow">UP NEXT</span><p>Mac, Linux, and more stealth tools coming soon.</p><span>Current client: Windows x64.</span></div>
  </aside>;
}

function Workspace() {
  const initialSelected = window.location.hash === '#setup' ? 'client' : 'account';
  const [selected, setSelected] = useState(initialSelected);
  const initialNodes = useMemo(() => steps.map(step => ({ id: step.id, type: 'step', position: { ...step.position }, data: step, selected: step.id === initialSelected })), []);
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
  const [setupOpen, setSetupOpen] = useState(window.location.hash === '#setup');
  const [inspectorTab, setInspectorTab] = useState('status');
  const [sessionState, setSessionState] = useState('checking');
  const [activity, setActivity] = useState([]);
  const [announcement, setAnnouncement] = useState('');
  const sequence = useRef(0);
  const activeCheck = useRef(null);
  const flow = useReactFlow();
  const endpoint = new URL('/mcp', window.location.origin).href;
  const record = useCallback(message => {
    const event = { id: ++sequence.current, message, time: new Date().toLocaleTimeString([], { hour12: false }) };
    setActivity(previous => [event, ...previous].slice(0, 20));
  }, []);
  const checkSession = useCallback(async () => {
    activeCheck.current?.abort();
    if (navigator.onLine === false) {
      activeCheck.current = null;
      setSessionState('unavailable');
      record('Browser is offline. Account status unavailable.');
      return;
    }
    const controller = new AbortController(); activeCheck.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), 12000);
    setSessionState('checking'); record('Account session check requested.');
    try {
      const response = await fetch('/api/account/session', { credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal, headers: { Accept: 'application/json' } });
      if (activeCheck.current !== controller) return;
      // Only the HTTP status is used. The response body is never read or stored.
      const state = response.status === 200 ? 'authenticated' : [401, 403].includes(response.status) ? 'signed-out' : 'unavailable';
      setSessionState(state);
      record(state === 'authenticated' ? 'Account session confirmed.' : state === 'signed-out' ? 'No current account session.' : 'Account session check unavailable.');
    } catch { if (activeCheck.current === controller) { setSessionState('unavailable'); record('Account session check unavailable.'); } }
    finally { window.clearTimeout(timeout); if (activeCheck.current === controller) activeCheck.current = null; }
  }, [record]);
  useEffect(() => {
    const refreshWhenVisible = () => { if (document.visibilityState === 'visible') checkSession(); };
    const markOffline = () => {
      activeCheck.current?.abort(); activeCheck.current = null;
      setSessionState('unavailable'); record('Browser went offline. Account status unavailable.');
    };
    checkSession();
    window.addEventListener('online', checkSession);
    window.addEventListener('offline', markOffline);
    window.addEventListener('focus', refreshWhenVisible);
    document.addEventListener('visibilitychange', refreshWhenVisible);
    return () => {
      window.removeEventListener('online', checkSession);
      window.removeEventListener('offline', markOffline);
      window.removeEventListener('focus', refreshWhenVisible);
      document.removeEventListener('visibilitychange', refreshWhenVisible);
      activeCheck.current?.abort(); activeCheck.current = null;
    };
  }, [checkSession, record]);
  const selectStep = useCallback((id, focus = false) => {
    setSelected(id); setNodes(previous => previous.map(node => ({ ...node, selected: node.id === id })));
    record(`Selected ${stepById[id].title.toLowerCase()}.`);
    if (focus) flow.fitView({ nodes: [{ id }], padding: .65, maxZoom: 1.1, duration: 220 });
  }, [flow, record, setNodes]);
  const openSetup = useCallback(() => { setSetupOpen(true); record('Opened Windows setup instructions.'); }, [record]);
  useEffect(() => {
    const handleHash = () => { if (window.location.hash === '#setup') { selectStep('client'); openSetup(); } };
    window.addEventListener('hashchange', handleHash); return () => window.removeEventListener('hashchange', handleHash);
  }, [openSetup, selectStep]);
  function navigate(path, label) { record(`Opened ${label.toLowerCase()}.`); window.location.assign(path); }
  function resetMap() {
    setNodes(steps.map(step => ({ id: step.id, type: 'step', position: { ...step.position }, data: step, selected: step.id === 'account' })));
    setEdges(initialEdges.map(edge => ({ ...edge }))); setSelected('account');
    window.requestAnimationFrame(() => flow.fitView({ padding: .2, duration: 220, maxZoom: 1 }));
    record('Reset the connection map.'); setAnnouncement('Map layout reset. Your account and PCs were not changed.');
  }
  function connect(params) {
    if (params.source === params.target) return;
    setEdges(previous => addEdge({ ...params, type: 'smoothstep', markerEnd: { type: MarkerType.ArrowClosed, width: 17, height: 17 } }, previous));
    record('Added a connection to the map layout.');
  }
  const sessionLabel = { checking: 'Checking account', authenticated: 'Signed in', 'signed-out': 'Not signed in', unavailable: 'Account check unavailable' }[sessionState];
  return <div className="command-app">
    <header className="app-header">
      <button type="button" className="brand-button" onClick={resetMap} aria-label="Stealth Commander — reset connection map"><span className="brand-mark"><img src="/icon.png" width="40" height="40" alt="" /></span><span><strong>Stealth Commander</strong><small>CONNECTION WORKSPACE</small></span></button>
      <div className="header-session" role="status" aria-label={sessionLabel} title={sessionLabel}><span className={`status-square ${sessionState === 'authenticated' ? 'is-known' : 'is-unconfirmed'}`} aria-hidden="true"/><span className="session-label">{sessionLabel}</span></div>
      <nav className="header-actions" aria-label="Account navigation"><button type="button" className="button button-quiet header-signin" onClick={() => navigate('/sign-in', 'Sign in')}>Sign in</button><button type="button" className="button button-primary" onClick={() => navigate('/sign-up', 'Create account')}>Create account <span aria-hidden="true">↗</span></button></nav>
    </header>
    <main className="workspace">
      <section className="map-workspace" aria-label="Interactive connection map">
        <div className="map-title"><div><span className="eyebrow">CURRENT PLATFORM / WINDOWS X64</span><h1>Connect your PC. Choose your tools.</h1><p>Select a step to see its actions.</p></div><span className="map-label">INTERACTIVE MAP</span></div>
        <div className="map-surface">
          <div className="step-selector" aria-label="Select a connection step">{steps.map(step => <button type="button" key={step.id} aria-pressed={selected === step.id} onClick={() => selectStep(step.id, true)} title={step.title}><span>{step.number}</span><span className="step-selector-label">{step.category}</span></button>)}</div>
          <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onNodeClick={(_, node) => selectStep(node.id)} onNodeDragStop={(_, node) => record(`Moved ${stepById[node.id].title.toLowerCase()} in the map.`)} onConnect={connect} onEdgesDelete={() => record('Removed a connection from the map layout.')} fitView fitViewOptions={{ padding: .2, maxZoom: 1 }} minZoom={.3} maxZoom={1.6} deleteKeyCode={['Backspace', 'Delete']} defaultEdgeOptions={{ type: 'smoothstep' }}>
            <Background variant={BackgroundVariant.Dots} gap={24} size={1} color="#273831" />
          </ReactFlow>
          <div className="canvas-hint">Drag to arrange <span>·</span> Connect handles to edit the map</div>
          <div className="map-controls" aria-label="Map view controls"><button type="button" aria-label="Zoom out" onClick={() => { flow.zoomOut({ duration: 140 }); record('Zoomed out.'); }}>−</button><button type="button" aria-label="Zoom in" onClick={() => { flow.zoomIn({ duration: 140 }); record('Zoomed in.'); }}>+</button><button type="button" onClick={() => { flow.fitView({ padding: .2, duration: 220, maxZoom: 1 }); record('Fit the connection map to view.'); }}>Fit</button><button type="button" onClick={resetMap}>Reset</button></div>
        </div>
        <section className="inspector" aria-label="System and activity inspector">
          <div className="inspector-heading"><div className="inspector-tabs" role="tablist" aria-label="Inspector"><button type="button" id="status-tab" role="tab" aria-selected={inspectorTab === 'status'} aria-controls="status-panel" onClick={() => setInspectorTab('status')}>System status</button><button type="button" id="activity-tab" role="tab" aria-selected={inspectorTab === 'activity'} aria-controls="activity-panel" onClick={() => setInspectorTab('activity')}>Activity <span>{activity.length}</span></button></div><span className="eyebrow inspector-label">LOCAL UI / THIS VISIT</span></div>
          {inspectorTab === 'status' ? <div id="status-panel" role="tabpanel" aria-labelledby="status-tab" className="status-grid"><div><span className="status-label">ACCOUNT SESSION</span><strong>{sessionLabel}</strong><button type="button" className="text-button" onClick={checkSession} disabled={sessionState === 'checking'}>{sessionState === 'checking' ? 'Checking…' : 'Check again ↗'}</button></div><div><span className="status-label">PAIRED PC</span><strong>Not checked</strong><button type="button" className="text-button" onClick={() => navigate('/dashboard', 'Dashboard')}>Inspect in dashboard ↗</button></div><div><span className="status-label">WINDOWS CLIENT</span><strong>Private distribution</strong><button type="button" className="text-button" onClick={openSetup}>Setup instructions ↗</button></div></div> : <div id="activity-panel" role="tabpanel" aria-labelledby="activity-tab" className="activity-list" tabIndex="0">{activity.length ? activity.map(event => <div className="activity-row" key={event.id}><time>{event.time}</time><span>{event.message}</span></div>) : <p>Actions you take in this workspace appear here.</p>}</div>}
        </section>
      </section>
      <DetailPanel selected={selected} navigate={navigate} openSetup={openSetup} endpoint={endpoint} onActivity={record} />
    </main>
    <footer className="app-footer"><span>STEALTH COMMANDER <span className="footer-separator">/</span> WINDOWS X64</span><span className="layout-note">The map edits your layout. Use a button to open a service.</span><nav aria-label="Product information"><button type="button" onClick={() => navigate('/about', 'About')}>About</button><button type="button" onClick={() => navigate('/support', 'Support')}>Support</button></nav></footer>
    <SetupDialog open={setupOpen} onClose={() => { setSetupOpen(false); record('Closed setup instructions.'); }} onActivity={record} />
    <span className="sr-only" aria-live="polite">{announcement}</span>
  </div>;
}
export function App() { return <ReactFlowProvider><Workspace /></ReactFlowProvider>; }
const root = document.getElementById('root');
if (root) createRoot(root).render(<App />);
