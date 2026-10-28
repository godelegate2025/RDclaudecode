import { Fragment, useEffect, useState, type ReactNode } from 'react';
import { holdRefresh, useAction } from '../desk';
import { fill, fmtClock, fmtTime, useNow } from '../format';
import { Icon } from '../icons';
import type { Desk, Id, Journey, JourneyStep, SentEmail } from '../types';
import { Banner, Modal } from '../ui';

const JST = { live: ['g', 'Live'], pending: ['a', 'Waiting on approval'], paused: ['o', 'Paused'] } as const;
const STE = { sent: ['g', 'Sent'], not_sent: ['o', 'Not sent'], failed: ['r', 'Failed'] } as const;
const GROUPS: [string, string[]][] = [
  ['Contact follow-up', ['newlead', 'oldlead']],
  ['Service updates', ['received', 'update', 'resolved']],
];

const sample = (desk: Desk) => ({
  first_name: 'Carlo', ticket: desk.config.ticket_prefix + '-1042', team: 'client success', interest: 'website', sign_off: desk.config.sign_off,
});

/** A template with its merge fields filled in (coral) and its [PLACEHOLDERS] marked (amber). */
function Filled({ text, vars }: { text: string; vars: Record<string, string> }) {
  const out: ReactNode[] = [];
  let at = 0;
  for (const m of text.matchAll(/\{(\w+)\}|\[([^\]]+)\]/g)) {
    out.push(text.slice(at, m.index));
    if (m[2] != null) out.push(<span key={m.index} className="ph">[{m[2]}]</span>);
    else if (vars[m[1]] != null) out.push(<span key={m.index} className="merge">{vars[m[1]]}</span>);
    else out.push(m[0]);
    at = m.index + m[0].length;
  }
  out.push(text.slice(at));
  return <>{out}</>;
}

/** How the email looks in the customer's inbox. */
function Mail({ subject, body, to, vars }: { subject: string; body: string; to: string; vars?: Record<string, string> }) {
  const show = (s: string) => vars ? <Filled text={s} vars={vars} /> : s;
  const paras = String(body).split(/\n\s*\n/).filter(p => p.trim());
  return <div className="mail" aria-label="Email preview">
    <div className="mh"><b>To:</b> {to}<br /><b>Subject:</b> {show(subject)}</div>
    <div className="mb">
      {paras.map((p, k) => <p key={k}>{p.split('\n').map((line, i) => <span key={i}>{i > 0 && <br />}{show(line)}</span>)}</p>)}
      <div className="mf">{vars ? 'Coral fields fill in per person. Amber items must be filled in before this can go live.' : 'Sent by the Care Desk.'}</div>
    </div>
  </div>;
}

function Editor({ step, onSave, onCancel }: { step: JourneyStep; onSave: (subject: string, body: string) => Promise<boolean>; onCancel: () => void }) {
  const [subject, setSubject] = useState(step.subject);
  const [body, setBody] = useState(step.body);
  const [busy, setBusy] = useState(false);
  // Nothing is redrawn under the person typing.
  useEffect(() => holdRefresh(), []);
  const save = async () => { setBusy(true); if (!await onSave(subject, body)) setBusy(false); };
  return <>
    <div className="field"><label htmlFor="edSubj">Subject</label><input id="edSubj" value={subject} maxLength={300} onChange={e => setSubject(e.target.value)} /></div>
    <div className="field"><label htmlFor="edBody">Body</label><textarea id="edBody" autoFocus value={body} onChange={e => setBody(e.target.value)} /></div>
    <p className="note">Merge fields: <code>{'{first_name}'}</code> <code>{'{interest}'}</code> <code>{'{ticket}'}</code> <code>{'{team}'}</code> <code>{'{sign_off}'}</code>. Leave a blank line between paragraphs.</p>
    <div className="acts">
      <button className="btn primary" disabled={busy} onClick={save}><Icon name="check" size={16} stroke={2.4} />Save</button>
      <button className="btn ghost" onClick={onCancel}>Cancel</button>
    </div>
  </>;
}

function Approval({ j, setStatus }: { j: Journey & { enrolled: number | null }; setStatus: (s: 'live' | 'paused') => void }) {
  if (j.placeholders.length) {
    return <div className="approval"><Icon name="shield" /><span style={{ flex: '1 1 220px' }}>
      Fill in {j.placeholders.map((p, k) => <Fragment key={p}>{k > 0 && ', '}
        <span className="ph" style={{ color: 'var(--amber)', background: 'rgba(233,162,59,.18)' }}>[{p}]</span></Fragment>)} before this can go live. Click a step, then Edit.
    </span></div>;
  }
  if (j.status !== 'live') {
    return <div className="approval"><Icon name="shield" /><span style={{ flex: '1 1 220px' }}>
      {j.status === 'paused' ? 'Paused.' : 'Ready for approval.'}{j.enrolled ? ` ${j.enrolled} contacts are waiting.` : ''}
    </span><button className="btn sm primary" onClick={() => setStatus('live')}><Icon name="check" size={14} stroke={2.4} />Approve and go live</button></div>;
  }
  return <div className="approval ok"><Icon name="shield" /><span style={{ flex: '1 1 220px' }}>
    Live. Editing an email so it has a [PLACEHOLDER] takes it back to approval.
  </span><button className="btn sm" onClick={() => setStatus('paused')}>Pause</button></div>;
}

function SentModal({ desk, e, onClose }: { desk: Desk; e: SentEmail; onClose: () => void }) {
  return <Modal label="Sent email" onClose={onClose}
    eyebrow={`${fmtClock(e.at)} · ${desk.journeys[e.journey]?.name || e.journey} · ${STE[e.status][1]}`}>
    {e.error && <p className="err">{e.error}</p>}
    <Mail subject={e.subject} body={e.body} to={e.to_addr} />
    <div className="acts"><button className="btn" onClick={onClose}>Close</button></div>
  </Modal>;
}

/** The journey to show first: the one named after the # (from "Review the journey"), or new contacts. */
const journeyFromHash = () => location.hash.split('/')[1] || 'newlead';

export function EmailView({ desk }: { desk: Desk }) {
  const now = useNow(60_000);
  const act = useAction();
  const [key, setKey] = useState(journeyFromHash);
  const [stepAt, setStepAt] = useState(0);
  const [editing, setEditing] = useState(false);
  const [viewing, setViewing] = useState<Id | null>(null);

  useEffect(() => {
    const follow = () => { setKey(journeyFromHash()); setStepAt(0); setEditing(false); };
    window.addEventListener('hashchange', follow);
    return () => window.removeEventListener('hashchange', follow);
  }, []);

  // How many contacts are in each journey, and how many of its emails went out.
  const stats = (j: Journey) => ({
    ...j,
    enrolled: j.group === 'leads'
      ? desk.leads.filter(l => l.kind === (j.key === 'oldlead' ? 'old' : 'new') && ['active', 'queued'].includes(l.status)).length : null,
    sent: desk.emails.filter(e => e.journey === j.key && e.status === 'sent').length,
  });
  const base = desk.journeys[key] || desk.journeys.newlead;
  if (!base) return <p className="loading">Loading the journeys</p>;
  const j = stats(base);
  const idx = Math.min(stepAt, j.steps.length - 1), st = j.steps[idx];
  const vars = sample(desk);
  const pick = (k: string) => { setKey(k); setStepAt(0); setEditing(false); };
  const setStatus = (status: 'live' | 'paused') => act<{ released?: number }>(`/api/desk/journeys/${j.key}/status`, { status },
    r => status === 'live' ? `Live.${r.released ? ` ${r.released} queued contacts released.` : ''}` : 'Paused');
  const save = async (subject: string, body: string) => {
    const r = await act(`/api/desk/journeys/${j.key}/steps/${idx}`, { subject, body }, 'Email saved');
    if (r) setEditing(false);
    return !!r;
  };
  const sent = viewing != null ? desk.emails.find(e => e.id === viewing) : undefined;

  const card = (k: string) => {
    const x = desk.journeys[k] && stats(desk.journeys[k]);
    if (!x) return null;
    return <button key={k} className="jcard" aria-selected={j.key === k} onClick={() => pick(k)}>
      <span className="jt"><b>{x.name}</b><span className={`chip ${JST[x.status][0]}`}><span className="dot" />{JST[x.status][1]}</span></span>
      <p>{x.description}</p>
      <span className="jm">
        <span>{x.steps.length} email{x.steps.length > 1 ? 's' : ''}</span>
        {x.enrolled != null && <span><b className="num">{x.enrolled}</b> in journey</span>}
        <span><b className="num">{x.sent}</b> sent</span>
      </span>
    </button>;
  };

  return <>
    <div className="vh">
      <div><div className="eyebrow">05 · Email automations</div><h1>Email</h1></div>
      <p>Service emails keep people updated on their tickets. Contact journeys follow up with new and old contacts. Nothing with a [PLACEHOLDER] in it can be sent.</p>
    </div>
    {!desk.status.email && <Banner icon="mail">
      <span>No email service is set up yet. Service emails are logged as not sent and contact journeys wait. Set <code>RESEND_API_KEY</code> and <code>DESK_FROM_EMAIL</code> on the server.</span>
    </Banner>}

    <div className="flows">
      <div className="jlist">
        {GROUPS.map(([label, keys]) => <Fragment key={label}>
          <div className="group-l">{label}</div>{keys.map(card)}
        </Fragment>)}
        <div className="card log" style={{ padding: '16px 18px' }}>
          <div className="eyebrow">Sent log</div>
          {desk.emails.slice(0, 15).map(e => <button key={e.id} className="logrow" onClick={() => setViewing(e.id)}>
            <span className="t">{fmtTime(e.at, now)}</span>
            <span style={{ minWidth: 0 }}><b>{e.subject}</b><span className="to">{e.to_addr}{e.error ? ' · ' + e.error : ''}</span></span>
            <span className={`chip ${STE[e.status][0]}`}>{STE[e.status][1]}</span>
          </button>)}
          {!desk.emails.length && <p className="note">No emails yet.</p>}
        </div>
      </div>

      <section className="card flow" aria-label={j.name}>
        <div className="fh"><h2>{j.name}</h2><span className={`chip ${JST[j.status][0]}`}><span className="dot" />{JST[j.status][1]}</span></div>
        <div className="trigger"><span className="ti"><Icon name="zap" size={16} /></span>
          <span><span className="eyebrow" style={{ display: 'block' }}>Starts when</span>{j.trigger}</span></div>
        <ol className="fsteps">{j.steps.map((s, k) => <li key={k}>
          <span className="delay">{s.delay ? s.label.replace('Day ', 'D') : <Icon name="zap" size={13} />}</span>
          <button className="fstep" aria-selected={idx === k} onClick={() => { setStepAt(k); setEditing(false); }}>
            <small>{s.label}</small><b>{fill(s.subject, vars)}</b>
          </button>
        </li>)}</ol>
        {j.exit && <div className="exit"><b>Exit rule.</b> {j.exit}</div>}
        <Approval j={j} setStatus={setStatus} />
        {editing
          ? <Editor key={`${j.key}-${idx}`} step={st} onSave={save} onCancel={() => setEditing(false)} />
          : <>
            <div className="acts"><button className="btn sm" onClick={() => setEditing(true)}><Icon name="edit" size={14} />Edit this email</button></div>
            <Mail subject={st.subject} body={st.body} to="[sample contact]" vars={vars} />
          </>}
      </section>
    </div>

    {sent && <SentModal desk={desk} e={sent} onClose={() => setViewing(null)} />}
  </>;
}
