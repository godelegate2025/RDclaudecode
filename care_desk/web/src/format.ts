import { useEffect, useState } from 'react';
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

/** The server's clock, ticking once a second, for countdowns and "ago" labels. */
export function useNow(every = 1000) {
  const [now, setNow] = useState(serverNow);
  useEffect(() => {
    const t = setInterval(() => setNow(serverNow()), every);
    return () => clearInterval(t);
  }, [every]);
  return now;
}
