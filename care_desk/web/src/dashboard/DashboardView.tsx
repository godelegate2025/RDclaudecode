import { useState } from 'react';
import { CHANNELS, fmtDur, median, nameOf, useNow } from '../format';
import { isOverdue } from '../tickets/sla';
import { TicketDrawer } from '../tickets/TicketDrawer';
import type { Desk, Id } from '../types';

type Row = [string, number];

/** Horizontal bars; `hot` rows are drawn in coral. */
function Bars({ rows, show = String, hot, empty }: { rows: Row[]; show?: (v: number) => string; hot?: (k: string, i: number) => boolean; empty: string }) {
  if (!rows.length) return <p className="empty">{empty}</p>;
  const max = Math.max(1, ...rows.map(r => r[1]));
  return <>{rows.map(([k, v], i) => <div key={k} className={`hbar${hot?.(k, i) ? ' c' : ''}`}>
    <span>{k}</span><span className="b"><i style={{ width: `${v / max * 100}%` }} /></span><span className="n">{show(v)}</span>
  </div>)}</>;
}

export function DashboardView({ desk }: { desk: Desk }) {
  const now = useNow(15_000);
  const [open, setOpen] = useState<Id | null>(null);
  const { messages, tickets, leads } = desk;

  const cats: Record<string, number> = {};
  messages.forEach(m => { if (m.category) cats[m.category] = (cats[m.category] || 0) + 1; });
  const catRows = Object.entries(cats).sort((a, b) => b[1] - a[1]);
  const chRows = Object.keys(CHANNELS).map((c): Row => [CHANNELS[c].label, messages.filter(m => m.channel === c).length]).filter(r => r[1]);
  const teamRows = [...new Set(tickets.map(t => t.team))]
    .map(tm => [tm, median(tickets.filter(t => t.team === tm && t.resolved_at).map(t => t.resolved_at! - t.created_at))] as const)
    .filter((r): r is Row => r[1] != null).sort((a, b) => b[1] - a[1]);

  const openT = tickets.filter(t => t.status !== 'Resolved');
  const over = openT.filter(t => isOverdue(desk, t, now));
  const stuck = over.concat(openT.filter(t => !isOverdue(desk, t, now)).sort((a, b) => a.created_at - b.created_at)).slice(0, 5);
  const resolved = tickets.filter(t => t.resolved_at);
  const sent = desk.emails.filter(e => e.status === 'sent').length;
  const ticket = open != null ? desk.ticketById.get(open) : undefined;
  const count = (f: (l: Desk['leads'][number]) => boolean) => leads.filter(f).length;

  return <>
    <div className="vh">
      <div><div className="eyebrow">01 · Management view</div><h1>Dashboard</h1></div>
      <p>What people raise, how fast each team resolves it, what is stuck, and where contacts are. Covers the latest 400 messages.</p>
    </div>
    <div className="tiles">
      <div className="card tile"><div className="k">Messages sorted</div><div className="v num">{messages.filter(m => m.sort_status === 'sorted').length}</div>
        <div className="s">{messages.filter(m => m.route === 'ticket').length} tickets · {messages.filter(m => m.route === 'lead').length} contacts</div></div>
      <div className={`card tile ${over.length ? 'alert' : ''}`}><div className="k">Past target</div><div className="v num">{over.length}</div>
        <div className="s">of {openT.length} open tickets</div></div>
      <div className="card tile"><div className="k">Median resolution</div>
        <div className="v num">{resolved.length ? fmtDur(median(resolved.map(t => t.resolved_at! - t.created_at))!) : 'None'}</div>
        <div className="s">{resolved.length} resolved</div></div>
      <div className="card tile"><div className="k">Emails sent</div><div className="v num">{sent}</div><div className="s">Latest 150 emails</div></div>
    </div>
    <div className="dgrid">
      <section className="card panel"><h3>What people raise</h3><p className="ps">Messages by issue type</p>
        <Bars rows={catRows} hot={k => k === 'Urgent issue' || k === 'Complaint'} empty="Nothing yet." /></section>
      <section className="card panel"><h3>Where messages come from</h3><p className="ps">By channel</p>
        <Bars rows={chRows} empty="Nothing yet." /></section>
      <section className="card panel"><h3>Resolution time by team</h3><p className="ps">Median, resolved tickets. Slowest first</p>
        <Bars rows={teamRows} show={fmtDur} hot={(_, i) => i === 0 && teamRows.length > 1} empty="No resolved tickets yet." /></section>
      <section className="card panel"><h3>Stuck cases</h3><p className="ps">Past target first, then the oldest open</p>
        {stuck.map(t => { const late = isOverdue(desk, t, now); return <button key={t.id} className="stuck" onClick={() => setOpen(t.id)}>
          <span><b>{t.code} · {t.customer}</b><small>{t.category} · {t.owner ? nameOf(t.owner, desk.people) : t.team} · open {fmtDur(now - t.created_at)}</small></span>
          <span className={`chip ${late ? 'u' : 'o'}`}>{late ? 'Past target' : t.status}</span>
        </button>; })}
        {!stuck.length && <p className="empty">Nothing stuck.</p>}
      </section>
      <section className="card panel" style={{ gridColumn: '1/-1' }}><h3>Contacts in follow-up</h3><p className="ps">New and old contacts by stage</p>
        <div className="funnel">
          <div><b className="num">{count(l => l.kind === 'new' && ['active', 'queued', 'manual'].includes(l.status))}</b><span>New contacts open</span></div>
          <div><b className="num">{count(l => l.kind === 'old' && ['active', 'queued'].includes(l.status))}</b><span>Old contacts in journey</span></div>
          <div><b className="num">{count(l => l.status === 'replied')}</b><span>Replied, follow up</span></div>
          <div><b className="num">{count(l => l.status === 'won')}</b><span>Won</span></div>
        </div>
      </section>
    </div>

    {ticket && <TicketDrawer desk={desk} ticket={ticket} onClose={() => setOpen(null)} />}
  </>;
}
