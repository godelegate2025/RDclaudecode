import type { Desk, DeskEvent, Id, Lead, Message, Note, SentEmail, StatePayload, Ticket } from './types';

// The header the desk requires on every change: a custom header cannot be sent
// cross-site without a CORS preflight, which the desk never grants.
const HEADERS = { 'X-Requested-With': 'desk' };

/** GET without a body, POST with one; `method` overrides (the team list uses PATCH and DELETE). */
export async function api<T = Record<string, unknown>>(path: string, body?: unknown, method?: string): Promise<T> {
  const r = await fetch(path, body === undefined
    ? { method: method || 'GET', headers: HEADERS }
    : { method: method || 'POST', headers: { ...HEADERS, 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (r.status === 401) {
    location.href = '/login';
    throw new Error('Signed out');
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${r.status})`);
  return data as T;
}

/* The first load brings everything; after that the server sends only what
   changed since the last load, and this keeps the records between loads. */
const kept = {
  messages: new Map<Id, Message>(),
  tickets: new Map<Id, Ticket>(),
  leads: new Map<Id, Lead>(),
  events: new Map<Id, DeskEvent>(),
  emails: new Map<Id, SentEmail>(),
  notes: new Map<Id, Note>(),
};
type Kept = typeof kept;
let since = 0;
let journeys: StatePayload['journeys'] = null;
let skew = 0;  // server clock minus this browser's, so countdowns agree with the server

export const serverNow = () => Date.now() + skew;

function group<T>(into: Map<Id, T[]>, key: Id, item: T) {
  if (!into.has(key)) into.set(key, []);
  into.get(key)!.push(item);
}

export async function loadDesk(): Promise<Desk> {
  const s = await api<StatePayload>('/api/desk/state' + (since ? `?since=${since}` : ''));
  skew = s.now - Date.now();
  since = s.now - 5000;  // a little overlap, so a write landing mid-request is not missed
  if (s.full) Object.values(kept).forEach(m => m.clear());
  // A deleted contact comes back as a bare marker, so loads that only fetch changes drop it too.
  (Object.keys(kept) as (keyof Kept)[]).forEach(k => {
    const into = kept[k] as Map<Id, { id: Id; deleted?: boolean }>;
    (s[k] as { id: Id; deleted?: boolean }[]).forEach(x => (x.deleted ? into.delete(x.id) : into.set(x.id, x)));
  });
  if (s.journeys) journeys = s.journeys;  // only sent when a template changed

  const leads = [...kept.leads.values()].sort((a, b) => b.created_at - a.created_at);
  const tickets = [...kept.tickets.values()].sort((a, b) => b.created_at - a.created_at);
  const eventsByLead = new Map<Id, DeskEvent[]>(), eventsByTicket = new Map<Id, DeskEvent[]>();
  [...kept.events.values()].sort((a, b) => a.at - b.at).forEach(e => {
    if (e.lead_id != null) group(eventsByLead, e.lead_id, e);
    if (e.ticket_id != null) group(eventsByTicket, e.ticket_id, e);
  });
  const notesByTicket = new Map<Id, Note[]>();
  [...kept.notes.values()].sort((a, b) => a.at - b.at).forEach(n => group(notesByTicket, n.ticket_id, n));
  const messages = [...kept.messages.values()].sort((a, b) => b.received_at - a.received_at);
  return {
    now: s.now,
    leads,
    leadById: new Map(leads.map(l => [l.id, l])),
    messages,
    messageById: new Map(messages.map(m => [m.id, m])),
    eventsByLead,
    tickets,
    ticketById: new Map(tickets.map(t => [t.id, t])),
    eventsByTicket,
    notesByTicket,
    emails: [...kept.emails.values()].sort((a, b) => b.at - a.at),
    journeys: Object.fromEntries((journeys || []).map(j => [j.key, j])),
    config: s.config,
    status: s.status,
    people: s.people,
    user: s.user,
    counts: {
      inboxProblems: messages.filter(m => m.sort_status === 'failed' || m.sort_status === 'unparsed').length,
    },
  };
}
