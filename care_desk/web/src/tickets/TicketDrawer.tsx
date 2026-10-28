import { useState } from 'react';
import { useAction } from '../desk';
import { channelLabel, fmtDur, fmtTime, nameOf, useNow } from '../format';
import { Icon } from '../icons';
import type { Desk, Message, Ticket } from '../types';
import { Activity, Drawer } from '../ui';
import { FirstReplyClock, ResolutionClock } from './sla';
import { TicketNotes } from './TicketNotes';

const STEPS = ['Open', 'Replied', 'In progress', 'Resolved'];
const DONE: Record<string, string> = {
  assign: 'Assigned to you', first_reply: 'First reply recorded', progress: 'Moved to In progress', resolve: 'Resolved', reopen: 'Reopened',
};

// The comment on its post when GHL sent the link, otherwise GHL's comment inbox.
function ghlLink(desk: Desk, m?: Message) {
  if (m?.post_url) return m.post_url;
  const loc = desk.status.ghl_location;
  return loc ? `https://app.gohighlevel.com/v2/location/${encodeURIComponent(loc)}/marketing/social-planner/comments` : null;
}

const lo = (text: string) => <span style={{ color: 'var(--t-lo)' }}>{text}</span>;

export function TicketDrawer({ desk, ticket: t, onClose }: { desk: Desk; ticket: Ticket; onClose: () => void }) {
  const now = useNow(60_000);
  const act = useAction();
  const [busy, setBusy] = useState(false);
  const m = t.message_id != null ? desk.messageById.get(t.message_id) : undefined;
  const link = ghlLink(desk, m);
  const at = t.status === 'Resolved' ? 3 : t.status === 'In progress' ? 2 : t.first_reply_at ? 1 : 0;
  const escalated = !!t.escalated_at && t.status !== 'Resolved';

  const run = async (action: string) => {
    setBusy(true);
    await act(`/api/desk/tickets/${t.id}/${action}`, {}, DONE[action]);
    setBusy(false);
  };
  const button = (action: string, label: string, icon: Parameters<typeof Icon>[0]['name'], primary = false) =>
    <button className={`btn${primary ? ' primary' : ''}`} disabled={busy} onClick={() => run(action)}><Icon name={icon} size={16} />{label}</button>;

  return (
    <Drawer label={`Ticket ${t.code}`} onClose={onClose}>
      <div className="eyebrow">{t.category} · {t.priority} priority</div>
      <h2>{t.code}</h2>
      <div className="track">{STEPS.map((s, k) => <span key={s} style={{ display: 'contents' }}>
        {k > 0 && <span className={`seg${k <= at ? ' on' : ''}`} />}
        <span className={`st${k <= at ? ' on' : ''}`}><i />{s}</span>
      </span>)}</div>

      {m && <div className="quote">
        {m.subject && <div className="qs">{m.subject}</div>}{m.text}
        <div className="note" style={{ marginTop: 6 }}>{t.customer} · {channelLabel(t.channel)} · {fmtTime(t.created_at, now)}</div>
      </div>}
      {m?.suggested_reply && !m.replied_at && <div className="draft"><span className="eyebrow">Suggested public reply</span>{m.suggested_reply}</div>}

      <div className="clocks">
        <div className="card"><div className="eyebrow">First reply</div><FirstReplyClock desk={desk} t={t} /></div>
        <div className="card"><div className="eyebrow">Resolution</div><ResolutionClock desk={desk} t={t} /></div>
      </div>

      <dl className="kv">
        <dt>Team</dt><dd>{t.team}{escalated && <> <span className="chip r">Escalated</span></>}</dd>
        <dt>Owner</dt><dd>{t.owner ? nameOf(t.owner, desk.people) : lo('Unassigned')}</dd>
        <dt>Customer email</dt><dd>{t.email || lo('Not on file. Update them in GHL instead')}</dd>
      </dl>

      <div className="acts">
        {t.owner !== desk.user.email && t.status !== 'Resolved' && button('assign', 'Assign to me', 'user')}
        {link && <a className="btn" href={link} target="_blank" rel="noopener noreferrer"><Icon name="ext" size={16} />Reply in GHL</a>}
        {!t.first_reply_at && button('first_reply', 'Mark first reply sent', 'send', true)}
        {t.status === 'Open' && button('progress', 'Mark in progress', 'arrow', !!t.first_reply_at)}
        {t.status !== 'Resolved'
          ? button('resolve', 'Resolve', 'check', t.status === 'In progress')
          : <>
            <span className="chip g"><Icon name="check" size={12} stroke={2.6} />Resolved in {fmtDur(t.resolved_at! - t.created_at)}</span>
            <button className="btn sm" disabled={busy} onClick={() => run('reopen')}>Reopen</button>
          </>}
      </div>

      <TicketNotes desk={desk} ticket={t} />
      <div className="sect"><h3>Activity</h3><Activity events={desk.eventsByTicket.get(t.id)} people={desk.people} /></div>
    </Drawer>
  );
}
