import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Background, BackgroundVariant, Handle, MarkerType, Position, ReactFlow, ReactFlowProvider, useKeyPress, useNodesState, useReactFlow } from '@xyflow/react';
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
    <Handle type="target" position={Position.Left} isConnectable={false} />
    <div className="node-heading"><span className="node-number">{data.number}</span><span>{data.category}</span></div>
    <strong>{data.title}</strong><p>{data.summary}</p><div className="node-footer"><span>{data.tag}</span></div>
    <Handle type="source" position={Position.Right} isConnectable={false} />
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
    <div className="roadmap"><p>Mac, Linux, and more stealth tools coming soon.</p></div>
  </aside>;
}

function Workspace() {
  const initialSelected = window.location.hash === '#setup' ? 'client' : 'account';
  const [selected, setSelected] = useState(initialSelected);
  const initialNodes = useMemo(() => steps.map(step => ({ id: step.id, type: 'step', position: { ...step.position }, data: step, selected: step.id === initialSelected })), []);
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [setupOpen, setSetupOpen] = useState(window.location.hash === '#setup');
  const [sessionState, setSessionState] = useState('checking');
  const [announcement, setAnnouncement] = useState('');
  const activeCheck = useRef(null);
  const flow = useReactFlow();
  const ctrlPressed = useKeyPress('Control', { preventDefault: false });
  const endpoint = new URL('/mcp', window.location.origin).href;
  const record = useCallback(message => setAnnouncement(message), []);
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
  const selectStep = useCallback(id => {
    setSelected(id); setNodes(previous => previous.map(node => ({ ...node, selected: node.id === id })));
    record(`Selected ${stepById[id].title.toLowerCase()}.`);
  }, [record, setNodes]);
  const openSetup = useCallback(() => { setSetupOpen(true); record('Opened Windows setup instructions.'); }, [record]);
  useEffect(() => {
    const handleHash = () => { if (window.location.hash === '#setup') { selectStep('client'); openSetup(); } };
    window.addEventListener('hashchange', handleHash); return () => window.removeEventListener('hashchange', handleHash);
  }, [openSetup, selectStep]);
  function navigate(path, label) { record(`Opened ${label.toLowerCase()}.`); window.location.assign(path); }
  function resetMap() {
    setNodes(steps.map(step => ({ id: step.id, type: 'step', position: { ...step.position }, data: step, selected: step.id === 'account' })));
    setSelected('account');
    window.requestAnimationFrame(() => flow.fitView({ padding: .2, duration: 220, maxZoom: 1 }));
    record('Reset the connection map.'); setAnnouncement('Map layout reset. Your account and PCs were not changed.');
  }
  const sessionLabel = { checking: 'Checking account', authenticated: 'Signed in', 'signed-out': 'Not signed in', unavailable: 'Account check unavailable' }[sessionState];
  return <div className="command-app">
    <header className="app-header">
      <button type="button" className="brand-button" onClick={resetMap} aria-label="Stealth Commander — reset connection map"><span className="brand-mark"><img src="/icon.png" width="40" height="40" alt="" /></span><span><strong>Stealth Commander</strong></span></button>
      <div className="header-session" role="status" aria-label={sessionLabel} title={sessionLabel}><span className={`status-square ${sessionState === 'authenticated' ? 'is-known' : 'is-unconfirmed'}`} aria-hidden="true"/><span className="session-label">{sessionLabel}</span></div>
      <a className="header-github" href="https://github.com/maplesyzzurp/Stealth-Commander-MCP" target="_blank" rel="noopener noreferrer" aria-label="GitHub repository" title="Open GitHub repository"><svg viewBox="0 0 24 24" width="23" height="23" fill="currentColor" aria-hidden="true"><path d="M12 .75a11.25 11.25 0 0 0-3.56 21.92c.56.1.77-.24.77-.54v-2.1c-3.13.68-3.79-1.33-3.79-1.33-.51-1.3-1.25-1.64-1.25-1.64-1.02-.7.08-.69.08-.69 1.13.08 1.72 1.16 1.72 1.16 1 1.71 2.63 1.22 3.27.93.1-.73.39-1.22.71-1.5-2.5-.28-5.13-1.25-5.13-5.56 0-1.23.44-2.23 1.16-3.02-.12-.28-.5-1.43.11-2.98 0 0 .95-.3 3.1 1.15a10.78 10.78 0 0 1 5.64 0c2.15-1.46 3.1-1.15 3.1-1.15.61 1.55.23 2.7.11 2.98.72.79 1.16 1.79 1.16 3.02 0 4.32-2.63 5.27-5.14 5.55.4.35.76 1.04.76 2.1v3.08c0 .3.2.65.78.54A11.25 11.25 0 0 0 12 .75Z"/></svg></a>
      <nav className="header-actions" aria-label="Account navigation">{sessionState === 'authenticated' ? <button type="button" className="button button-primary" onClick={() => navigate('/dashboard', 'Dashboard')}>Open dashboard</button> : <><button type="button" className="button button-quiet header-signin" onClick={() => navigate('/sign-in', 'Sign in')}>Sign in</button><button type="button" className="button button-primary" onClick={() => navigate('/sign-up', 'Create account')}>Create account</button></>}</nav>
    </header>
    <main className="workspace">
      <section className="map-workspace" aria-label="Interactive connection map">
        <div className="map-title"><div><h1>Connect your PC. Choose your tools.</h1><p>Select a step to open its controls.</p></div></div>
        <div className="map-surface">
          <div className="step-selector" aria-label="Select a connection step">{steps.map(step => <button type="button" key={step.id} aria-pressed={selected === step.id} onClick={() => selectStep(step.id)} title={step.title}><span>{step.number}</span><span className="step-selector-label">{step.category}</span></button>)}</div>
          <ReactFlow nodes={nodes} edges={initialEdges} nodeTypes={nodeTypes} onNodesChange={onNodesChange} onNodeClick={(_, node) => selectStep(node.id)} fitView fitViewOptions={{ padding: .2, maxZoom: 1 }} minZoom={.3} maxZoom={1.6} nodesConnectable={false} edgesReconnectable={false} deleteKeyCode={null} zoomOnScroll={false} zoomActivationKeyCode="Control" zoomOnPinch={ctrlPressed} zoomOnDoubleClick={false} panOnScroll={false} preventScrolling={false} defaultEdgeOptions={{ type: 'smoothstep', selectable: false }}>
            <Background variant={BackgroundVariant.Dots} gap={24} size={1} color="#352242" />
          </ReactFlow>
          <div className="canvas-hint">Ctrl + scroll to zoom</div>
          <div className="map-controls" aria-label="Map view controls"><button type="button" aria-label="Zoom out" onClick={() => { flow.zoomOut({ duration: 140 }); record('Zoomed out.'); }}>−</button><button type="button" aria-label="Zoom in" onClick={() => { flow.zoomIn({ duration: 140 }); record('Zoomed in.'); }}>+</button><button type="button" onClick={() => { flow.fitView({ padding: .2, duration: 220, maxZoom: 1 }); record('Fit the connection map to view.'); }}>Fit</button><button type="button" onClick={resetMap}>Reset</button></div>
        </div>

      </section>
      <DetailPanel selected={selected} navigate={navigate} openSetup={openSetup} endpoint={endpoint} onActivity={record} />
    </main>
    <footer className="app-footer"><nav aria-label="Product information"><button type="button" onClick={() => navigate('/about', 'About')}>About</button><button type="button" onClick={() => navigate('/support', 'Support')}>Support</button></nav></footer>
    <SetupDialog open={setupOpen} onClose={() => { setSetupOpen(false); record('Closed setup instructions.'); }} onActivity={record} />
    <span className="sr-only" aria-live="polite">{announcement}</span>
  </div>;
}
export function App() { return <ReactFlowProvider><Workspace /></ReactFlowProvider>; }
const root = document.getElementById('root');
if (root) createRoot(root).render(<App />);
