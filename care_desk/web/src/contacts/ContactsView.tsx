import { createColumnHelper, flexRender, getCoreRowModel, getSortedRowModel, useReactTable, type RowSelectionState, type SortingState } from '@tanstack/react-table';
import { useEffect, useMemo, useState } from 'react';
import { fmtAgo, fmtClock, fmtDateTime, useNow } from '../format';
import { Icon, type IconName } from '../icons';
import type { Desk, Id, Lead } from '../types';
import { Tags, Tick } from '../ui';
import { BulkModal, type BulkKind } from './BulkModal';
import { ContactDrawer } from './ContactDrawer';
import { ImportModal } from './ImportModal';

// Journey status, shown in the contact's panel.
export const STATUS: Record<string, [string, string]> = {
  active: ['g', 'In journey'], queued: ['a', 'Waiting on approval'], manual: ['o', 'No email · follow up by DM'],
  replied: ['w', 'Replied · follow up'], won: ['g', 'Won'], completed: ['o', 'Journey finished'],
  closed: ['o', 'Closed'], unsubscribed: ['o', 'Unsubscribed'], saved: ['o', 'No journey'],
};
export const statusOf = (l: Lead) => STATUS[l.status] || ['o', l.status];

// new: asked about a service (new contact journey). old: imported (reactivation). general: anyone else.
export const KIND: Record<Lead['kind'], [string, string, string]> = {
  new: ['w', 'New', 'Asked about a service; in the new contact journey'],
  old: ['a', 'Old', 'Imported past inquiry; in the reactivation journey'],
  general: ['o', 'Other', 'Wrote in about something else; no journey'],
};

// Each row carries its last activity, so that column sorts like the others.
type Activity = { at: number; label: string };
type Row = Lead & { last?: Activity };
const col = createColumnHelper<Row>();

// What the cells need besides the row. Passed as table meta so the columns stay the same
// objects: new column definitions would rebuild every cell, and a button could vanish mid-click.
type Meta = { now: number };
const metaOf = (t: { options: { meta?: unknown } }) => t.options.meta as Meta;
const muted = (text: string) => <span style={{ color: 'var(--t-lo)' }}>{text}</span>;

/** The latest thing that happened with each contact: a message from them, or anything in their activity. */
function lastActivity(desk: Desk): Map<Id, Activity> {
  const last = new Map<Id, Activity>();
  // At the same moment, what comes later in this list wins: a message says more than "Contact saved".
  const keep = (id: Id, a: Activity) => { if (a.at >= (last.get(id)?.at ?? 0)) last.set(id, a); };
  desk.leads.forEach(l => keep(l.id, { at: l.created_at, label: 'Added' }));
  desk.eventsByLead.forEach((events, id) => events.forEach(e => keep(id, { at: e.at, label: e.label })));
  desk.messages.forEach(m => { if (m.lead_id != null) keep(m.lead_id, { at: m.received_at, label: m.category || 'Message' }); });
  return last;
}

const COLUMNS = [
  col.display({
    id: 'pick',
    header: ({ table }) => <Tick label="Select all contacts" checked={table.getIsAllRowsSelected()}
      some={table.getIsSomeRowsSelected()} onChange={table.getToggleAllRowsSelectedHandler() as () => void} />,
    cell: ({ row }) => <Tick label={`Select ${row.original.name}`} checked={row.getIsSelected()}
      onChange={row.getToggleSelectedHandler() as () => void} />,
  }),
  // Text sorts A to Z on the first click, dates newest first. Empty phones, emails and tags
  // sort last whichever way the column is sorted.
  col.accessor('name', { header: 'Name', sortingFn: 'text', sortDescFirst: false, cell: ({ row: { original: l } }) => <b>{l.name}</b> }),
  col.accessor(l => l.phone || undefined, {
    id: 'phone', header: 'Phone', sortDescFirst: false, sortUndefined: 'last', cell: ({ row: { original: l } }) => l.phone || muted('None'),
  }),
  col.accessor(l => l.email || undefined, {
    id: 'email', header: 'Email', sortingFn: 'text', sortDescFirst: false, sortUndefined: 'last', cell: ({ row: { original: l } }) => l.email || muted('None'),
  }),
  col.accessor('source', { header: 'Source', sortingFn: 'text', sortDescFirst: false }),
  col.accessor('created_at', {
    header: 'Created', sortDescFirst: true,
    cell: ({ row: { original: l } }) => <span className="num">{fmtDateTime(l.created_at)}</span>,
  }),
  col.accessor(l => l.last?.at, {
    id: 'last', header: 'Last activity', sortDescFirst: true, sortUndefined: 'last',
    cell: ({ row: { original: { last: a } }, table }) =>
      a ? <span title={fmtClock(a.at)}>{fmtAgo(a.at, metaOf(table).now)}<span className="sub">{a.label}</span></span> : muted('None'),
  }),
  col.accessor(l => l.tags?.length ? l.tags.join(', ') : undefined, {
    id: 'tags', header: 'Tags', sortingFn: 'text', sortDescFirst: false, sortUndefined: 'last',
    cell: ({ row: { original: l } }) => l.tags?.length ? <Tags tags={l.tags} /> : muted('None'),
  }),
];

const SORT_KEY = 'care-desk.contacts.sort';
const DEFAULT_SORT: SortingState = [{ id: 'created_at', desc: true }];

export function ContactsView({ desk }: { desk: Desk }) {
  const [picked, setPicked] = useState<RowSelectionState>({});
  const [open, setOpen] = useState<Id | null>(null);
  const [modal, setModal] = useState<BulkKind | 'import' | null>(null);
  const now = useNow(15_000);
  const list = useMemo<Row[]>(() => {
    const last = lastActivity(desk);
    return desk.leads.map(l => ({ ...l, last: last.get(l.id) }));
  }, [desk]);
  // Newest first until someone picks another order; their pick is remembered in this browser.
  const [sorting, setSorting] = useState<SortingState>(() => {
    try { return JSON.parse(localStorage.getItem(SORT_KEY) || '') as SortingState; } catch { return DEFAULT_SORT; }
  });
  useEffect(() => { try { localStorage.setItem(SORT_KEY, JSON.stringify(sorting)); } catch { /* private window */ } }, [sorting]);
  const isAdmin = ['owner', 'admin'].includes(desk.user.role);

  // Contacts deleted elsewhere drop out of the selection.
  useEffect(() => {
    setPicked(p => {
      const keep = Object.fromEntries(Object.entries(p).filter(([k]) => desk.leadById.has(+k)));
      return Object.keys(keep).length === Object.keys(p).length ? p : keep;
    });
  }, [desk.leadById]);

  const table = useReactTable({
    data: list,
    columns: COLUMNS,
    meta: { now } satisfies Meta,
    getRowId: l => String(l.id),
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    enableSortingRemoval: false,  // a header click only flips the order; there is always one
    state: { rowSelection: picked, sorting },
    onSortingChange: setSorting,
    onRowSelectionChange: setPicked,
    enableRowSelection: true,
  });

  const pickedLeads = Object.keys(picked).filter(k => picked[k]).map(k => desk.leadById.get(+k)).filter((l): l is Lead => !!l);
  const n = pickedLeads.length;
  const bulk = (v: BulkKind, icon: IconName, label: string) =>
    <button className="btn sm ghost" onClick={() => setModal(v)}><Icon name={icon} size={14} />{label}</button>;
  const lead = open != null ? desk.leadById.get(open) : undefined;

  return <>
    <div className="vh">
      <div><div className="eyebrow">04 · People</div><h1>Contacts</h1></div>
      <p>Everyone who writes in is saved here once: the same email, GHL contact or name is one contact. Open a contact to see everything they sent and, for sales contacts, their follow-up journey.</p>
      <div className="right"><button className="btn" onClick={() => setModal('import')}><Icon name="upload" size={16} />Import old contacts</button></div>
    </div>

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
        {g.headers.map(h => {
          const sorted = h.column.getIsSorted();
          return <th key={h.id} className={h.id === 'pick' ? 'pick' : undefined}
            aria-sort={sorted === 'asc' ? 'ascending' : sorted === 'desc' ? 'descending' : undefined}
            onClick={h.id === 'pick' ? () => table.toggleAllRowsSelected(!table.getIsAllRowsSelected()) : undefined}>
            {h.column.getCanSort()
              ? <button className="sortby" onClick={h.column.getToggleSortingHandler()}
                  title={`Sort by ${String(h.column.columnDef.header).toLowerCase()}`}>
                  {flexRender(h.column.columnDef.header, h.getContext())}
                  <span aria-hidden="true">{sorted === 'asc' ? '↑' : sorted === 'desc' ? '↓' : '↕'}</span>
                </button>
              : flexRender(h.column.columnDef.header, h.getContext())}
          </th>;
        })}
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
        {!list.length && <tr><td colSpan={COLUMNS.length} className="empty">No contacts yet. Everyone who writes in is saved here.</td></tr>}
      </tbody>
    </table></div>
    {list.length > 0 && <p className="note">{list.length} {list.length === 1 ? 'contact' : 'contacts'}</p>}

    {lead && <ContactDrawer desk={desk} lead={lead} onClose={() => setOpen(null)} />}
    {modal === 'import' && <ImportModal onClose={() => setModal(null)} onDone={() => setModal(null)} />}
    {modal && modal !== 'import' && n > 0 && <BulkModal kind={modal} desk={desk} people={pickedLeads}
      onClose={() => setModal(null)} onDone={deleted => { setModal(null); if (deleted) setPicked({}); }} />}
  </>;
}
