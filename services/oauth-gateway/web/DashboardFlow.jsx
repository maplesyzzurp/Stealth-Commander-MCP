import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './dashboard-flow.css';

const emptyState = { signedIn: false, pairedCount: 0, selectedDevice: false, checked: false, liveCount: null, connectedClients: null, totalClients: null };
function snapshot() { return { ...emptyState, ...window.dashboardSnapshot?.() }; }
function openTab(name) { window.showTab?.(name, document.querySelector(`.tab[data-tab="${name}"]`)); }

function DashboardTasks() {
  const [state, setState] = useState(snapshot);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState('');
  useEffect(() => {
    const update = () => setState(snapshot());
    window.addEventListener('stealth-dashboard-state', update);
    update();
    return () => window.removeEventListener('stealth-dashboard-state', update);
  }, []);

  async function refreshStatus() {
    if (refreshing) return;
    setRefreshing(true);
    setRefreshError('');
    try {
      await window.loadAccountMe?.();
      await window.poll?.();
      setState(snapshot());
    } catch {
      setRefreshError('Could not refresh. Try again.');
    } finally {
      setRefreshing(false);
    }
  }

  const pcStatus = !state.signedIn ? 'Sign in required' : !state.selectedDevice ? 'No PC selected' : state.checked ? 'Device responding' : 'Connection not confirmed';
  const pcDescription = !state.signedIn ? 'Sign in to manage your paired Windows PCs.' : !state.pairedCount ? 'Pair a Windows PC from your account to get started.' : 'Choose the Windows PC you want to use.';
  const toolsStatus = state.checked ? `${state.connectedClients} / ${state.totalClients} clients configured` : 'Configuration not checked';
  const desktopStatus = state.checked ? `${state.liveCount} active ${state.liveCount === 1 ? 'desktop' : 'desktops'}` : 'Sessions not checked';

  return <section className="dashboard-tasks" aria-label="Desktop controls">
    <div className="tasks-toolbar">
      <span className="tasks-account-status">{state.signedIn ? `${state.pairedCount} paired ${state.pairedCount === 1 ? 'PC' : 'PCs'}` : 'Sign in to manage your PCs'}</span>
      <button type="button" className="sec tasks-refresh" disabled={refreshing} onClick={refreshStatus}>{refreshing ? 'Refreshing...' : 'Refresh status'}</button>
    </div>
    {refreshError && <p className="tasks-error" role="status">{refreshError}</p>}
    <div className="task-cards">
      <article className="task-card" data-task="pc">
        <h2>Windows PC</h2>
        <p className="task-status">{pcStatus}</p>
        <p className="task-description">{pcDescription}</p>
        <button type="button" onClick={() => openTab('account')}>{!state.signedIn ? 'Sign in' : state.pairedCount ? 'Choose a PC' : 'Pair a PC'}</button>
      </article>
      <article className="task-card" data-task="tools">
        <h2>AI tools</h2>
        <p className="task-status">{toolsStatus}</p>
        <p className="task-description">Connect an MCP client or configure a supported local app.</p>
        <button type="button" className="sec" onClick={() => openTab('connect')}>Connect AI / MCP</button>
      </article>
      <article className="task-card" data-task="desktops">
        <h2>Hidden desktops</h2>
        <p className="task-status">{desktopStatus}</p>
        <p className="task-description">View and manage supported sessions on your selected PC.</p>
        <button type="button" className="sec" onClick={() => openTab('live')}>Open desktops</button>
      </article>
    </div>
    {!state.pairedCount && <p className="tasks-setup">Need the Windows client? <a href="/about#setup-title">Read the setup guide</a>.</p>}
  </section>;
}

const root = document.getElementById('dashboard-flow-root');
if (root) createRoot(root).render(<DashboardTasks />);
