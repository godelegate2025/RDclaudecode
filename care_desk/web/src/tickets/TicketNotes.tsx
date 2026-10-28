import { useRef, useState } from 'react';
import { useAction } from '../desk';
import { fmtClock, fmtSize, nameOf } from '../format';
import { Icon } from '../icons';
import type { Desk, Id, Ticket } from '../types';
import { useToast } from '../ui';

const MAX_FILES = 5, MAX_BYTES = 10 * 1024 * 1024;

// Unsent notes and the files picked for them, by ticket, so closing the panel never loses them.
const drafts = new Map<Id, { text: string; files: File[] }>();

const base64 = (f: File) => new Promise<string>((ok, no) => {
  const r = new FileReader();
  r.onload = () => ok(String(r.result).split(',')[1] || '');
  r.onerror = () => no(r.error);
  r.readAsDataURL(f);
});

export function TicketNotes({ desk, ticket: t }: { desk: Desk; ticket: Ticket }) {
  const [draft, setDraft] = useState(() => drafts.get(t.id) || { text: '', files: [] });
  const [busy, setBusy] = useState(false);
  const picker = useRef<HTMLInputElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const toast = useToast();
  const act = useAction();
  const notes = desk.notesByTicket.get(t.id) || [];

  const keep = (next: { text: string; files: File[] }) => { drafts.set(t.id, next); setDraft(next); };
  const pick = (chosen: FileList | null) => {
    const files = draft.files.concat(Array.from(chosen || []));
    if (files.length > MAX_FILES) return toast(`Attach up to ${MAX_FILES} files per note.`, 'u');
    if (files.reduce((n, f) => n + f.size, 0) > MAX_BYTES) return toast(`Attach up to ${MAX_BYTES / 1048576} MB per note.`, 'u');
    if (files.some(f => !f.size)) return toast('That file is empty.', 'u');
    keep({ ...draft, files });
  };
  const add = async () => {
    if (busy) return;
    if (!draft.text.trim() && !draft.files.length) { box.current?.focus(); return toast('Write a note or attach a file first.'); }
    setBusy(true);
    let files;
    try { files = await Promise.all(draft.files.map(async f => ({ name: f.name, type: f.type, data: await base64(f) }))); }
    catch { setBusy(false); return toast('A file could not be read. Attach it again.', 'u'); }
    const r = await act(`/api/desk/tickets/${t.id}/notes`, { text: draft.text, files }, draft.files.length ? 'Note and files added' : 'Note added');
    setBusy(false);
    if (r) { drafts.delete(t.id); setDraft({ text: '', files: [] }); }
  };

  const list = notes.length > 0 && <div className="tnotes">{notes.map(n => <div key={n.id} className="tnote">
    <div className="by"><b title={n.by}>{nameOf(n.by, desk.people)}</b> · {fmtClock(n.at)}</div>
    {n.text && <p>{n.text}</p>}
    {n.files.length > 0 && <div className="files">{n.files.map(f =>
      <a key={f.id} className="file" href={`/api/desk/files/${f.id}`} target="_blank" rel="noopener">
        <Icon name="file" size={14} /><span>{f.name}</span><small>{fmtSize(f.size)}</small>
      </a>)}</div>}
  </div>)}</div>;

  // Notes and files go on open tickets only; a resolved one shows what it has.
  if (t.status === 'Resolved') {
    return <div className="sect"><h3>Notes</h3>{list}
      <p className="note" style={notes.length ? undefined : { marginTop: 0 }}>{notes.length ? 'Reopen' : 'No notes. Reopen'} the ticket to add notes or files.</p>
    </div>;
  }
  return <div className="sect"><h3>Notes</h3>{list}
    <div className="compose">
      <textarea ref={box} aria-label="Note" placeholder="Add a note for the team. The customer never sees it."
        value={draft.text} onChange={e => keep({ ...draft, text: e.target.value })}
        onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) add(); }} />
      {draft.files.length > 0 && <div className="files">{draft.files.map((f, k) => <span key={k} className="file">
        <Icon name="file" size={14} /><span>{f.name}</span><small>{fmtSize(f.size)}</small>
        <button aria-label={`Remove ${f.name}`} onClick={() => keep({ ...draft, files: draft.files.filter((_, i) => i !== k) })}><Icon name="x" size={13} /></button>
      </span>)}</div>}
      <div className="acts">
        <button className="btn sm" onClick={() => picker.current?.click()}><Icon name="clip" size={14} />Attach files</button>
        <input ref={picker} type="file" multiple hidden onChange={e => { pick(e.target.files); e.target.value = ''; }} />
        <span className="note" style={{ margin: 0 }}>Up to {MAX_FILES} files, {MAX_BYTES / 1048576} MB in all</span>
        <button className="btn sm primary" disabled={busy} onClick={add}><Icon name="check" size={14} stroke={2.4} />Add note</button>
      </div>
    </div>
  </div>;
}
