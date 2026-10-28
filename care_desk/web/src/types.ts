// What /api/desk/state sends. Only the fields the new desk reads are typed;
// records carry more, and the old desk still uses those.

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

export interface Message {
  id: Id;
  channel: string;
  author: string;
  text: string;
  subject?: string | null;
  received_at: number;
  sort_status?: string;
  suggested_reply?: string | null;
  replied_at?: number | null;
  post_url?: string | null;
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
  subject: string;
  body: string;
}

export interface Journey {
  key: string;
  name: string;
  group: string;
  status: 'live' | 'paused' | 'draft' | string;
  steps: JourneyStep[];
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
  emails: Record_[];
  notes: Note[];
  journeys: Journey[] | null;
  config: {
    ticket_prefix: string;
    sign_off: string;
    sla: Record<Priority, { first: number; resolve: number }>;  // minutes
    [k: string]: unknown;
  };
  status: { sorting: string; email: boolean; email_from?: string | null; ghl_location?: string | null; [k: string]: unknown };
  people: Record<string, string>;
  user: { email: string; role: string; name?: string | null };
}

/** The desk as the page sees it: everything kept so far, newest first, plus lookups. */
export interface Desk {
  now: number;
  leads: Lead[];
  leadById: Map<Id, Lead>;
  messageById: Map<Id, Message>;
  eventsByLead: Map<Id, DeskEvent[]>;
  tickets: Ticket[];
  ticketById: Map<Id, Ticket>;
  eventsByTicket: Map<Id, DeskEvent[]>;
  notesByTicket: Map<Id, Note[]>;
  journeys: Record<string, Journey>;
  config: StatePayload['config'];
  status: StatePayload['status'];
  people: Record<string, string>;
  user: StatePayload['user'];
  counts: { inboxProblems: number };
}
