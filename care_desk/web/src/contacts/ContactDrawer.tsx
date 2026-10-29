import { useAction } from '../desk';
import { channelLabel, fill, firstName, fmtClock, fmtIn, fmtTime, useNow } from '../format';
import type { Desk, Lead } from '../types';
import { Activity, Drawer, Tags } from '../ui';
import { KIND, statusOf } from './ContactsView';

const none = (text: string) => <span style={{ color: 'var(--t-lo)' }}>{text}</span>;
const DONE: Record<string, string> = { replied: 'Marked replied. No more journey emails.', won: 'Marked won', closed: 'Contact closed' };

export function ContactDrawer({ desk, lead: l, onClose }: { desk: Desk; lead: Lead; onClose: () => void }) {
  const now = useNow(60_000);
  const act = useAction();
  const [c, label] = statusOf(l);
  // Everything they sent that the desk has loaded, newest first: every message is filed under its contact.
  const messages = desk.messages.filter(m => m.lead_id === l.id);
  const journey = l.kind === 'general' ? undefined : desk.journeys[l.kind === 'old' ? 'oldlead' : 'newlead'];
  const steps = journey?.steps.length ?? 0, sent = Math.min(l.step, steps);
  const next = l.status === 'active' && l.next_at != null ? journey?.steps[l.step] : undefined;
  const setStatus = (status: string) => act(`/api/desk/leads/${l.id}/${status}`, {}, DONE[status]);

  return (
    <Drawer label={`Contact ${l.name}`} onClose={onClose}>
      <div className="eyebrow">{(KIND[l.kind] || KIND.general)[1]} contact · {l.interest}</div>
      <h2>{l.name}</h2>
      <dl className="kv" style={{ marginTop: 16 }}>
        <dt>Email</dt><dd>{l.email || none('None. Follow up by direct message')}</dd>
        <dt>Phone</dt><dd>{l.phone || none('None')}</dd>
        <dt>Tags</dt><dd>{l.tags?.length ? <Tags tags={l.tags} /> : none('None')}</dd>
        <dt>Source</dt><dd>{l.source}</dd>
        <dt>Added</dt><dd>{fmtClock(l.created_at)}</dd>
      </dl>

      {journey && <div className="sect"><h3>Follow-up</h3>
        <dl className="kv">
          <dt>Journey</dt><dd>{journey.name}</dd>
          <dt>Status</dt><dd><span className={`chip ${c}`}>{label}</span></dd>
          <dt>Emails sent</dt><dd>
            <span className="steps" aria-label={`${sent} of ${steps} emails sent`}>
              {Array.from({ length: steps }, (_, k) =>
                <i key={k} className={k < l.step ? 'done' : k === l.step && l.status === 'active' ? 'next' : ''} />)}
            </span> {sent} of {steps}</dd>
          <dt>Next email</dt><dd>{next ? <>{fmtIn(l.next_at!, now)}<span className="sub">
            {fill(next.subject, { first_name: firstName(l.name), interest: String(l.interest).toLowerCase(), sign_off: desk.config.sign_off })}
          </span></> : none('None')}</dd>
        </dl>
        <div className="acts">
          {['active', 'queued', 'manual'].includes(l.status) && <button className="btn sm" onClick={() => setStatus('replied')}>Mark replied</button>}
          {!['won', 'unsubscribed'].includes(l.status) && <button className="btn sm" onClick={() => setStatus('won')}>Mark won</button>}
          {!['closed', 'won', 'unsubscribed', 'completed'].includes(l.status) && <button className="btn sm ghost" onClick={() => setStatus('closed')}>Close</button>}
        </div>
      </div>}

      <div className="sect"><h3>Messages{messages.length > 1 ? ` · ${messages.length}` : ''}</h3>
        {messages.map(m => <div key={m.id} className="quote">{m.text || none('No text')}
          <div className="note" style={{ marginTop: 6 }}>
            {[m.category, channelLabel(m.channel), fmtTime(m.received_at, now)].filter(Boolean).join(' · ')}
          </div>
        </div>)}
        {!messages.length && <p className="note">None in the latest messages the desk has loaded.</p>}
      </div>
      <div className="sect"><h3>Activity</h3>
        <Activity events={desk.eventsByLead.get(l.id)} people={desk.people} />
      </div>
    </Drawer>
  );
}
