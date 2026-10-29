import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { api } from '../api';
import { holdRefresh, useAction } from '../desk';
import { channelOf, fill, firstName, fmtClock, fmtIn, initials, nameOf, plural, useNow, withNames } from '../format';
import { Icon } from '../icons';
import { CategoryChip } from '../inbox/MessageDetail';
import { ReplyBlock } from '../inbox/ReplyBlock';
import { go, onLink } from '../nav';
import { TicketDrawer } from '../tickets/TicketDrawer';
import type { Desk, DeskEvent, Id, Lead, Message, SentEmail, Ticket } from '../types';
import { useToast } from '../ui';
import { KIND, statusOf } from './ContactsView';
import { possibleDuplicates } from './duplicates';
import { listedOrder } from './order';

type Thread = { lead: Lead; messages: Message[]; tickets: Ticket[]; emails: SentEmail[]; events: DeskEvent[] };

const none = (text: string) => <span style={{ color: 'var(--t-lo)' }}>{text}</span>;
const day = (t: number) => new Date(t).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
const clock = (t: number) => new Date(t).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
const DONE: Record<string, string> = { replied: 'Marked replied. No more journey emails.', won: 'Marked won', closed: 'Contact closed' };
const SENT: Record<SentEmail['status'], [string, string]> = { sent: ['g', 'Sent'], not_sent: ['o', 'Not sent'], failed: ['r', 'Failed'] };

/* One contact on a page of their own: their details on the left, everything said between them and
   Redefine in the middle, and what happened on the right. It loads the contact's whole history
   (the desk's state holds only the latest messages), and reloads with the desk. */
export function ContactPage({ desk, id }: { desk: Desk; id: Id }) {
  const { data, error } = useQuery({
    queryKey: ['desk', 'contact', id],  // under 'desk', so every change made on the desk reloads it
    queryFn: () => api<Thread>(`/api/desk/leads/${id}`),
    refetchInterval: 20_000,
  });
  const [ticket, setTicket] = useState<Id | null>(null);
  const lead = desk.leadById.get(id) || data?.lead;
  const order = listedOrder(), at = order.indexOf(id);
  const step = (k: number) => order[at + k] != null && go(`/contacts/${order[at + k]}`);

  const top = <div className="cbar">
    <a className="btn sm ghost" href="/contacts" onClick={onLink}><span aria-hidden="true">←</span>Contacts</a>
    {at >= 0 && <span className="cstep">
      <span className="num">{at + 1} / {order.length}</span>
      <button className="btn sm ghost" disabled={at === 0} onClick={() => step(-1)} aria-label="Previous contact">‹</button>
      <button className="btn sm ghost" disabled={at === order.length - 1} onClick={() => step(1)} aria-label="Next contact">›</button>
    </span>}
  </div>;

  if (!lead) {
    return <>{top}<p className="loading">{error ? "This contact isn't on the desk any more. It may have been deleted, or merged into another contact."
      : 'Loading the contact'}</p></>;
  }
  const open = ticket != null ? desk.ticketById.get(ticket) : undefined;
  return <>
    {top}
    <div className="cpage">
      <Details desk={desk} l={lead} />
      <Conversation desk={desk} l={lead} thread={data} openTicket={setTicket} />
      <section className="card cpanel" aria-label="Activity">
        <h3>Activity</h3>
        <Timeline events={data?.events} tickets={data?.tickets || []} people={desk.people} />
      </section>
    </div>
    {open && <TicketDrawer desk={desk} ticket={open} onClose={() => setTicket(null)} />}
  </>;
}

/* ---------------------------------------------------------------- left: who they are */

function Details({ desk, l }: { desk: Desk; l: Lead }) {
  const now = useNow(60_000);
  const act = useAction();
  const [c, label] = statusOf(l);
  const journey = l.kind === 'general' ? undefined : desk.journeys[l.kind === 'old' ? 'oldlead' : 'newlead'];
  const steps = journey?.steps.length ?? 0, sent = Math.min(l.step, steps);
  const next = l.status === 'active' && l.next_at != null ? journey?.steps[l.step] : undefined;
  const dupes = possibleDuplicates(desk.leads).get(l.id) || [];
  const merge = (o: Lead) => {
    if (!confirm(`Merge ${o.name}${o.email ? ` (${o.email})` : ''} into ${l.name}? Everything moves here and the other contact goes. This can't be undone.`)) return;
    act(`/api/desk/leads/${l.id}/merge`, { other: o.id }, `${o.name} merged in`);
  };

  return <section className="card cpanel" aria-label="Contact details">
    <div className="chead"><span className="av">{initials(l.name)}</span><div><h2>{l.name}</h2>
      <span className="sub">{(KIND[l.kind] || KIND.general)[1]} contact · {l.interest}</span></div></div>
    <dl className="kv">
      <dt>Email</dt><dd>{l.email || none('None')}
        {l.email && l.unsubscribed_at && <span className="sub unsub">Unsubscribed {fmtClock(l.unsubscribed_at)}. Bulk and journey emails skip them</span>}</dd>
      <dt>Phone</dt><dd>{l.phone || none('None')}</dd>
      <dt>Tags</dt><dd><TagEditor l={l} /></dd>
      <dt>Source</dt><dd>{l.source}</dd>
      <dt>Created</dt><dd>{fmtClock(l.created_at)}</dd>
    </dl>

    {journey && <div className="sect"><h3>Follow-up</h3>
      <dl className="kv">
        <dt>Journey</dt><dd>{journey.name}</dd>
        <dt>Status</dt><dd><span className={`chip ${c}`}>{label}</span></dd>
        <dt>Emails sent</dt><dd>
          <span className="steps" aria-label={`${sent} of ${steps} emails sent`}>
            {Array.from({ length: steps }, (_, k) => <i key={k} className={k < l.step ? 'done' : k === l.step && l.status === 'active' ? 'next' : ''} />)}
          </span> {sent} of {steps}</dd>
        <dt>Next email</dt><dd>{next ? <>{fmtIn(l.next_at!, now)}<span className="sub">
          {fill(next.subject, { first_name: firstName(l.name), interest: String(l.interest).toLowerCase(), sign_off: desk.config.sign_off })}
        </span></> : none('None')}</dd>
      </dl>
      <div className="acts">
        {['active', 'queued', 'manual'].includes(l.status) && <button className="btn sm" onClick={() => act(`/api/desk/leads/${l.id}/replied`, {}, DONE.replied)}>Mark replied</button>}
        {!['won', 'unsubscribed'].includes(l.status) && <button className="btn sm" onClick={() => act(`/api/desk/leads/${l.id}/won`, {}, DONE.won)}>Mark won</button>}
        {!['closed', 'won', 'unsubscribed', 'completed'].includes(l.status) && <button className="btn sm ghost" onClick={() => act(`/api/desk/leads/${l.id}/closed`, {}, DONE.closed)}>Close</button>}
      </div>
    </div>}

    {dupes.length > 0 && <div className="sect"><h3>Possible duplicates</h3>
      <p className="note">The same name, email or GHL contact as this one. If it's the same person, merge it in: its messages, activity, tags and missing details move here, and it goes from the list.</p>
      {dupes.map(o => <div key={o.id} className="card dupe">
        <a href={`/contacts/${o.id}`} onClick={onLink}><b>{o.name}</b></a>
        <span className="sub">{[o.email || 'No email', o.phone, o.source, `added ${fmtClock(o.created_at)}`].filter(Boolean).join(' · ')}</span>
        <span className="sub">{plural(desk.messages.filter(m => m.lead_id === o.id).length, 'message')}{o.kind !== 'general' ? ' · in a sales journey' : ''}</span>
        <div className="acts">
          <button className="btn sm primary" onClick={() => merge(o)}>Merge into this contact</button>
          <button className="btn sm ghost" onClick={() => act(`/api/desk/leads/${l.id}/separate`, { other: o.id }, 'Kept as different people')}>Not the same person</button>
        </div>
      </div>)}
    </div>}
  </section>;
}

/** Tags with a remove button each, and a field to add one. */
function TagEditor({ l }: { l: Lead }) {
  const act = useAction();
  const [text, setText] = useState('');
  const change = (action: 'tag' | 'untag', tag: string) =>
    act('/api/desk/leads/bulk', { action, ids: [l.id], tags: [tag] }, action === 'tag' ? `Tagged ${tag}` : `Removed ${tag}`);
  const add = async () => { const tag = text.trim(); if (tag && await change('tag', tag)) setText(''); };
  return <span className="tagedit">
    {(l.tags || []).map(t => <span key={t} className="tag">{t}
      <button aria-label={`Remove tag ${t}`} onClick={() => change('untag', t)}><Icon name="x" size={11} stroke={2.4} /></button></span>)}
    <input aria-label="Add a tag" placeholder="Add tag" value={text} maxLength={40}
      onChange={e => setText(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') add(); }} />
  </span>;
}

/* ---------------------------------------------------------------- middle: what was said */

type Item = { at: number; key: string; node: ReactNode };

function Conversation({ desk, l, thread, openTicket }:
  { desk: Desk; l: Lead; thread?: Thread; openTicket: (id: Id) => void }) {
  const [replying, setReplying] = useState<Id | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const items: Item[] = [];
  for (const old of thread?.messages || []) {
    const m = desk.messageById.get(old.id) || old;  // the desk's copy is the fresher one
    const t = m.ticket_id != null ? (desk.ticketById.get(m.ticket_id) || thread?.tickets.find(x => x.id === m.ticket_id)) : undefined;
    items.push({ at: m.received_at, key: `m${m.id}`, node: <Inbound desk={desk} m={m} ticket={t} openTicket={openTicket}
      replying={replying === m.id} toggle={() => setReplying(replying === m.id ? null : m.id)} /> });
    // A reply posted on Facebook, or one marked as posted elsewhere. Email replies are in the emails.
    if (m.replied_at && !m.emailed_at) {
      items.push({ at: m.replied_at, key: `r${m.id}`, node: <Bubble out head={<>
        <Icon name={m.fb_reply_id ? 'fb' : 'check'} size={13} />{m.fb_reply_id ? 'Replied on Facebook' : 'Marked replied'}
        {m.replied_by && <> · {nameOf(m.replied_by, desk.people)}</>}</>} at={m.replied_at}>
        {m.reply_text || none('Posted outside the desk.')}</Bubble> });
    }
  }
  for (const e of thread?.emails || []) items.push({ at: e.at, key: `e${e.id}`, node: <Outbound desk={desk} e={e} /> });
  for (const t of thread?.tickets || []) {
    const live = desk.ticketById.get(t.id) || t;
    items.push({ at: t.created_at, key: `t${t.id}`, node: <div className="tline">
      <Icon name="ticket" size={13} /><b>Ticket {live.code}</b> · {live.category} · {live.status}
      <button className="linkish" onClick={() => openTicket(t.id)}>Details</button>
    </div> });
  }
  items.sort((a, b) => a.at - b.at);
  // Open on the latest, the way a chat does.
  useEffect(() => { end.current?.scrollIntoView({ block: 'end' }); }, [thread?.messages.length, thread?.emails.length]);

  let lastDay = '';
  return <section className="card cpanel cthread" aria-label="Conversation">
    <div className="chead"><span className="av">{initials(l.name)}</span><h2>{l.name}</h2></div>
    <div className="cmsgs">
      {!thread && <p className="loading">Loading the conversation</p>}
      {thread && !items.length && <p className="note">Nothing yet.</p>}
      {items.map(it => {
        const d = day(it.at), sep = d !== lastDay;
        lastDay = d;
        return <div key={it.key}>{sep && <div className="cday">{d}</div>}{it.node}</div>;
      })}
      <div ref={end} />
    </div>
    <Composer desk={desk} l={l} />
  </section>;
}

function Bubble({ out, head, at, children }: { out?: boolean; head: ReactNode; at: number; children: ReactNode }) {
  return <div className={`bubble${out ? ' out' : ''}`}>
    <div className="bh">{head}<span className="bt" title={fmtClock(at)}>{clock(at)}</span></div>
    <div className="bb">{children}</div>
  </div>;
}

function Inbound({ desk, m, ticket, openTicket, replying, toggle }:
  { desk: Desk; m: Message; ticket?: Ticket; openTicket: (id: Id) => void; replying: boolean; toggle: () => void }) {
  const c = channelOf(m.channel);
  return <>
    <Bubble at={m.received_at} head={<><Icon name={c.icon} size={13} />
      {c.kind.toLowerCase() === c.label.toLowerCase() ? c.label : `${c.label} ${c.kind.toLowerCase()}`}</>}>
      {m.subject && <b className="bs">{m.subject}</b>}
      {m.text || none('No text in this webhook')}
      <div className="bf">
        {m.sort_status === 'sorted' && <CategoryChip m={m} />}
        {ticket && <button className="linkish" onClick={() => openTicket(ticket.id)}>Ticket {ticket.code}</button>}
        {m.sort_status === 'sorted' && <button className="linkish" onClick={toggle} aria-expanded={replying}>{replying ? 'Close' : 'Reply'}</button>}
      </div>
    </Bubble>
    {replying && <div className="breply"><ReplyBlock desk={desk} m={m} /></div>}
  </>;
}

const emailKind = (e: SentEmail, desk: Desk) =>
  e.journey === 'reply' ? 'Reply by email' : e.journey === 'direct' ? 'Email'
    : ['received', 'update', 'resolved'].includes(e.journey) ? 'Ticket update' : desk.journeys[e.journey]?.name || 'Email';

function Outbound({ desk, e }: { desk: Desk; e: SentEmail }) {
  const [more, setMore] = useState(false);
  const [c, label] = SENT[e.status] || SENT.not_sent;
  const long = e.body.length > 280;
  return <Bubble out at={e.at} head={<><Icon name="mail" size={13} />{emailKind(e, desk)} · to {e.to_addr}
    <span className={`chip ${c}`} style={{ height: 20 }} title={e.error || undefined}>{label}</span></>}>
    <b className="bs">{e.subject}</b>
    {more || !long ? e.body : e.body.slice(0, 280) + '…'}
    {long && <div className="bf"><button className="linkish" onClick={() => setMore(!more)}>{more ? 'Show less' : 'Show all'}</button></div>}
    {e.error && <div className="bf"><span className="sub unsub">{e.error}</span></div>}
  </Bubble>;
}

/** A new email to this contact, from the desk. One person: a personal email, with no unsubscribe link. */
function Composer({ desk, l }: { desk: Desk; l: Lead }) {
  const act = useAction();
  const toast = useToast();
  const [subject, setSubject] = useState('');
  const [body, setBody] = useState('');
  const [busy, setBusy] = useState(false);
  // While something is half-written, the 20-second reload waits.
  const drafting = !!(subject || body);
  useEffect(() => (drafting ? holdRefresh() : undefined), [drafting]);

  if (!l.email) return <div className="ccomp"><p className="note">No email on file. Reply to their messages above with Reply, or in GHL.</p></div>;
  if (!desk.status.email) return <div className="ccomp"><p className="note">Email sending isn't set up on the server, so email {l.email} from the info@ inbox for now.</p></div>;
  const send = async () => {
    if (!subject.trim()) { document.getElementById('cSubj')?.focus(); return toast('Write a subject first.'); }
    if (!body.trim()) { document.getElementById('cBody')?.focus(); return toast('Write the message first.'); }
    setBusy(true);
    const r = await act<{ sent: number; failed: number }>('/api/desk/leads/email', { ids: [l.id], subject, body },
      r => r.sent ? `Email sent to ${l.email}` : 'The email was not sent');
    setBusy(false);
    if (r?.sent) { setSubject(''); setBody(''); }
  };
  return <div className="ccomp">
    <input id="cSubj" aria-label="Subject" placeholder={`Email ${l.email} · subject`} maxLength={300}
      value={subject} onChange={e => setSubject(e.target.value)} />
    <div className="crow">
      <textarea id="cBody" aria-label="Message" placeholder="Type a message. {first_name} fills in their first name." maxLength={8000}
        value={body} onChange={e => setBody(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) send(); }} />
      <button className="btn primary" disabled={busy} onClick={send} aria-label="Send email"><Icon name="send" size={16} />Send</button>
    </div>
  </div>;
}

/* ---------------------------------------------------------------- right: what happened */

function Timeline({ events, tickets, people }: { events?: DeskEvent[]; tickets: Ticket[]; people: Record<string, string> }) {
  if (!events) return <p className="loading">Loading</p>;
  if (!events.length) return <p className="note">Nothing yet.</p>;
  const code = new Map(tickets.map(t => [t.id, t.code]));
  let lastDay = '';
  return <ul className="hist">
    {[...events].reverse().map(e => {
      const d = day(e.at), sep = d !== lastDay;
      lastDay = d;
      return <li key={e.id} className={sep ? 'newday' : undefined}>
        {sep && <span className="cday left">{d}</span>}
        <span className="t">{clock(e.at)}{e.ticket_id != null && code.get(e.ticket_id) ? ` · ${code.get(e.ticket_id)}` : ''}</span>
        <b>{e.label}</b>{e.detail ? '. ' + withNames(e.detail, people) : ''}
      </li>;
    })}
  </ul>;
}
