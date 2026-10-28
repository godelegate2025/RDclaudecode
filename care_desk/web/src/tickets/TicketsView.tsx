import { createColumnHelper, flexRender, getCoreRowModel, useReactTable } from '@tanstack/react-table';
import { useMemo, useState } from 'react';
import { channelLabel, fmtDur, median, MIN, nameOf, useNow } from '../format';
import { Icon } from '../icons';
import type { Desk, Id, Ticket } from '../types';
import { FirstReplyClock, isOverdue, ResolutionClock } from './sla';
import { TicketDrawer } from './TicketDrawer';

export const PRIORITY = { Urgent: 'u', High: 'a', Normal: 'o' } as const;
export const STATUS = { Open: 'o', 'In progress': 'a', Resolved: 'g' } as const;

type Filter = 'open' | 'mine' | 'overdue' | 'resolved' | 'all';
const FILTERS: [Filter, string][] = [['open', 'Open'], ['mine', 'Mine'], ['overdue', 'Past target'], ['resolved', 'Resolved'], ['all', 'All']];

// As in Contacts, what the cells need goes in as table meta, so the columns never change.
type Meta = { desk: Desk };
const deskOf = (t: { options: { meta?: unknown } }) => (t.options.meta as Meta).desk;

const col = createColumnHelper<Ticket>();
const COLUMNS = [
  col.accessor('code', {
    header: 'Ticket',
    cell: ({ row: { original: t } }) => <>
      <span className="id">{t.code}</span>
      <span className="sub"><span className={`chip ${PRIORITY[t.priority] || 'o'}`} style={{ height: 22, marginTop: 4 }}>{t.priority}</span></span>
      {t.escalated_at && t.status !== 'Resolved' && <span className="esc"><Icon name="up" size={12} stroke={2.6} />Escalated</span>}
    </>,
  }),
  col.accessor('customer', {
    header: 'Customer',
    cell: ({ row: { original: t } }) => <>{t.customer}<span className="sub">{channelLabel(t.channel)}</span></>,
  }),
  col.accessor('category', { header: 'Issue' }),
  col.accessor('team', {
    header: 'Team · Owner',
    cell: ({ row: { original: t }, table }) => <>{t.team}<span className="sub">{t.owner ? nameOf(t.owner, deskOf(table).people) : 'Unassigned'}</span></>,
  }),
  col.accessor('status', {
    header: 'Status',
    cell: ({ row: { original: t } }) => <span className={`chip ${STATUS[t.status] || 'o'}`}>{t.status}</span>,
  }),
  col.display({ id: 'first', header: 'First reply', cell: ({ row, table }) => <FirstReplyClock desk={deskOf(table)} t={row.original} /> }),
  col.display({ id: 'resolve', header: 'Resolution', cell: ({ row, table }) => <ResolutionClock desk={deskOf(table)} t={row.original} /> }),
];

export function TicketsView({ desk }: { desk: Desk }) {
  const [filter, setFilter] = useState<Filter>('open');
  const [open, setOpen] = useState<Id | null>(null);
  const now = useNow(60_000);  // which tickets are past target moves by the minute

  const test = useMemo(() => ({
    open: (t: Ticket) => t.status !== 'Resolved',
    mine: (t: Ticket) => t.owner === desk.user.email && t.status !== 'Resolved',
    overdue: (t: Ticket) => isOverdue(desk, t, now),
    resolved: (t: Ticket) => t.status === 'Resolved',
    all: () => true,
  }), [desk, now]);
  // Past target first, then newest.
  const list = useMemo(() => desk.tickets.filter(test[filter])
    .sort((a, b) => (+test.overdue(b) - +test.overdue(a)) || b.created_at - a.created_at), [desk.tickets, test, filter]);

  const table = useReactTable({ data: list, columns: COLUMNS, getRowId: t => String(t.id), getCoreRowModel: getCoreRowModel(), meta: { desk } satisfies Meta });

  const openOnes = desk.tickets.filter(test.open), over = openOnes.filter(test.overdue);
  const resolved = desk.tickets.filter(test.resolved);
  const mFirst = median(desk.tickets.filter(t => t.first_reply_at).map(t => t.first_reply_at! - t.created_at));
  const mRes = median(resolved.map(t => t.resolved_at! - t.created_at));
  const sla = desk.config.sla;
  const ticket = open != null ? desk.ticketById.get(open) : undefined;

  return <>
    <div className="vh">
      <div><div className="eyebrow">03 · Support tickets</div><h1>Tickets</h1></div>
      <p>Every concern gets a team, an owner and two clocks: time to first reply and time to resolution. Anything past its first reply target is escalated.</p>
    </div>

    <div className="tiles">
      <div className="card tile"><div className="k">Open</div><div className="v num">{openOnes.length}</div>
        <div className="s">{openOnes.filter(t => t.priority === 'Urgent').length} urgent</div></div>
      <div className={`card tile${over.length ? ' alert' : ''}`}><div className="k">Past target</div><div className="v num">{over.length}</div>
        <div className="s">{openOnes.filter(t => t.escalated_at).length} escalated</div></div>
      <div className="card tile"><div className="k">Median first reply</div><div className="v num">{mFirst != null ? fmtDur(mFirst) : 'None'}</div>
        <div className="s">Across all tickets</div></div>
      <div className="card tile"><div className="k">Median resolution</div><div className="v num">{mRes != null ? fmtDur(mRes) : 'None'}</div>
        <div className="s">{resolved.length} resolved</div></div>
    </div>

    <div className="filters" role="group" aria-label="Ticket filter">
      {FILTERS.map(([k, label]) => <button key={k} className="fbtn" aria-pressed={filter === k} onClick={() => setFilter(k)}>
        {label}<span className="n num">{desk.tickets.filter(test[k]).length}</span></button>)}
    </div>

    <div className="tablewrap"><table>
      <thead>{table.getHeaderGroups().map(g => <tr key={g.id}>
        {g.headers.map(h => <th key={h.id}>{flexRender(h.column.columnDef.header, h.getContext())}</th>)}
      </tr>)}</thead>
      <tbody>
        {table.getRowModel().rows.map(row => <tr key={row.id} tabIndex={0} data-act="open"
          onClick={() => setOpen(row.original.id)}
          onKeyDown={e => { if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) { e.preventDefault(); setOpen(row.original.id); } }}>
          {row.getVisibleCells().map(c => <td key={c.id}>{flexRender(c.column.columnDef.cell, c.getContext())}</td>)}
        </tr>)}
        {!list.length && <tr><td colSpan={COLUMNS.length} className="empty">No tickets here.</td></tr>}
      </tbody>
    </table></div>
    <p className="note">Targets: urgent {fmtDur(sla.Urgent.first * MIN)} first reply and {fmtDur(sla.Urgent.resolve * MIN)} resolution,
      high {fmtDur(sla.High.first * MIN)} and {fmtDur(sla.High.resolve * MIN)}, normal {fmtDur(sla.Normal.first * MIN)} and {fmtDur(sla.Normal.resolve * MIN)}.</p>

    {ticket && <TicketDrawer desk={desk} ticket={ticket} onClose={() => setOpen(null)} />}
  </>;
}
