import { useSyncExternalStore } from 'react';
import { serverNow } from './api';
import type { IconName } from './icons';
import type { Desk, Message } from './types';

export const MIN = 60_000, HOUR = 60 * MIN, DAY = 24 * HOUR;

export const fmtClock = (t: number) => {
  const d = new Date(t);
  return d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' }) + ' '
    + d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
};
export const fmtTime = (t: number, now: number) => {
  const d = new Date(t);
  return (now - t > DAY ? d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ' ' : '')
    + d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
};
export const fmtDur = (ms: number) => {
  const m = Math.max(0, Math.floor(ms / MIN));
  if (m < 60) return m + 'm';
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${String(m % 60).padStart(2, '0')}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
};
export const fmtAgo = (t: number, now: number) => (now - t < MIN ? 'just now' : fmtDur(now - t) + ' ago');
export const fmtIn = (t: number, now: number) => (t - now <= 0 ? 'due now' : 'in ' + fmtDur(t - now));

export const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`;
export const firstName = (n: string) => {
  const f = String(n || '').trim().split(/\s+/)[0];
  return f && f.toLowerCase() !== 'unknown' ? f : 'there';
};
/** Fills {first_name} and the like; unknown keys stay as they are. */
export const fill = (s: string, v: Record<string, string>) => String(s).replace(/\{(\w+)\}/g, (m, k) => v[k] ?? m);

/* One clock for the whole page, ticking once a second while anything listens. */
const listeners = new Set<() => void>();
let tick = serverNow();
let timer: ReturnType<typeof setInterval> | undefined;
function listen(fn: () => void) {
  listeners.add(fn);
  if (!timer) {
    tick = serverNow();  // fresh after a quiet spell, and once the server's clock is known
    timer = setInterval(() => { tick = serverNow(); listeners.forEach(f => f()); }, 1000);
  }
  return () => {
    listeners.delete(fn);
    if (!listeners.size) { clearInterval(timer); timer = undefined; }
  };
}

/** The server's clock, rounded down to `every` ms: a component redraws only when that changes. */
export function useNow(every = 1000) {
  return useSyncExternalStore(listen, () => Math.floor(tick / every) * every);
}

export const fmtSize = (b: number) => (b < 1024 ? b + ' B' : b < 1048576 ? Math.round(b / 1024) + ' KB' : (b / 1048576).toFixed(1) + ' MB');
export const median = (a: number[]) => {
  if (!a.length) return null;
  const s = a.slice().sort((x, y) => x - y), k = s.length >> 1;
  return s.length % 2 ? s[k] : (s[k - 1] + s[k]) / 2;
};

// Every channel a message can come from: its name, its icon, and what one item from it is called.
export const CHANNELS: Record<string, { label: string; icon: IconName; kind: string }> = {
  facebook: { label: 'Facebook', icon: 'fb', kind: 'Comment' },
  instagram: { label: 'Instagram', icon: 'ig', kind: 'Comment' },
  messenger: { label: 'Messenger', icon: 'dm', kind: 'Message' },
  tiktok: { label: 'TikTok', icon: 'tt', kind: 'Comment' },
  linkedin: { label: 'LinkedIn', icon: 'li', kind: 'Comment' },
  threads: { label: 'Threads', icon: 'at', kind: 'Comment' },
  email: { label: 'Email', icon: 'mail', kind: 'Email' },
  website: { label: 'Website', icon: 'web', kind: 'Form' },
  other: { label: 'Other', icon: 'dm', kind: 'Message' },
};
export const channelOf = (c: string) => CHANNELS[c] || CHANNELS.other;
export const channelLabel = (c: string) => channelOf(c).label;
export const initials = (n: string) =>
  String(n || '?').split(/\s+/).filter(Boolean).map(w => w[0]).slice(0, 2).join('').toUpperCase() || '?';

// Records name people by email; pages show the name each gave, where there is one.
export const nameOf = (email: string | null | undefined, people: Record<string, string>) => (email && people[email]) || email || '';
export function withNames(text: string, people: Record<string, string>) {
  const emails = Object.keys(people);
  if (!emails.length) return text;
  const re = new RegExp(emails.map(e => e.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'gi');
  return text.replace(re, m => people[m.toLowerCase()] || m);
}

// The comment on its post when GHL sent the link, otherwise GHL's comment inbox.
export function ghlLink(desk: Desk, m?: Message) {
  if (m?.post_url) return m.post_url;
  const loc = desk.status.ghl_location;
  return loc ? `https://app.gohighlevel.com/v2/location/${encodeURIComponent(loc)}/marketing/social-planner/comments` : null;
}
