import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../api';
import { withNames } from '../format';
import { Icon } from '../icons';
import type { Desk } from '../types';
import { useToast } from '../ui';

type Role = 'owner' | 'admin' | 'agent';
interface Person {
  email: string;
  name?: string | null;
  role: Role;
  status: 'approved' | 'pending';
  added_by: string;
  added_at: string | null;
  joined_at: string | null;
  invited_at: string | null;
  invites: number;
  invite_error: string | null;
}
type Listing = { people: Person[]; invited?: boolean; note?: string | null };

const ROLE: Record<Role, string> = { owner: 'Owner', admin: 'Admin', agent: 'Agent' };
const fmtDay = (iso: string | null) => iso ? new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) : '';
const path = (email: string) => `/api/desk/team/${encodeURIComponent(email)}`;

/* Only admins may read the team, so it loads on its own rather than with the desk's state.
   Every change answers with the whole list, which replaces the one shown. */
export function TeamView({ desk }: { desk: Desk }) {
  const client = useQueryClient();
  const toast = useToast();
  const { data, error } = useQuery({ queryKey: ['team'], queryFn: () => api<Listing>('/api/desk/team'), refetchOnMount: 'always' });
  const [email, setEmail] = useState('');
  const [role, setRole] = useState<'agent' | 'admin'>('agent');
  const [busy, setBusy] = useState<string | null>(null);  // the email being changed, or '+' while adding

  const change = async (key: string, call: () => Promise<Listing>, done: (r: Listing) => void) => {
    setBusy(key);
    try {
      const r = await call();
      client.setQueryData(['team'], { people: r.people });
      done(r);
    } catch (e) { toast((e as Error).message, 'u'); }
    setBusy(null);
  };
  const add = () => {
    const who = email.trim();
    if (!who) { document.getElementById('teamEmail')?.focus(); return toast('Enter their email first.'); }
    change('+', () => api<Listing>('/api/desk/team', { email: who, role }), r => {
      setEmail('');
      if (r.invited) toast(`${who} added as ${ROLE[role].toLowerCase()} and invited by email`);
      else toast(r.note || `${who} added`, 'u');
    });
  };
  const invite = (p: Person) => change(p.email, () => api<Listing>(`${path(p.email)}/invite`, {}), () => toast(`Invite sent to ${p.email}`));
  const setRoleOf = (p: Person, to: 'agent' | 'admin') =>
    change(p.email, () => api<Listing>(path(p.email), { role: to }, 'PATCH'), () => toast(`${p.email} is now ${to === 'admin' ? 'an admin' : 'an agent'}`));
  const remove = (p: Person) => {
    if (!confirm(`Remove ${p.email} from the Care Desk? They lose access within a minute.`)) return;
    change(p.email, () => api<Listing>(path(p.email), undefined, 'DELETE'), () => toast(`${p.email} removed`));
  };

  const people = data?.people;
  const counts: Partial<Record<Role, number>> = {};
  people?.forEach(p => { counts[p.role] = (counts[p.role] || 0) + 1; });
  const pending = people?.filter(p => p.status === 'pending').length || 0;

  return <>
    <div className="vh">
      <div><div className="eyebrow">06 · Desk access</div><h1>Team</h1></div>
      <p>Who can use the Care Desk. Admins manage this list; agents work tickets and contacts.</p>
    </div>
    <div className="card try">
      <label htmlFor="teamEmail">Add someone</label>
      <div className="row">
        <input id="teamEmail" type="email" placeholder="name@example.com" autoComplete="off" value={email}
          onChange={e => setEmail(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') add(); }} />
        <select id="teamRole" aria-label="Role" value={role} onChange={e => setRole(e.target.value as 'agent' | 'admin')}>
          <option value="agent">Agent</option><option value="admin">Admin</option>
        </select>
        <button className="btn primary" disabled={busy === '+'} onClick={add}><Icon name="user" size={16} />Add to the desk</button>
      </div>
      <p className="note">They get an email inviting them to the desk, and sign in with Google or an emailed code using this address. Removing someone signs them out within a minute.</p>
    </div>

    {error ? <p className="err">{(error as Error).message}</p> : !people ? <p className="loading">Loading the team</p> : <>
      <div className="tablewrap"><table className="team">
        <thead><tr><th>Person</th><th>Role</th><th>Status</th><th>Added by</th><th><span className="sr">Actions</span></th></tr></thead>
        <tbody>{people.map(p => {
          const self = p.email === desk.user.email, owner = p.role === 'owner', locked = owner || self, off = busy === p.email;
          return <tr key={p.email}>
            <td><span className="id">{p.name || p.email}</span>{self && <span className="you">You</span>}{p.name && <span className="sub">{p.email}</span>}</td>
            <td>{locked
              ? <span className={`chip ${owner ? 'w' : 'o'}`}>{ROLE[p.role] || p.role}</span>
              : <select className="sel" aria-label={`Role for ${p.email}`} value={p.role} disabled={off}
                  onChange={e => setRoleOf(p, e.target.value as 'agent' | 'admin')}>
                  <option value="agent">Agent</option><option value="admin">Admin</option>
                </select>}</td>
            <td>{p.status === 'approved'
              // Approved: has used the desk. Pending: on the list, not signed in yet.
              ? <><span className="chip g"><Icon name="check" size={12} stroke={2.6} />Approved</span>
                  <span className="sub">{owner ? 'Owner' : p.joined_at ? 'Signed in ' + fmtDay(p.joined_at) : ''}</span></>
              : <><span className="chip a">Pending</span>
                  <span className={`sub${p.invite_error ? ' bad' : ''}`}>{p.invite_error ? 'Invite not sent'
                    : p.invited_at ? `Invited ${fmtDay(p.invited_at)}${p.invites > 1 ? ` · ${p.invites} invites` : ''}` : 'Not invited yet'}</span></>}</td>
            <td>{withNames(p.added_by || '', desk.people)}<span className="sub">{owner ? 'Owner of the Redefine App' : fmtDay(p.added_at)}</span></td>
            <td><div className="rowacts">
              {p.status === 'pending' && !owner && <button className="btn sm" disabled={off} onClick={() => invite(p)}>
                <Icon name="send" size={14} />{p.invited_at ? 'Resend invite' : 'Send invite'}</button>}
              {!locked && <button className="btn sm" disabled={off} onClick={() => remove(p)}><Icon name="x" size={14} />Remove</button>}
            </div></td>
          </tr>;
        })}</tbody>
      </table></div>
      <p className="note">{people.length} {people.length === 1 ? 'person' : 'people'}: {(['owner', 'admin', 'agent'] as Role[]).filter(r => counts[r])
        .map(r => `${counts[r]} ${ROLE[r].toLowerCase()}${counts[r]! > 1 ? 's' : ''}`).join(', ')}. {pending ? `${pending} pending. ` : ''}
        Pending means they haven't signed in to the desk yet; it turns to Approved the first time they do. Owners are set on the server and are always admins.</p>
    </>}
  </>;
}
