import { useEffect } from 'react';
import { api } from './api';
import { ContactsView } from './contacts/ContactsView';
import { useDesk } from './desk';
import { Icon, type IconName } from './icons';
import { useToast } from './ui';

// Views not moved yet open in the current desk.
const NAV: { label: string; icon: IconName; here?: boolean; admin?: boolean }[] = [
  { label: 'Dashboard', icon: 'chart' },
  { label: 'Inbox', icon: 'inbox' },
  { label: 'Tickets', icon: 'ticket' },
  { label: 'Contacts', icon: 'users', here: true },
  { label: 'Email', icon: 'mail' },
  { label: 'Team', icon: 'shield', admin: true },
];

export function App() {
  const { data: desk, error, refetch, isFetching } = useDesk();
  const toast = useToast();

  // A refresh that fails once the desk is showing says so, instead of blanking the page.
  useEffect(() => { if (error && desk) toast(error.message, 'u'); }, [error, desk, toast]);

  // The page also drives the clock (escalations, journey emails) whenever it is open.
  useEffect(() => {
    const t = setInterval(() => {
      if (document.visibilityState === 'visible') api('/api/desk/tick', {}).catch(() => {});
    }, 60_000);
    return () => clearInterval(t);
  }, []);

  const isAdmin = !!desk && ['owner', 'admin'].includes(desk.user.role);
  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <img src="/static/favicon.svg" alt="Redefine" width={32} height={32} />
          <span className="word">Redefine</span>
          <span className="divider" aria-hidden="true" />
          <span className="tool">Care Desk</span>
        </div>
        <span className="pill new" title="The new desk. Views not moved yet open in the current one.">
          New desk · <a href="/desk">back to current</a>
        </span>
        {desk && <>
          <span className={`pill ${desk.status.sorting === 'claude' ? 'ok' : 'warn'}`}>
            <span className="dot" />{desk.status.sorting === 'claude' ? 'Claude sorting' : 'Keyword sorting'}
          </span>
          <span className={`pill ${desk.status.email ? 'ok' : 'warn'}`}>
            <span className="dot" />{desk.status.email ? 'Email on' : 'Email off'}
          </span>
        </>}
        <div className="controls">
          {desk && <a className="who" href="/welcome" title={`${desk.user.email} · change your name`}>{desk.user.name || desk.user.email}</a>}
          <button className="btn sm" disabled={isFetching}
            onClick={async () => { const r = await refetch(); if (!r.error) toast('Up to date'); }}>
            <Icon name="refresh" size={15} />Refresh
          </button>
          <a className="btn sm ghost" href="/auth/logout"><Icon name="out" size={15} />Sign out</a>
        </div>
      </header>

      <nav className="side" aria-label="Views">
        {NAV.filter(n => !n.admin || isAdmin).map(n => n.here
          ? <a key={n.label} className="nav" href="/desk-next" aria-current="page"><Icon name={n.icon} size={22} />{n.label}</a>
          : <a key={n.label} className="nav" href="/desk" title={`${n.label} opens in the current desk`}>
              <Icon name={n.icon} size={22} />{n.label}
              {n.label === 'Inbox' && desk && desk.counts.inboxProblems > 0 && <span className="badge">{desk.counts.inboxProblems}</span>}
            </a>)}
      </nav>

      <main>
        {desk ? <ContactsView desk={desk} />
          : <p className="loading">{error ? `Could not load the desk: ${error.message}` : 'Loading the desk'}</p>}
      </main>
    </div>
  );
}
