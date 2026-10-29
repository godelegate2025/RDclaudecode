import { useEffect } from 'react';
import { api } from './api';
import { ContactsView } from './contacts/ContactsView';
import { DashboardView } from './dashboard/DashboardView';
import { useDesk } from './desk';
import { EmailView } from './email/EmailView';
import { useNow } from './format';
import { Icon, type IconName } from './icons';
import { InboxView } from './inbox/InboxView';
import { onLink, usePath } from './nav';
import { isOverdue } from './tickets/sla';
import { TeamView } from './team/TeamView';
import { TicketsView } from './tickets/TicketsView';
import type { Desk } from './types';
import { useToast } from './ui';

// Each view has its own address: / for the Dashboard, /inbox, /tickets and so on, and what
// follows is for the view itself (/email/oldlead opens that journey). The server serves
// the page at the same addresses (DESK_VIEWS in care_desk/app.py).
const VIEWS = { dashboard: DashboardView, inbox: InboxView, tickets: TicketsView, contacts: ContactsView, email: EmailView, team: TeamView };
type View = keyof typeof VIEWS;
const viewOf = (path: string): View => {
  const v = path.split('/')[1] || 'dashboard';
  return v in VIEWS ? v as View : 'dashboard';
};
const hrefOf = (v: View) => (v === 'dashboard' ? '/' : `/${v}`);

// Old links (/desk#tickets, /desk-next#tickets) reach / with the #view kept; swap it for the view's address.
const old = /^#(\w+)(\/[\w-]+)?$/.exec(location.hash);
if (location.pathname === '/' && old && old[1] in VIEWS) {
  history.replaceState(null, '', old[1] === 'dashboard' ? '/' : `/${old[1]}${old[2] || ''}`);
}

const NAV: { label: string; icon: IconName; view: View; admin?: boolean }[] = [
  { label: 'Dashboard', icon: 'chart', view: 'dashboard' },
  { label: 'Inbox', icon: 'inbox', view: 'inbox' },
  { label: 'Tickets', icon: 'ticket', view: 'tickets' },
  { label: 'Contacts', icon: 'users', view: 'contacts' },
  { label: 'Email', icon: 'mail', view: 'email' },
  { label: 'Team', icon: 'shield', view: 'team', admin: true },
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
  const path = usePath();
  const view = viewOf(path);

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
        {desk && <>
          <span className={`pill ${desk.status.sorting === 'claude' ? 'ok' : 'warn'}`}>
            <span className="dot" />{desk.status.sorting === 'claude' ? 'Claude sorting' : 'Keyword sorting'}
          </span>
          <span className={`pill ${desk.status.email ? 'ok' : 'warn'}`}>
            <span className="dot" />{desk.status.email ? 'Email on' : 'Email off'}
          </span>
        </>}
        <div className="controls">
          {desk && <a className="who" href={`/welcome?next=${encodeURIComponent(path)}`} title={`${desk.user.email} · change your name`}>{desk.user.name || desk.user.email}</a>}
          <button className="btn sm" disabled={isFetching}
            onClick={async () => { const r = await refetch(); if (!r.error) toast('Up to date'); }}>
            <Icon name="refresh" size={15} />Refresh
          </button>
          <a className="btn sm ghost" href="/auth/logout"><Icon name="out" size={15} />Sign out</a>
        </div>
      </header>

      <nav className="side" aria-label="Views">
        {NAV.filter(n => !n.admin || isAdmin).map(n =>
          <a key={n.label} className="nav" href={hrefOf(n.view)} onClick={onLink} aria-current={view === n.view ? 'page' : undefined}>
            <Icon name={n.icon} size={22} />{n.label}
            {n.view === 'tickets' && desk && <OverdueBadge desk={desk} />}
            {n.view === 'inbox' && desk && <Badge n={desk.counts.inboxProblems} />}
          </a>)}
      </nav>

      <main>
        {desk ? <Current desk={desk} />
          : <p className="loading">{error ? `Could not load the desk: ${error.message}` : 'Loading the desk'}</p>}
      </main>
    </div>
  );
}
