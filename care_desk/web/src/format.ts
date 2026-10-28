import { useSyncExternalStore } from 'react';
import { serverNow } from './api';

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

export const CHANNEL: Record<string, string> = {
  facebook: 'Facebook', instagram: 'Instagram', messenger: 'Messenger', tiktok: 'TikTok', linkedin: 'LinkedIn',
  threads: 'Threads', email: 'Email', website: 'Website',
};
export const channelLabel = (c: string) => CHANNEL[c] || 'Other';

// Records name people by email; pages show the name each gave, where there is one.
export const nameOf = (email: string | null | undefined, people: Record<string, string>) => (email && people[email]) || email || '';
export function withNames(text: string, people: Record<string, string>) {
  const emails = Object.keys(people);
  if (!emails.length) return text;
  const re = new RegExp(emails.map(e => e.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'gi');
  return text.replace(re, m => people[m.toLowerCase()] || m);
}
