import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './app.css';

const installCommands = ['node ./bin/stealth-commander.js install', 'node ./bin/stealth-commander.js doctor'];
const pairingCommands = ['node ./bin/stealth-commander.js login', 'node ./bin/stealth-commander.js setup --enable-remote'];

function CopyButton({ value, label = 'Copy', onActivity }) {
  const [feedback, setFeedback] = useState('');
  const timer = useRef();
  useEffect(() => () => window.clearTimeout(timer.current), []);
  async function copy() {
    window.clearTimeout(timer.current);
    try {
      await navigator.clipboard.writeText(value);
      setFeedback('Copied');
      onActivity(`${label} copied to clipboard.`);
    } catch {
      setFeedback('Copy unavailable - select the text');
      onActivity('Clipboard access was unavailable.');
    }
    timer.current = window.setTimeout(() => setFeedback(''), 4000);
  }
  return <span className="copy-control">
    <button type="button" className="button button-quiet" onClick={copy}>{feedback === 'Copied' ? 'Copied' : label}</button>
    <span className="copy-feedback" role="status">{feedback.startsWith('Copy unavailable') ? feedback : ''}</span>
  </span>;
}

function CommandBlock({ commands, onActivity, label }) {
  return <div className="command-block">
    <div className="command-label"><span>{label}</span><CopyButton value={commands.join('\n')} label="Copy commands" onActivity={onActivity} /></div>
    <pre tabIndex="0" aria-label={label}>{commands.join('\n')}</pre>
  </div>;
}

function SetupDialog({ open, onClose, onActivity }) {
  const dialog = useRef(null);
  useEffect(() => {
    if (open && !dialog.current.open) dialog.current.showModal();
    if (!open && dialog.current.open) dialog.current.close();
  }, [open]);
  return <dialog ref={dialog} className="setup-dialog" aria-labelledby="setup-dialog-title" onCancel={event => { event.preventDefault(); onClose(); }}>
    <div className="dialog-heading"><h2 id="setup-dialog-title">Set up your Windows PC</h2><button type="button" className="icon-button" onClick={onClose} aria-label="Close setup instructions">&times;</button></div>
    <p><a className="button button-primary" href="https://github.com/maplesyzzurp/Stealth-Commander-MCP/releases/download/v1.1.3-private.6/stealth-commander-1.1.3-private.6.zip">Download Windows client</a></p>
    <div className="requirement-strip"><span>Windows x64</span><span>Node.js 18+</span><span>Python 3.11+ x64</span></div>
    <p>Extract the archive. Open PowerShell in the folder containing <code>package.json</code>.</p>
    <CommandBlock commands={installCommands} label="1. Install and check" onActivity={onActivity} />
    <p className="small-note">Install adds dependencies and updates supported existing local client configurations. Read each readiness result before continuing.</p>
    <CommandBlock commands={pairingCommands} label="2. Sign in and pair" onActivity={onActivity} />
    <p className="small-note">Login prompts for your account key without echoing it. Setup pairs this PC and enables remote access using your Windows account's permissions.</p>
    <div className="dialog-bottom"><span>Pair only a PC you own or administer.</span><button type="button" className="button button-primary" onClick={onClose}>Done</button></div>
  </dialog>;
}

export function App() {
  const [setupOpen, setSetupOpen] = useState(window.location.hash === '#setup');
  const [sessionState, setSessionState] = useState('checking');
  const [announcement, setAnnouncement] = useState('');
  const activeCheck = useRef(null);
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
  useEffect(() => {
    const handleHash = () => { if (window.location.hash === '#setup') setSetupOpen(true); };
    window.addEventListener('hashchange', handleHash);
    return () => window.removeEventListener('hashchange', handleHash);
  }, []);
  const signedIn = sessionState === 'authenticated';
  const sessionLabel = { checking: 'Checking account', authenticated: 'Signed in', 'signed-out': 'Not signed in', unavailable: 'Account check unavailable' }[sessionState];
  const primaryHref = signedIn ? '/dashboard' : '/sign-up';
  const primaryLabel = signedIn ? 'Open dashboard' : 'Create account';
  return <div className="command-app">
    <header className="app-header">
      <a className="brand-button" href="/" aria-label="Stealth Commander home"><span className="brand-mark"><img src="/icon.png" width="40" height="40" alt="" /></span><strong>Stealth Commander</strong></a>
      <div className="header-session" role="status" aria-label={sessionLabel} title={sessionLabel}><span className={`status-square ${signedIn ? 'is-known' : 'is-unconfirmed'}`} aria-hidden="true"/><span className="session-label">{sessionLabel}</span></div>
      <a className="header-github" href="https://github.com/maplesyzzurp/Stealth-Commander-MCP" target="_blank" rel="noopener noreferrer" aria-label="GitHub repository" title="Open GitHub repository"><svg viewBox="0 0 24 24" width="23" height="23" fill="currentColor" aria-hidden="true"><path d="M12 .75a11.25 11.25 0 0 0-3.56 21.92c.56.1.77-.24.77-.54v-2.1c-3.13.68-3.79-1.33-3.79-1.33-.51-1.3-1.25-1.64-1.25-1.64-1.02-.7.08-.69.08-.69 1.13.08 1.72 1.16 1.72 1.16 1 1.71 2.63 1.22 3.27.93.1-.73.39-1.22.71-1.5-2.5-.28-5.13-1.25-5.13-5.56 0-1.23.44-2.23 1.16-3.02-.12-.28-.5-1.43.11-2.98 0 0 .95-.3 3.1 1.15a10.78 10.78 0 0 1 5.64 0c2.15-1.46 3.1-1.15 3.1-1.15.61 1.55.23 2.7.11 2.98.72.79 1.16 1.79 1.16 3.02 0 4.32-2.63 5.27-5.14 5.55.4.35.76 1.04.76 2.1v3.08c0 .3.2.65.78.54A11.25 11.25 0 0 0 12 .75Z"/></svg></a>
      <nav className="header-actions" aria-label="Account navigation">{!signedIn && <a className="button button-quiet header-signin" href="/sign-in">Sign in</a>}<a className="button button-primary" href={primaryHref}>{primaryLabel}</a></nav>
    </header>
    <main className="landing-main">
      <section className="hero" aria-labelledby="hero-title">
        <h1 id="hero-title">Delegate work to your <span>Windows desktop.</span></h1>
        <p>Connect your PC to your AI assistant. Pair your device, authorize access, and manage desktop sessions from your dashboard.</p>
        <div className="hero-actions"><a className="button button-primary" href={primaryHref}>{primaryLabel}</a><button type="button" className="button" onClick={() => setSetupOpen(true)}>Set up Windows</button></div>
      </section>
      <section className="setup-section" aria-labelledby="setup-title">
        <h2 id="setup-title">Get connected</h2>
        <div className="setup-cards">
          <article className="setup-card">
            <h3><span aria-hidden="true">1.</span> Your account</h3>
            <p>{signedIn ? 'Manage your paired PCs and choose which device your assistant can access.' : 'Create an account to manage your PCs and authorize connections.'}</p>
            <div className="card-actions"><a className="button" href={primaryHref}>{primaryLabel}</a>{!signedIn && <a className="text-link" href="/sign-in">Already have an account? Sign in</a>}</div>
          </article>
          <article className="setup-card">
            <h3><span aria-hidden="true">2.</span> Prepare your PC</h3>
            <p>Install the Windows client from your source archive, then sign in and pair your PC.</p>
            <p className="requirements">Windows x64, Node.js 18+, Python 3.11+ x64.</p>
            <div className="card-actions"><button type="button" className="button" onClick={() => setSetupOpen(true)}>Open setup instructions</button></div>
          </article>
          <article className="setup-card">
            <h3><span aria-hidden="true">3.</span> Connect your assistant</h3>
            <p>Add this endpoint in a compatible MCP client, sign in with OAuth, and choose your paired PC.</p>
            <div className="endpoint-box"><code tabIndex="0" aria-label="MCP endpoint">{endpoint}</code><CopyButton value={endpoint} label="Copy endpoint" onActivity={record} /></div>
            <p className="small-note">Keep the PC online with its Windows client running.</p>
          </article>
        </div>
      </section>
      <p className="roadmap">Mac, Linux, and more stealth tools coming soon.</p>
    </main>
    <footer className="app-footer"><nav aria-label="Product information"><a href="/about">About</a><a href="/support">Support</a></nav></footer>
    <SetupDialog open={setupOpen} onClose={() => setSetupOpen(false)} onActivity={record} />
    <span className="sr-only" aria-live="polite">{announcement}</span>
  </div>;
}

const root = document.getElementById('root');
if (root) createRoot(root).render(<App />);
