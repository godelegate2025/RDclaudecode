import { useMemo, useState } from 'react';
import { CHANNELS, channelOf, fmtAgo, initials, useNow } from '../format';
import { Icon } from '../icons';
import { go } from '../nav';
import { TicketDrawer } from '../tickets/TicketDrawer';
import type { Desk, Id, Message } from '../types';
import { Banner } from '../ui';
import { AddMessage } from './AddMessage';
import { CategoryChip, MessageDetail, RouteLabel } from './MessageDetail';

export type Open = (kind: 'ticket' | 'lead', id: Id) => void;

export function InboxView({ desk }: { desk: Desk }) {
  const [ch, setCh] = useState('all');
  const [cat, setCat] = useState('all');
  const [sel, setSel] = useState<Id | null>(null);
  const [adding, setAdding] = useState(false);
  const [ticketId, setTicketId] = useState<Id | null>(null);
  const now = useNow(15_000);

  const counts = useMemo(() => {
    const n: Record<string, number> = { all: desk.messages.length };
    desk.messages.forEach(m => { n[m.channel] = (n[m.channel] || 0) + 1; });
    return n;
  }, [desk.messages]);
  const channels = ['all', ...Object.keys(CHANNELS).filter(c => counts[c])];
  const categories = ['all', ...new Set(desk.messages.filter(m => m.category).map(m => m.category!))];
  const list = desk.messages.filter(m => (ch === 'all' || m.channel === ch) && (cat === 'all' || m.category === cat));
  const shown: Message | undefined = (sel != null ? desk.messageById.get(sel) : undefined) || list[0];

  const pick = (id: Id) => {
    setSel(id);
    // On narrow screens the detail sits under the list, so bring it into view.
    if (innerWidth < 1100) setTimeout(() => document.querySelector('.detail')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 0);
  };
  // A ticket opens over the inbox; a contact on their own page.
  const open: Open = (kind, id) => (kind === 'ticket' ? setTicketId(id) : go(`/contacts/${id}`));
  const ticket = ticketId != null ? desk.ticketById.get(ticketId) : undefined;

  return <>
    <div className="vh">
      <div><div className="eyebrow">02 · Comment intelligence</div><h1>Inbox</h1></div>
      <p>Every comment, message and email from Redefine's GHL lands here and is sorted by issue type. Concerns become tickets. Questions about services and prices become contacts.</p>
    </div>
    {!desk.status.webhook && <Banner>
      <span>The GHL webhook is not switched on yet, so only messages you add by hand arrive. Set <code>DESK_WEBHOOK_KEY</code> on the server and add the workflow in GHL.</span>
    </Banner>}

    {adding
      ? <AddMessage onClose={() => setAdding(false)} onAdded={id => { setSel(id); setCh('all'); setCat('all'); }} />
      : <button className="btn tryopen" aria-expanded="false" onClick={() => setAdding(true)}><Icon name="plus" size={15} />Add a message by hand</button>}

    <div className="inbox">
      <div style={{ minWidth: 0 }}>
        <div className="filters" role="group" aria-label="Channel">
          {channels.map(c => <button key={c} className="fbtn" aria-pressed={ch === c} onClick={() => setCh(c)}>
            {c === 'all' ? 'All channels' : <><Icon name={channelOf(c).icon} size={13} />{channelOf(c).label}</>}
            <span className="n num">{counts[c]}</span>
          </button>)}
        </div>
        {categories.length > 1 && <div className="filters" role="group" aria-label="Issue type">
          {categories.map(c => <button key={c} className="fbtn" aria-pressed={cat === c} onClick={() => setCat(c)}>
            {c === 'all' ? 'All issue types' : c}
          </button>)}
        </div>}
        <div className="msgs" role="listbox" aria-label="Messages">
          {list.map(m => <button key={m.id} className="msg" role="option" aria-selected={shown?.id === m.id} onClick={() => pick(m.id)}>
            <span className="av">{initials(m.author)}<span className="ch"><Icon name={channelOf(m.channel).icon} size={11} stroke={2.2} /></span></span>
            <span style={{ minWidth: 0 }}>
              <span className="who">{m.author}<span>{channelOf(m.channel).label} · {fmtAgo(m.received_at, now)}</span></span>
              {m.subject && <span className="subj">{m.subject}</span>}
              <span className="txt">{m.text || <i>No text in this webhook</i>}</span>
            </span>
            <span className="tags"><CategoryChip m={m} /><span className="route"><RouteLabel desk={desk} m={m} /></span></span>
          </button>)}
          {!list.length && <div className="empty">{desk.messages.length ? 'Nothing matches these filters.'
            : 'No messages yet. Once the GHL workflow is on, new comments show up here within seconds.'}</div>}
        </div>
      </div>
      {/* The detail itself is the grid's second column, so it can stick while the list scrolls. */}
      {shown ? <MessageDetail key={shown.id} desk={desk} m={shown} open={open} /> : <div />}
    </div>

    {ticket && <TicketDrawer desk={desk} ticket={ticket} onClose={() => setTicketId(null)} />}
  </>;
}
