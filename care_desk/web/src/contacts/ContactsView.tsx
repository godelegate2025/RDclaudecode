import { createColumnHelper, flexRender, getCoreRowModel, useReactTable, type RowSelectionState } from '@tanstack/react-table';
import { useEffect, useMemo, useState } from 'react';
import { useAction } from '../desk';
import { fill, firstName, fmtAgo, fmtIn, useNow } from '../format';
import { Icon, type IconName } from '../icons';
import type { Desk, Id, Journey, Lead } from '../types';
import { Banner, Tags, Tick } from '../ui';
import { BulkModal, type BulkKind } from './BulkModal';
import { ContactDrawer } from './ContactDrawer';
import { ImportModal } from './ImportModal';

export const STATUS: Record<string, [string, string]> = {
  active: ['g', 'In journey'], queued: ['a', 'Waiting on approval'], manual: ['o', 'No email · follow up by DM'],
  replied: ['w', 'Replied · follow up'], won: ['g', 'Won'], completed: ['o', 'Journey finished'],
  closed: ['o', 'Closed'], unsubscribed: ['o', 'Unsubscribed'],
};
export const statusOf = (l: Lead) => STATUS[l.status] || ['o', l.status];

const DONE: Record<string, string> = { replied: 'Marked replied. No more journey emails.', won: 'Marked won', closed: 'Contact closed' };

const col = createColumnHelper<Lead>();

// What the cells need besides the row. Passed as table meta so the columns stay the same
// objects: new column definitions would rebuild every cell, and a button could vanish mid-click.
type Meta = { now: number; kind: string; journey?: Journey; signOff: string; setStatus: (l: Lead, status: string) => void };
const metaOf = (t: { options: { meta?: unknown } }) => t.options.meta as Meta;

const COLUMNS = [
  col.display({
    id: 'pick',
    header: ({ table }) => <Tick label={`Select all ${metaOf(table).kind} contacts`} checked={table.getIsAllRowsSelected()}
      some={table.getIsSomeRowsSelected()} onChange={table.getToggleAllRowsSelectedHandler() as () => void} />,
    cell: ({ row }) => <Tick label={`Select ${row.original.name}`} checked={row.getIsSelected()}
      onChange={row.getToggleSelectedHandler() as () => void} />,
  }),
  col.accessor('name', {
    header: 'Contact',
    cell: ({ row: { original: l } }) => <>
      <b>{l.name}</b>
      <span className="sub">{[l.email || 'No email', l.phone].filter(Boolean).join(' · ')}</span>
      <Tags tags={l.tags} />
    </>,
  }),
  col.accessor('interest', { header: 'Interest' }),
  col.accessor('source', {
    header: 'Source',
    cell: ({ row: { original: l }, table }) => <>{l.source}<span className="sub">
      {l.kind === 'old' && l.last_contact_days != null ? `Last contact ${l.last_contact_days} days ago` : fmtAgo(l.created_at, metaOf(table).now)}
    </span></>,
  }),
  col.display({
    id: 'journey',
    header: 'Journey',
    cell: ({ row: { original: l }, table }) => {
      const steps = metaOf(table).journey?.steps.length ?? 0, sent = Math.min(l.step, steps);
      return <>
        <span className="steps" aria-label={`${sent} of ${steps} emails sent`}>
          {Array.from({ length: steps }, (_, k) =>
            <i key={k} className={k < l.step ? 'done' : k === l.step && l.status === 'active' ? 'next' : ''} />)}
        </span>
        <span className="sub">{sent} of {steps} sent</span>
      </>;
    },
  }),
  col.display({
    id: 'next',
    header: 'Next email',
    cell: ({ row: { original: l }, table }) => {
      const { journey, now, signOff } = metaOf(table), next = journey?.steps[l.step];
      if (l.status !== 'active' || l.next_at == null || !next) return <span style={{ color: 'var(--t-lo)' }}>None</span>;
      return <span className="num">{fmtIn(l.next_at, now)}<span className="sub">
        {fill(next.subject, { first_name: firstName(l.name), interest: String(l.interest).toLowerCase(), sign_off: signOff })}
      </span></span>;
    },
  }),
  col.accessor('status', {
    header: 'Status',
    cell: ({ row: { original: l } }) => { const [c, label] = statusOf(l); return <span className={`chip ${c}`}>{label}</span>; },
  }),
  col.display({
    id: 'acts',
    header: () => <span className="sr">Actions</span>,
    cell: ({ row: { original: l }, table }) => {
      const { setStatus } = metaOf(table);
      return <div className="rowacts" onClick={e => e.stopPropagation()}>
        {['active', 'queued', 'manual'].includes(l.status) && <button className="btn sm" onClick={() => setStatus(l, 'replied')}>Replied</button>}
        {!['won', 'unsubscribed'].includes(l.status) && <button className="btn sm" onClick={() => setStatus(l, 'won')}>Won</button>}
        {!['closed', 'won', 'unsubscribed', 'completed'].includes(l.status) && <button className="btn sm ghost" onClick={() => setStatus(l, 'closed')}>Close</button>}
      </div>;
    },
  }),
];

export function ContactsView({ desk }: { desk: Desk }) {
  const [kind, setKind] = useState<'new' | 'old'>('new');
  const [picked, setPicked] = useState<RowSelectionState>({});
  const [open, setOpen] = useState<Id | null>(null);
  const [modal, setModal] = useState<BulkKind | 'import' | null>(null);
  const act = useAction();
  const now = useNow(15_000);

  const list = useMemo(() => desk.leads.filter(l => l.kind === kind), [desk.leads, kind]);
  const journey: Journey | undefined = desk.journeys[kind === 'old' ? 'oldlead' : 'newlead'];
  const isAdmin = ['owner', 'admin'].includes(desk.user.role);

  // Contacts deleted elsewhere drop out of the selection.
  useEffect(() => {
    setPicked(p => {
      const keep = Object.fromEntries(Object.entries(p).filter(([k]) => desk.leadById.has(+k)));
      return Object.keys(keep).length === Object.keys(p).length ? p : keep;
    });
  }, [desk.leadById]);

  const setStatus = (l: Lead, status: string) => act(`/api/desk/leads/${l.id}/${status}`, {}, DONE[status]);

  const table = useReactTable({
    data: list,
    columns: COLUMNS,
    meta: { now, kind, journey, signOff: desk.config.sign_off, setStatus } satisfies Meta,
    getRowId: l => String(l.id),
    getCoreRowModel: getCoreRowModel(),
    state: { rowSelection: picked },
    onRowSelectionChange: setPicked,
    enableRowSelection: true,
  });

  const pickedLeads = Object.keys(picked).filter(k => picked[k]).map(k => desk.leadById.get(+k)).filter((l): l is Lead => !!l);
  const n = pickedLeads.length;
  const count = (k: string) => desk.leads.filter(l => l.kind === k).length;
  const showTab = (k: 'new' | 'old') => { setKind(k); setPicked({}); };
  const bulk = (v: BulkKind, icon: IconName, label: string) =>
    <button className="btn sm ghost" onClick={() => setModal(v)}><Icon name={icon} size={14} />{label}</button>;
  const lead = open != null ? desk.leadById.get(open) : undefined;

  return <>
    <div className="vh">
      <div><div className="eyebrow">04 · Sales follow-up</div><h1>Contacts</h1></div>
      <p>Questions about services and prices become contacts. New contacts get a fast answer and a short follow-up. Old inquiries you import get a reactivation sequence.</p>
      <div className="right"><button className="btn" onClick={() => setModal('import')}><Icon name="upload" size={16} />Import old contacts</button></div>
    </div>

    <div className="tabs" role="tablist">
      {(['new', 'old'] as const).map(k =>
        <button key={k} className="fbtn" role="tab" aria-pressed={kind === k} aria-selected={kind === k} onClick={() => showTab(k)}>
          {k === 'new' ? 'New contacts' : 'Old contacts'}<span className="n num">{count(k)}</span>
        </button>)}
    </div>

    {journey && journey.status !== 'live' && <Banner icon="shield">
      <span>The {journey.name} is {journey.status === 'paused' ? 'paused' : 'waiting for approval'}. Contacts with an email are queued and nothing is sent until it goes live.</span>
      <a className="btn sm" href={`#email/${journey.key}`}>Review the journey</a>
    </Banner>}
    {!desk.status.email && <Banner icon="mail">
      <span>No email service is set up, so journeys are on hold. Set <code>RESEND_API_KEY</code> and <code>DESK_FROM_EMAIL</code> on the server.</span>
    </Banner>}

    {n > 0 && <div className="selbar" role="toolbar" aria-label="Selected contacts">
      <span className="count">Selected: {n}
        {n < list.length && <button onClick={() => table.toggleAllRowsSelected(true)}>Select all: {list.length}</button>}
        <button onClick={() => setPicked({})}>Clear</button>
      </span>
      <span className="sep" aria-hidden="true" />
      {bulk('sms', 'dm', 'Send SMS')}{bulk('email', 'mail', 'Send email')}{bulk('whatsapp', 'phone', 'Send WhatsApp')}
      <span className="sep" aria-hidden="true" />
      {bulk('tag', 'tag', 'Add tags')}{bulk('untag', 'tag', 'Remove tags')}
      {isAdmin && <><span className="sep" aria-hidden="true" />{bulk('delete', 'trash', 'Delete')}</>}
    </div>}

    <div className="tablewrap"><table>
      <thead>{table.getHeaderGroups().map(g => <tr key={g.id}>
        {g.headers.map(h => <th key={h.id} className={h.id === 'pick' ? 'pick' : undefined}
          onClick={h.id === 'pick' ? () => table.toggleAllRowsSelected(!table.getIsAllRowsSelected()) : undefined}>
          {flexRender(h.column.columnDef.header, h.getContext())}
        </th>)}
      </tr>)}</thead>
      <tbody>
        {table.getRowModel().rows.map(row => <tr key={row.id} tabIndex={0} data-act="open"
          className={row.getIsSelected() ? 'picked' : undefined}
          onClick={() => setOpen(row.original.id)}
          onKeyDown={e => { if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) { e.preventDefault(); setOpen(row.original.id); } }}>
          {row.getVisibleCells().map(c => <td key={c.id} className={c.column.id === 'pick' ? 'pick' : undefined}
            onClick={c.column.id === 'pick' ? e => { e.stopPropagation(); row.toggleSelected(); } : undefined}>
            {flexRender(c.column.columnDef.cell, c.getContext())}
          </td>)}
        </tr>)}
        {!list.length && <tr><td colSpan={COLUMNS.length} className="empty">
          {kind === 'old' ? 'No old contacts yet. Import a list of past inquiries to start.' : 'No contacts yet.'}
        </td></tr>}
      </tbody>
    </table></div>

    {lead && <ContactDrawer desk={desk} lead={lead} onClose={() => setOpen(null)} />}
    {modal === 'import' && <ImportModal onClose={() => setModal(null)} onDone={() => { setModal(null); showTab('old'); }} />}
    {modal && modal !== 'import' && n > 0 && <BulkModal kind={modal} desk={desk} people={pickedLeads}
      onClose={() => setModal(null)} onDone={deleted => { setModal(null); if (deleted) setPicked({}); }} />}
  </>;
}
