import { channelLabel, fmtClock, fmtTime, useNow } from '../format';
import type { Desk, Lead } from '../types';
import { Activity, Drawer, Tags } from '../ui';
import { statusOf } from './ContactsView';

const none = (text: string) => <span style={{ color: 'var(--t-lo)' }}>{text}</span>;

export function ContactDrawer({ desk, lead: l, onClose }: { desk: Desk; lead: Lead; onClose: () => void }) {
  const now = useNow(60_000);
  const m = l.message_id != null ? desk.messageById.get(l.message_id) : undefined;
  const [c, label] = statusOf(l);
  return (
    <Drawer label={`Contact ${l.name}`} onClose={onClose}>
      <div className="eyebrow">{l.kind === 'old' ? 'Old contact' : 'New contact'} · {l.interest}</div>
      <h2>{l.name}</h2>
      <div style={{ margin: '14px 0' }}><span className={`chip ${c}`}>{label}</span></div>
      {m && <div className="quote">{m.text}
        <div className="note" style={{ marginTop: 6 }}>{channelLabel(m.channel)} · {fmtTime(m.received_at, now)}</div>
      </div>}
      <dl className="kv" style={{ marginTop: 16 }}>
        <dt>Email</dt><dd>{l.email || none('None. Follow up by direct message')}</dd>
        <dt>Phone</dt><dd>{l.phone || none('None')}</dd>
        <dt>Tags</dt><dd>{l.tags?.length ? <Tags tags={l.tags} /> : none('None')}</dd>
        <dt>Source</dt><dd>{l.source}</dd>
        <dt>Added</dt><dd>{fmtClock(l.created_at)}</dd>
      </dl>
      <div className="sect"><h3>Activity</h3>
        <Activity events={desk.eventsByLead.get(l.id)} people={desk.people} />
      </div>
    </Drawer>
  );
}
