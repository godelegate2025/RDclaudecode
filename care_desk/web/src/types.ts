// What /api/desk/state sends. Only the fields the desk reads are typed;
// records carry more.

export type Id = number;

export interface Lead {
  id: Id;
  name: string;
  email?: string | null;
  phone?: string | null;
  tags?: string[];
  interest: string;
  source: string;
  kind: 'new' | 'old';
  status: string;
  step: number;
  next_at?: number | null;
  created_at: number;
  last_contact_days?: number | null;
  message_id?: Id | null;
  deleted?: boolean;
}

export interface FbCandidate { id: string; message: string; created_at: number; comment_url?: string | null }

export interface Message {
  id: Id;
  channel: string;
  author: string;
  email?: string | null;
  text: string;
  subject?: string | null;
  received_at: number;
  source?: string;
  // Sorting: pending until Claude (or the keyword rules) has read it.
  sort_status?: 'pending' | 'sorted' | 'failed' | 'unparsed' | string;
  sort_error?: string | null;
  sorted_by?: string;
  intent?: 'sales' | 'service' | 'other' | string;
  category?: string | null;
  priority?: Priority;
  interest?: string | null;
  summary?: string | null;
  confidence?: number | null;
  route?: 'ticket' | 'lead' | 'logged' | string | null;
  ticket_id?: Id | null;
  lead_id?: Id | null;
  raw?: unknown;
  // Replying
  suggested_reply?: string | null;
  replied_at?: number | null;
  replied_by?: string | null;
  reply_text?: string | null;
  emailed_at?: number | null;
  post_url?: string | null;
  fb_match?: 'matched' | 'ambiguous' | 'pending' | 'none' | string | null;
  fb_candidates?: FbCandidate[];
  fb_reply_id?: string | null;
  match_error?: string | null;
  deleted?: boolean;
}

export type Priority = 'Urgent' | 'High' | 'Normal';

export interface Ticket {
  id: Id;
  code: string;
  priority: Priority;
  status: 'Open' | 'In progress' | 'Resolved';
  category: string;
  team: string;
  owner?: string | null;
  customer: string;
  channel: string;
  email?: string | null;
  created_at: number;
  first_reply_at?: number | null;
  resolved_at?: number | null;
  escalated_at?: number | null;
  message_id?: Id | null;
  deleted?: boolean;
}

export interface NoteFile { id: Id; name: string; type: string; size: number }

export interface Note {
  id: Id;
  ticket_id: Id;
  by: string;
  at: number;
  text: string;
  files: NoteFile[];
  deleted?: boolean;
}

export interface DeskEvent {
  id: Id;
  at: number;
  label: string;
  detail?: string | null;
  ticket_id?: Id | null;
  lead_id?: Id | null;
  deleted?: boolean;
}

export interface JourneyStep {
  delay: number;
  label: string;
  subject: string;
  body: string;
}

export interface Journey {
  key: string;
  name: string;
  group: string;
  status: 'live' | 'pending' | 'paused';
  trigger: string;
  description: string;
  exit?: string;
  steps: JourneyStep[];
  placeholders: string[];
}

export interface SentEmail {
  id: Id;
  at: number;
  journey: string;
  to_addr: string;
  subject: string;
  body: string;
  status: 'sent' | 'not_sent' | 'failed';
  error?: string | null;
  deleted?: boolean;
}

export interface Record_ {
  id: Id;
  deleted?: boolean;
  [k: string]: unknown;
}

export interface StatePayload {
  now: number;
  full: boolean;
  messages: Message[];
  tickets: Ticket[];
  leads: Lead[];
  events: DeskEvent[];
  emails: SentEmail[];
  notes: Note[];
  journeys: Journey[] | null;
  config: {
    ticket_prefix: string;
    sign_off: string;
    sla: Record<Priority, { first: number; resolve: number }>;  // minutes
    [k: string]: unknown;
  };
  status: {
    sorting: string;
    email: boolean;
    email_from?: string | null;
    ghl_location?: string | null;
    webhook?: boolean;
    facebook_replies?: boolean;
    [k: string]: unknown;
  };
  people: Record<string, string>;
  user: { email: string; role: string; name?: string | null };
}

/** The desk as the page sees it: everything kept so far, newest first, plus lookups. */
export interface Desk {
  now: number;
  leads: Lead[];
  leadById: Map<Id, Lead>;
  messages: Message[];
  messageById: Map<Id, Message>;
  eventsByLead: Map<Id, DeskEvent[]>;
  tickets: Ticket[];
  ticketById: Map<Id, Ticket>;
  eventsByTicket: Map<Id, DeskEvent[]>;
  notesByTicket: Map<Id, Note[]>;
  emails: SentEmail[];
  journeys: Record<string, Journey>;
  config: StatePayload['config'];
  status: StatePayload['status'];
  people: Record<string, string>;
  user: StatePayload['user'];
  counts: { inboxProblems: number };
}
