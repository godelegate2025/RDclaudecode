import { useState } from 'react';
import { api } from '../api';
import { useAction } from '../desk';
import { plural } from '../format';
import { Icon } from '../icons';
import { Modal, useToast } from '../ui';
import { parseCSV } from './csv';

type Verdict = { name: string; email: string; interest: string; ok: boolean; reason?: string };

export function ImportModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [raw, setRaw] = useState<Record<string, string>[] | null>(null);
  const [rows, setRows] = useState<Verdict[] | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const act = useAction();

  // Each row is checked on the server before anything is enrolled.
  const choose = async (file: File | undefined) => {
    if (!file) return;
    const parsed = parseCSV(await file.text());
    if (!parsed.length) return toast('That file has no rows.', 'u');
    try {
      const r = await api<{ rows: Verdict[] }>('/api/desk/import', { rows: parsed, dry_run: true });
      setRaw(parsed);
      setRows(r.rows);
    } catch (e) { toast((e as Error).message, 'u'); }
  };
  const enroll = async () => {
    setBusy(true);
    const r = await act<{ enrolled: number; skipped: number }>('/api/desk/import', { rows: raw, dry_run: false },
      r => `${plural(r.enrolled, 'contact')} enrolled, ${r.skipped} skipped`);
    setBusy(false);
    if (r) onDone();
  };

  const ok = rows ? rows.filter(r => r.ok).length : 0;
  const why = rows ? Object.entries(rows.filter(r => !r.ok).reduce<Record<string, number>>((a, r) => {
    const k = r.reason || 'Skipped';
    a[k] = (a[k] || 0) + 1;
    return a;
  }, {})) : [];

  return (
    <Modal label="Import old contacts" eyebrow="Import" title="Old inquiry list" onClose={onClose}>
      <p style={{ color: 'var(--t-mid)' }}>A CSV with the columns <code>name</code>, <code>email</code>, <code>interest</code>, <code>last_contact_days</code> and <code>consent</code> (yes or no). Each row is checked before anyone is enrolled: people without email consent, who unsubscribed, who are already a contact, or who were contacted in the last 90 days are skipped.</p>
      <label className="drop">Choose a CSV file
        <input type="file" accept=".csv,text/csv" onChange={e => { choose(e.target.files?.[0]); e.target.value = ''; }} />
      </label>
      {rows && <>
        <div style={{ overflowX: 'auto' }}><table className="imp">
          <thead><tr><th>Name</th><th>Email</th><th>Interest</th><th>Check</th></tr></thead>
          <tbody>
            {rows.slice(0, 8).map((r, k) => <tr key={k}><td>{r.name}</td><td>{r.email}</td><td>{r.interest}</td>
              <td>{r.ok ? <span className="chip g">Ready</span> : <span className="chip r">{r.reason}</span>}</td></tr>)}
            {rows.length > 8 && <tr><td colSpan={4} style={{ color: 'var(--t-lo)' }}>and {rows.length - 8} more rows</td></tr>}
          </tbody>
        </table></div>
        <dl className="kv" style={{ marginTop: 16 }}>
          <dt>Rows</dt><dd className="num">{rows.length}</dd>
          <dt>Ready to enroll</dt><dd className="num">{ok}</dd>
          {why.length > 0 && <><dt>Skipped</dt><dd>{why.map(([k, v]) => `${v} ${k.toLowerCase()}`).join(', ')}</dd></>}
        </dl>
        <div className="acts">
          <button className="btn primary" disabled={!ok || busy} onClick={enroll}><Icon name="upload" size={16} />Enroll {plural(ok, 'contact')}</button>
          <button className="btn ghost" onClick={onClose}>Cancel</button>
        </div>
      </>}
    </Modal>
  );
}
