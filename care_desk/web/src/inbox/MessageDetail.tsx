import { useAction } from '../desk';
import { channelOf, fmtTime, initials, nameOf, useNow } from '../format';
import { Icon } from '../icons';
import type { Desk, Message } from '../types';
import type { Open } from './InboxView';
import { ReplyBlock } from './ReplyBlock';

const INTENT: Record<string, string> = { sales: 'Sales inquiry', service: 'Concern', other: 'No action needed' };

export function CategoryChip({ m }: { m: Message }) {
  if (m.sort_status === 'unparsed') return <span className="chip a"><Icon name="alert" size={12} stroke={2.4} />Could not read</span>;
  if (m.sort_status === 'failed') return <span className="chip r"><Icon name="alert" size={12} stroke={2.4} />Not sorted</span>;
  if (m.sort_status !== 'sorted') return <i className="sorting" aria-label="Sorting" />;
  if (m.intent === 'service' && m.priority === 'Urgent') return <span className="chip u"><Icon name="alert" size={12} stroke={2.4} />{m.category}</span>;
  return <span className={`chip ${m.intent === 'other' ? 'o' : 'w'}`}>{m.category}</span>;
}

export function RouteLabel({ desk, m }: { desk: Desk; m: Message }) {
  if (m.route === 'ticket') return <><Icon name="ticket" size={13} />Ticket {m.ticket_id != null ? desk.ticketById.get(m.ticket_id)?.code : ''}</>;
  if (m.route === 'lead') return <><Icon name="users" size={13} />Contact</>;
  if (m.route === 'logged') return <><Icon name="check" size={13} />Logged</>;
  return null;
}

export function MessageDetail({ desk, m, open }: { desk: Desk; m: Message; open: Open }) {
  const now = useNow(60_000);
  const act = useAction();
  const c = channelOf(m.channel);
  const t = m.ticket_id != null ? desk.ticketById.get(m.ticket_id) : undefined;
  const l = m.lead_id != null ? desk.leadById.get(m.lead_id) : undefined;
  const post = (action: string, done?: string) => act(`/api/desk/messages/${m.id}/${action}`, {}, done);

  const head = <div className="dh">
    <span className="av">{initials(m.author)}<span className="ch"><Icon name={c.icon} size={11} stroke={2.2} /></span></span>
    <div><b>{m.author}</b><small>
      {c.label} {c.kind.toLowerCase()} · {fmtTime(m.received_at, now)}{m.email ? ` · ${m.email}` : ''}{m.source === 'manual' ? ' · added by hand' : ''}
    </small></div>
  </div>;

  if (m.sort_status === 'unparsed') {
    return <aside className="card detail" aria-label="Message detail">{head}
      <p>GHL sent this webhook, but it had no comment text the desk could find. In the GHL workflow, add a Custom Data item named <code>comment</code> holding the comment text.</p>
      <div className="eyebrow" style={{ marginTop: 14 }}>What GHL sent</div>
      <pre className="raw">{JSON.stringify(m.raw, null, 2)}</pre>
    </aside>;
  }

  let body;
  if (m.sort_status === 'sorted') {
    const pct = Math.round((m.confidence || 0) * 100);
    body = <>
      <dl className="kv">
        <dt>Issue type</dt><dd><CategoryChip m={m} /></dd>
        <dt>Intent</dt><dd>{INTENT[m.intent || ''] || m.intent}</dd>
        {m.intent === 'service' && <><dt>Priority</dt><dd>
          <span className={`chip ${m.priority === 'Urgent' ? 'u' : m.priority === 'High' ? 'a' : 'o'}`}>{m.priority}</span></dd></>}
        {m.interest && <><dt>Interested in</dt><dd>{m.interest}</dd></>}
        <dt>Summary</dt><dd style={{ fontWeight: 500 }}>{m.summary}</dd>
        <dt>Confidence</dt><dd className="conf"><i><b style={{ width: `${pct}%` }} /></i><span className="num">{pct}%</span>
          {m.sorted_by === 'rules' && <span className="chip a">Keyword rules</span>}</dd>
        <dt>Routed to</dt><dd>
          {t ? <button className="btn sm" onClick={() => open('ticket', t.id)}><Icon name="ticket" size={14} />{t.code} · {t.owner ? nameOf(t.owner, desk.people) : t.team}</button>
            : l ? <button className="btn sm" onClick={() => open('lead', l.id)}><Icon name="users" size={14} />{l.name}</button>
            : m.route === 'logged' ? 'Logged, nothing to do' : 'Not routed'}
        </dd>
      </dl>
      <ReplyBlock desk={desk} m={m} />
      <div className="acts">
        {!t && <button className="btn sm" onClick={() => post('ticket', 'Ticket created')}><Icon name="ticket" size={14} />Make a ticket</button>}
        {!l && m.intent !== 'service' && <button className="btn sm" onClick={() => post('lead', 'Added as a contact')}><Icon name="users" size={14} />Add as contact</button>}
      </div>
    </>;
  } else if (m.sort_status === 'failed') {
    body = <>
      <p className="err">{m.sort_error || 'Sorting failed.'}</p>
      <div className="acts"><button className="btn sm primary" onClick={() => post('resort', 'Sorting again')}><Icon name="refresh" size={14} />Sort again</button></div>
      <p className="note">Failed sorts are retried on their own up to three times.</p>
    </>;
  } else {
    body = <span className="sorting" style={{ width: '100%', height: 120, borderRadius: 12 }} />;
  }

  return <aside className="card detail" aria-label="Message detail">{head}
    <div className="quote">{m.subject && <div className="qs">{m.subject}</div>}{m.text}</div>
    <div className="ai"><div className="eyebrow" style={{ marginBottom: 10 }}>How it was sorted</div>{body}</div>
  </aside>;
}
