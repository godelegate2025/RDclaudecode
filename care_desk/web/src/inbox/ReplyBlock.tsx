import { useState } from 'react';
import { useAction } from '../desk';
import { fmtClock, fmtTime, ghlLink, nameOf, useNow } from '../format';
import { Icon } from '../icons';
import type { Desk, Message } from '../types';
import { useToast } from '../ui';

// Half-written replies, by message, so picking another message and coming back keeps them.
const drafts = new Map<string, string>();

function useDraft(key: string, start: string) {
  const [text, setText] = useState(() => drafts.get(key) ?? start);
  return [text, (v: string) => { drafts.set(key, v); setText(v); }, () => { drafts.delete(key); setText(''); }] as const;
}

/* The reply: sent from here when the comment is matched to Facebook, otherwise copied into GHL.
   Emails to info@ are answered from the desk, in the customer's own thread. */
export function ReplyBlock({ desk, m }: { desk: Desk; m: Message }) {
  if (m.channel === 'email' && m.email) return <EmailReply desk={desk} m={m} />;
  return <SocialReply desk={desk} m={m} />;
}

function EmailReply({ desk, m }: { desk: Desk; m: Message }) {
  const now = useNow(60_000);
  const act = useAction();
  const toast = useToast();
  const [text, setText, clear] = useDraft(`email-${m.id}`, m.replied_at ? '' : m.suggested_reply || '');
  const [busy, setBusy] = useState(false);

  const last = m.replied_at && <div className="draft">
    <span className="eyebrow">{m.emailed_at ? 'Replied by email' : 'Marked replied'} · {nameOf(m.replied_by, desk.people)} · {fmtTime(m.emailed_at || m.replied_at, now)}</span>
    {m.reply_text || 'Replied outside the desk.'}
  </div>;
  const markReplied = (label: string, cls = 'btn sm ghost') =>
    <button className={cls} onClick={() => act(`/api/desk/messages/${m.id}/replied`, {}, 'Marked as replied')}>{label}</button>;

  if (!desk.status.email) {
    return <>{last}
      <p className="note">Email sending isn't set up on the server, so reply from the info@ inbox for now.</p>
      {!m.replied_at && <div className="acts">{markReplied('Mark replied')}</div>}
    </>;
  }
  const send = async () => {
    if (!text.trim()) { document.getElementById(`ereply-${m.id}`)?.focus(); return toast('Write the reply first.'); }
    setBusy(true);
    const r = await act(`/api/desk/messages/${m.id}/email`, { text }, 'Email sent');
    setBusy(false);
    if (r) clear();
  };
  return <>{last}
    <div className="replybox">
      <label className="eyebrow" htmlFor={`ereply-${m.id}`}>
        {m.replied_at ? 'Send another reply' : 'Reply by email'} · to {m.email}{desk.status.email_from ? ` from ${desk.status.email_from}` : ''}
      </label>
      <textarea id={`ereply-${m.id}`} maxLength={8000} value={text} onChange={e => setText(e.target.value)} />
    </div>
    <div className="acts">
      <button className="btn sm primary" disabled={busy} onClick={send}><Icon name="send" size={14} />Send email</button>
      {!m.replied_at && markReplied('Replied elsewhere')}
    </div>
    <p className="note">It goes as a reply in their email thread, with their message quoted underneath.</p>
  </>;
}

function SocialReply({ desk, m }: { desk: Desk; m: Message }) {
  const now = useNow(60_000);
  const act = useAction();
  const toast = useToast();
  const [text, setText, clear] = useDraft(`reply-${m.id}`, m.suggested_reply || '');
  const [busy, setBusy] = useState(false);
  const link = ghlLink(desk, m);
  const post = (action: string, done?: string) => act(`/api/desk/messages/${m.id}/${action}`, {}, done);

  const openLink = link && <a className="btn sm" href={link} target="_blank" rel="noopener noreferrer">
    <Icon name="ext" size={14} />{m.post_url ? 'Open on Facebook' : 'Open GHL comments'}</a>;

  if (m.replied_at) {
    return <>
      <div className="draft">
        <span className="eyebrow">{m.fb_reply_id ? 'Replied on Facebook' : 'Reply marked posted'} · {nameOf(m.replied_by, desk.people)} · {fmtTime(m.replied_at, now)}</span>
        {m.reply_text || 'Posted outside the desk.'}
      </div>
      <div className="acts">{openLink}</div>
    </>;
  }

  const copy = async () => {
    try { await navigator.clipboard.writeText(m.suggested_reply || ''); toast('Reply copied. Paste it in GHL, then mark it posted.'); }
    catch { toast('Could not copy. Select the text and copy it yourself.', 'u'); }
  };
  const manual = <>
    {m.suggested_reply && <div className="draft"><span className="eyebrow">Suggested public reply · check it, then post it in GHL</span>{m.suggested_reply}</div>}
    <div className="acts">
      {m.suggested_reply && <button className="btn sm" onClick={copy}><Icon name="copy" size={14} />Copy reply</button>}
      {openLink}
      <button className="btn sm primary" onClick={() => post('replied', 'Marked as replied')}><Icon name="check" size={14} stroke={2.4} />Mark reply posted</button>
    </div>
  </>;
  if (!(desk.status.facebook_replies && m.channel === 'facebook')) return manual;

  if (m.fb_match === 'matched') {
    const send = async () => {
      if (!text.trim()) { document.getElementById(`reply-${m.id}`)?.focus(); return toast('Write the reply first.'); }
      if (!confirm('Post this reply publicly on Facebook as Redefine Ph?')) return;
      setBusy(true);
      const r = await act(`/api/desk/messages/${m.id}/send`, { text }, 'Reply posted on Facebook');
      setBusy(false);
      if (r) clear();
    };
    return <>
      <div className="replybox">
        <label className="eyebrow" htmlFor={`reply-${m.id}`}>Public reply · posts as Redefine Ph under this comment</label>
        <textarea id={`reply-${m.id}`} maxLength={8000} value={text} onChange={e => setText(e.target.value)} />
      </div>
      <div className="acts">
        <button className="btn sm primary" disabled={busy} onClick={send}><Icon name="send" size={14} />Send reply</button>
        {openLink}
        <button className="btn sm ghost" onClick={() => post('replied', 'Marked as replied')}>Replied elsewhere</button>
      </div>
    </>;
  }
  if (m.fb_match === 'ambiguous') {
    return <>
      <div className="replybox"><span className="eyebrow">Several Facebook comments match. Which one is it?</span>
        {(m.fb_candidates || []).map(c => <div key={c.id} className="cand">
          <span><b>{c.message}</b><small>{fmtClock(c.created_at)}{c.comment_url && <> · <a href={c.comment_url} target="_blank" rel="noopener noreferrer">view</a></>}</small></span>
          <button className="btn sm" onClick={() => act(`/api/desk/messages/${m.id}/pick`, { comment_id: c.id }, 'Matched. You can reply from here now.')}>This one</button>
        </div>)}
      </div>
      {manual}
    </>;
  }
  if (m.fb_match === 'pending') {
    return <><p className="note">Looking for this comment on Facebook so you can reply from here{m.match_error ? `: ${m.match_error}` : '…'}</p>{manual}</>;
  }
  return <>
    <p className="note">Couldn't find this comment on Facebook, so reply in GHL instead.{' '}
      <button className="btn sm ghost" onClick={() => { toast('Searching Facebook'); post('rematch'); }}><Icon name="refresh" size={13} />Search again</button>
    </p>
    {manual}
  </>;
}
