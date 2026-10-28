import { fmtDur, MIN, useNow } from '../format';
import type { Desk, Ticket } from '../types';

// Each ticket has two targets, set by its priority: time to first reply and time to resolution.
export const targetOf = (desk: Desk, t: Ticket) => desk.config.sla[t.priority] || desk.config.sla.Normal;
export const firstOver = (desk: Desk, t: Ticket, now: number) =>
  !t.first_reply_at && t.status !== 'Resolved' && now - t.created_at > targetOf(desk, t).first * MIN;
export const resolveOver = (desk: Desk, t: Ticket, now: number) =>
  t.status !== 'Resolved' && now - t.created_at > targetOf(desk, t).resolve * MIN;
export const isOverdue = (desk: Desk, t: Ticket, now: number) => firstOver(desk, t, now) || resolveOver(desk, t, now);

/** A running clock from `since` until `until` (or now), with a bar that fills toward the target. */
export function Clock({ since, until, target }: { since: number; until?: number | null; target: number }) {
  const now = useNow(1000);
  const end = until || now, p = (end - since) / target;
  const bar = until ? (p > 1 ? 'over' : 'done') : p > 1 ? 'over' : p > 0.75 ? 'warn' : '';
  return <>
    <span className={`timer${!until && p > 1 ? ' over' : ''}`}>{fmtDur(end - since)}</span>
    <span className={`sla ${bar}`}><b style={{ width: `${Math.min(100, p * 100)}%` }} /></span>
  </>;
}

export const FirstReplyClock = ({ desk, t }: { desk: Desk; t: Ticket }) =>
  <Clock since={t.created_at} until={t.first_reply_at} target={targetOf(desk, t).first * MIN} />;
export const ResolutionClock = ({ desk, t }: { desk: Desk; t: Ticket }) =>
  <Clock since={t.created_at} until={t.resolved_at} target={targetOf(desk, t).resolve * MIN} />;
