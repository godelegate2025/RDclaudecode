import { fmtClock, fmtTime, useNow } from '../format';
import type { Desk, Lead } from '../types';
import { Drawer, Tags } from '../ui';
import { statusOf } from './ContactsView';

const CHANNEL: Record<string, string> = {
  facebook: 'Facebook', instagram: 'Instagram', messenger: 'Messenger', tiktok: 'TikTok', linkedin: 'LinkedIn',
  threads: 'Threads', email: 'Email', website: 'Website',
};

// Activity details name people by email; show the name each gave, where there is one.
function withNames(text: string, people: Record<string, string>) {
  const emails = Object.keys(people);
  if (!emails.length) return text;
  const re = new RegExp(emails.map(e => e.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'gi');
  return text.replace(re, m => people[m.toLowerCase()] || m);
}

const none = (text: string) => <span style={{ color: 'var(--t-lo)' }}>{text}</span>;

export function ContactDrawer({ desk, lead: l, onClose }: { desk: Desk; lead: Lead; onClose: () => void }) {
  const now = useNow(60_000);
  const m = l.message_id != null ? desk.messageById.get(l.message_id) : undefined;
  const [c, label] = statusOf(l);
  const events = desk.eventsByLead.get(l.id) || [];
  return (
    <Drawer label={`Contact ${l.name}`} onClose={onClose}>
      <div className="eyebrow">{l.kind === 'old' ? 'Old contact' : 'New contact'} · {l.interest}</div>
      <h2>{l.name}</h2>
      <div style={{ margin: '14px 0' }}><span className={`chip ${c}`}>{label}</span></div>
      {m && <div className="quote">{m.text}
        <div className="note" style={{ marginTop: 6 }}>{CHANNEL[m.channel] || 'Other'} · {fmtTime(m.received_at, now)}</div>
      </div>}
      <dl className="kv" style={{ marginTop: 16 }}>
        <dt>Email</dt><dd>{l.email || none('None. Follow up by direct message')}</dd>
        <dt>Phone</dt><dd>{l.phone || none('None')}</dd>
        <dt>Tags</dt><dd>{l.tags?.length ? <Tags tags={l.tags} /> : none('None')}</dd>
        <dt>Source</dt><dd>{l.source}</dd>
        <dt>Added</dt><dd>{fmtClock(l.created_at)}</dd>
      </dl>
      <div className="sect"><h3>Activity</h3>
        <ul className="hist">
          {events.map(e => <li key={e.id}>
            <span className="t">{fmtClock(e.at)}</span><b>{e.label}</b>{e.detail ? '. ' + withNames(e.detail, desk.people) : ''}
          </li>)}
          {!events.length && <li>Nothing yet.</li>}
        </ul>
      </div>
    </Drawer>
  );
}
