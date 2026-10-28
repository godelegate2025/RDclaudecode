import { useMemo, useState } from 'react';
import { useAction } from '../desk';
import { plural } from '../format';
import { Icon, type IconName } from '../icons';
import type { Desk, Lead } from '../types';
import { Banner, Modal } from '../ui';

export type BulkKind = 'sms' | 'email' | 'whatsapp' | 'tag' | 'untag' | 'delete';

// Email sends for real once the server can send it; SMS and WhatsApp aren't connected yet,
// so their windows let people write the message and say so.
const SEND: Record<'sms' | 'email' | 'whatsapp', { label: string; title: string; has: (l: Lead) => boolean; what: string; off: string }> = {
  sms: { label: 'Send SMS', title: 'New text message', has: l => !!l.phone, what: 'a phone number', off: 'Texting' },
  email: { label: 'Send email', title: 'New email', has: l => !!l.email, what: 'an email address', off: 'Email' },
  whatsapp: { label: 'Send WhatsApp', title: 'New WhatsApp message', has: l => !!l.phone, what: 'a phone number', off: 'WhatsApp' },
};

const splitTags = (v: string) => v.split(',').map(t => t.trim()).filter(Boolean);

type Props = { kind: BulkKind; desk: Desk; people: Lead[]; onClose: () => void; onDone: (deleted: boolean) => void };

export function BulkModal(props: Props) {
  const { kind } = props;
  if (kind === 'sms' || kind === 'email' || kind === 'whatsapp') return <SendModal {...props} kind={kind} />;
  if (kind === 'tag') return <TagModal {...props} />;
  if (kind === 'untag') return <UntagModal {...props} />;
  return <DeleteModal {...props} />;
}

const Cancel = ({ onClose }: { onClose: () => void }) => <button className="btn ghost" onClick={onClose}>Cancel</button>;

function Primary({ icon, busy, disabled, onClick, children }:
  { icon: IconName; busy: boolean; disabled?: boolean; onClick: () => void; children: React.ReactNode }) {
  return <button className="btn primary" disabled={busy || disabled} onClick={onClick}><Icon name={icon} size={16} />{children}</button>;
}

function SendModal({ kind, desk, people, onClose, onDone }: Props & { kind: keyof typeof SEND }) {
  const b = SEND[kind], n = people.length, reach = people.filter(b.has).length;
  const live = kind === 'email' && desk.status.email;
  const [subject, setSubject] = useState('');
  const [body, setBody] = useState('');
  const [busy, setBusy] = useState(false);
  const act = useAction();

  const send = async () => {
    if (!subject.trim()) return void document.getElementById('bSubj')?.focus();
    if (!body.trim()) return void document.getElementById('bBody')?.focus();
    if (reach > 1 && !confirm(`Send this email to ${reach} contacts? Each gets their own copy.`)) return;
    setBusy(true);
    const r = await act<{ sent: number; unsubscribed?: number; failed?: number }>('/api/desk/leads/email',
      { ids: people.map(l => l.id), subject, body }, r => {
        const extra = [r.unsubscribed && `${r.unsubscribed} unsubscribed`, r.failed && `${r.failed} failed`].filter(Boolean).join(', ');
        return `Sent to ${plural(r.sent, 'contact')}${extra ? ` (${extra})` : ''}`;
      });
    setBusy(false);
    if (r) onDone(false);
  };

  return (
    <Modal eyebrow={`${b.label} · ${plural(n, 'contact')}`} title={b.title} onClose={onClose}>
      {live
        ? reach > 1 && <Banner icon="mail" ok><span>Each person gets their own email{desk.status.email_from ? ` from ${desk.status.email_from}` : ''}, with an unsubscribe link. Anyone who unsubscribed is skipped.</span></Banner>
        : <Banner><span>{b.off} from the desk isn't connected yet, so nothing will be sent.</span></Banner>}
      <p className="note">{reach} of {n} selected {n === 1 ? 'has' : 'have'} {b.what}.{reach < n ? ` The other ${n - reach} would be skipped.` : ''}</p>
      {kind === 'email' && <div className="field"><label htmlFor="bSubj">Subject</label>
        <input id="bSubj" maxLength={300} value={subject} onChange={e => setSubject(e.target.value)} /></div>}
      <div className="field"><label htmlFor="bBody">Message</label>
        <textarea id="bBody" maxLength={kind === 'email' ? 8000 : 1600} placeholder="Hi {first_name}, "
          value={body} onChange={e => setBody(e.target.value)} /></div>
      <p className="note">Write {'{first_name}'} where each person's first name should go.</p>
      <div className="acts">
        <Primary icon="send" busy={busy} disabled={!live || !reach} onClick={send}>Send to {reach}</Primary>
        <Cancel onClose={onClose} />
      </div>
    </Modal>
  );
}

function TagModal({ desk, people, onClose, onDone }: Props) {
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const act = useAction();
  const known = useMemo(() => [...new Set(desk.leads.flatMap(l => l.tags || []))].sort((a, b) => a.localeCompare(b)), [desk.leads]);
  const n = people.length;

  const add = (t: string) => {
    const have = splitTags(value);
    if (!have.some(x => x.toLowerCase() === t.toLowerCase())) setValue([...have, t].join(', '));
    document.getElementById('tagInput')?.focus();
  };
  const save = async () => {
    const tags = splitTags(value);
    if (!tags.length) return void document.getElementById('tagInput')?.focus();
    setBusy(true);
    const r = await act<{ changed: number }>('/api/desk/leads/bulk', { action: 'tag', ids: people.map(l => l.id), tags },
      r => (r.changed ? `Tagged ${plural(r.changed, 'contact')}` : 'They already have those tags'));
    setBusy(false);
    if (r) onDone(false);
  };

  return (
    <Modal eyebrow={`Add tags · ${plural(n, 'contact')}`} title="Add tags" onClose={onClose}>
      <div className="field"><label htmlFor="tagInput">Tags</label>
        <input id="tagInput" maxLength={400} autoComplete="off" placeholder="e.g. VIP, Cebu, Call back in May"
          value={value} onChange={e => setValue(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') save(); }} /></div>
      <p className="note">Separate tags with commas. Each one is added to every selected contact.</p>
      {known.length > 0 && <div className="tagpick" aria-label="Tags already in use">
        {known.slice(0, 40).map(t => <button key={t} className="fbtn" onClick={() => add(t)}><Icon name="plus" size={12} />{t}</button>)}
      </div>}
      <div className="acts"><Primary icon="tag" busy={busy} onClick={save}>Add to {plural(n, 'contact')}</Primary><Cancel onClose={onClose} /></div>
    </Modal>
  );
}

function UntagModal({ people, onClose, onDone }: Props) {
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const act = useAction();
  const counts: Record<string, number> = {};
  people.forEach(l => (l.tags || []).forEach(t => { counts[t] = (counts[t] || 0) + 1; }));
  const tags = Object.keys(counts).sort((a, b) => a.localeCompare(b));
  const n = people.length;

  const toggle = (t: string) => setChosen(s => { const c = new Set(s); if (c.has(t)) c.delete(t); else c.add(t); return c; });
  const save = async () => {
    setBusy(true);
    const r = await act<{ changed: number }>('/api/desk/leads/bulk', { action: 'untag', ids: people.map(l => l.id), tags: [...chosen] },
      r => `Tags removed from ${plural(r.changed, 'contact')}`);
    setBusy(false);
    if (r) onDone(false);
  };

  return (
    <Modal eyebrow={`Remove tags · ${plural(n, 'contact')}`} title="Remove tags" onClose={onClose}>
      {tags.length ? <>
        <p className="note">Pick the tags to take off the selected contacts. The number is how many of them have it.</p>
        <div className="tagpick">{tags.map(t => <button key={t} className="fbtn" aria-pressed={chosen.has(t)} onClick={() => toggle(t)}>
          {t}<span className="n num">{counts[t]}</span></button>)}</div>
        <div className="acts">
          <Primary icon="x" busy={busy} disabled={!chosen.size} onClick={save}>Remove {chosen.size || ''} tag{chosen.size === 1 ? '' : 's'}</Primary>
          <Cancel onClose={onClose} />
        </div>
      </> : <>
        <p className="note">None of the selected contacts have tags.</p>
        <div className="acts"><button className="btn" onClick={onClose}>Close</button></div>
      </>}
    </Modal>
  );
}

function DeleteModal({ people, onClose, onDone }: Props) {
  const [busy, setBusy] = useState(false);
  const act = useAction();
  const n = people.length;
  const remove = async () => {
    setBusy(true);
    const r = await act<{ changed: number }>('/api/desk/leads/bulk', { action: 'delete', ids: people.map(l => l.id) },
      r => `${plural(r.changed, 'contact')} deleted`);
    setBusy(false);
    if (r) onDone(true);
  };
  return (
    <Modal eyebrow={`Delete · ${plural(n, 'contact')}`} title={`Delete ${plural(n, 'contact')}?`} onClose={onClose}>
      <p style={{ color: 'var(--t-mid)' }}>{n === 1 ? people[0].name : `These ${n} contacts`} will be removed from the desk along with their activity, and no more journey emails will go to them. The inbox messages they came from stay. This can't be undone.</p>
      <div className="acts"><Primary icon="trash" busy={busy} onClick={remove}>Delete {plural(n, 'contact')}</Primary><Cancel onClose={onClose} /></div>
    </Modal>
  );
}
