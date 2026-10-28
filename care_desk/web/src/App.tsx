import { useEffect, useState } from 'react';
import { api } from './api';
import { ContactsView } from './contacts/ContactsView';
import { DashboardView } from './dashboard/DashboardView';
import { useDesk } from './desk';
import { EmailView } from './email/EmailView';
import { useNow } from './format';
import { Icon, type IconName } from './icons';
import { InboxView } from './inbox/InboxView';
import { isOverdue } from './tickets/sla';
import { TicketsView } from './tickets/TicketsView';
import type { Desk } from './types';
import { useToast } from './ui';

// Views already in the new desk are picked by the address's #, so a reload or the Back button keeps them.
// What follows a / is for the view itself (#email/oldlead opens that journey).
const VIEWS = { dashboard: DashboardView, inbox: InboxView, tickets: TicketsView, contacts: ContactsView, email: EmailView };
type View = keyof typeof VIEWS;
const viewFromHash = (): View => {
  const v = location.hash.slice(1).split('/')[0];
  return v in VIEWS ? v as View : 'dashboard';
};

// The rest still open in the current desk.
const NAV: { label: string; icon: IconName; view?: View; admin?: boolean }[] = [
  { label: 'Dashboard', icon: 'chart', view: 'dashboard' },
  { label: 'Inbox', icon: 'inbox', view: 'inbox' },
  { label: 'Tickets', icon: 'ticket', view: 'tickets' },
  { label: 'Contacts', icon: 'users', view: 'contacts' },
  { label: 'Email', icon: 'mail', view: 'email' },
  { label: 'Team', icon: 'shield', admin: true },
];

function Badge({ n }: { n: number }) {
  return n > 0 ? <span className="badge">{n}</span> : null;
}
function OverdueBadge({ desk }: { desk: Desk }) {
  const now = useNow(60_000);
  return <Badge n={desk.tickets.filter(t => t.status !== 'Resolved' && isOverdue(desk, t, now)).length} />;
}

export function App() {
  const { data: desk, error, refetch, isFetching } = useDesk();
  const toast = useToast();
  const [view, setView] = useState(viewFromHash);
  useEffect(() => {
    const follow = () => { setView(viewFromHash()); window.scrollTo(0, 0); };
    window.addEventListener('hashchange', follow);
    return () => window.removeEventListener('hashchange', follow);
  }, []);

  // A refresh that fails once the desk is showing says so, instead of blanking the page.
  useEffect(() => { if (error && desk) toast(error.message, 'u'); }, [error, desk, toast]);

  // The page also drives the clock (escalations, journey emails) whenever it is open.
  useEffect(() => {
    const t = setInterval(() => {
      if (document.visibilityState === 'visible') api('/api/desk/tick', {}).catch(() => {});
    }, 60_000);
    return () => clearInterval(t);
  }, []);

  const Current = VIEWS[view];
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
        {NAV.filter(n => !n.admin || isAdmin).map(n => n.view
          ? <a key={n.label} className="nav" href={`#${n.view}`} aria-current={view === n.view ? 'page' : undefined}>
              <Icon name={n.icon} size={22} />{n.label}
              {n.view === 'tickets' && desk && <OverdueBadge desk={desk} />}
              {n.view === 'inbox' && desk && <Badge n={desk.counts.inboxProblems} />}
            </a>
          : <a key={n.label} className="nav" href="/desk" title={`${n.label} opens in the current desk`}>
              <Icon name={n.icon} size={22} />{n.label}
            </a>)}
      </nav>

      <main>
        {desk ? <Current desk={desk} />
          : <p className="loading">{error ? `Could not load the desk: ${error.message}` : 'Loading the desk'}</p>}
      </main>
    </div>
  );
}
