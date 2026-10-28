import { useState } from 'react';
import { useAction } from '../desk';
import { CHANNELS } from '../format';
import { Icon } from '../icons';
import type { Id } from '../types';
import { useToast } from '../ui';

// For messages that never came through GHL: a call, a walk-in, an email forwarded by hand.
export function AddMessage({ onClose, onAdded }: { onClose: () => void; onAdded: (id: Id) => void }) {
  const [text, setText] = useState('');
  const [author, setAuthor] = useState('');
  const [email, setEmail] = useState('');
  const [channel, setChannel] = useState('facebook');
  const [busy, setBusy] = useState(false);
  const act = useAction();
  const toast = useToast();

  const add = async () => {
    if (busy) return;
    if (!text.trim()) { document.getElementById('tryText')?.focus(); return toast('Paste a message first.'); }
    setBusy(true);
    const r = await act<{ id: Id; duplicate: boolean }>('/api/desk/messages', { text, channel, author, email },
      r => (r.duplicate ? 'That message is already in the inbox.' : 'Added and sorted.'));
    setBusy(false);
    if (r) { setText(''); setAuthor(''); setEmail(''); onAdded(r.id); }
  };

  return <div className="card try">
    <div className="tryhd">
      <label htmlFor="tryText">Add a message by hand</label>
      <button className="btn ghost sm" aria-label="Close" onClick={onClose}><Icon name="x" size={15} /></button>
    </div>
    <div className="row">
      <textarea id="tryText" autoFocus value={text} onChange={e => setText(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) add(); }}
        placeholder="Paste a comment, message or email, for example: How much for a website for my cafe?" />
      <input placeholder="Name" aria-label="Name" autoComplete="off" value={author} onChange={e => setAuthor(e.target.value)} />
      <input type="email" placeholder="Email, if known" aria-label="Email" autoComplete="off" value={email} onChange={e => setEmail(e.target.value)} />
      <select aria-label="Channel" value={channel} onChange={e => setChannel(e.target.value)}>
        {Object.entries(CHANNELS).map(([k, c]) => <option key={k} value={k}>{c.label}</option>)}
      </select>
      <button className="btn primary" disabled={busy} onClick={add}><Icon name="zap" size={16} />Add and sort</button>
    </div>
  </div>;
}
