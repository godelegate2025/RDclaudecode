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
  tickets: Record_[];
  leads: Lead[];
  events: DeskEvent[];
  emails: Record_[];
  notes: Record_[];
  journeys: Journey[] | null;
  config: { ticket_prefix: string; sign_off: string; [k: string]: unknown };
  status: { sorting: string; email: boolean; email_from?: string | null; [k: string]: unknown };
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
  journeys: Record<string, Journey>;
  config: StatePayload['config'];
  status: StatePayload['status'];
  people: Record<string, string>;
  user: StatePayload['user'];
  counts: { inboxProblems: number };
}
