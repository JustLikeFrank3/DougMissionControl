/* notices.ts — what the watch on Douglas raised: the pure part of
   pi/deck-ui/js/notices.js. Colour is state: red is critical, amber a
   warning. */

import { fmtAgo, fmtClock } from './format';
import type { Tone } from './workstation';

export const NOTICE_ROUTE = '/api/douglas/memory';
export const SEVERITIES = ['critical', 'warning', 'information'] as const;
export const SEV_TONE: Record<string, Tone | 'warn'> = { critical: 'bad', warning: 'warn', information: '' };

/** How many are open, in the few words a pill has room for, and how bad the
    worst is. "—" for "could not be read", which is not "none". */
export function summary(got: any): { text: string; cls: Tone } {
  if (!got || !got.ok) return { text: '—', cls: '' };
  const open: any[] = got.open || [];
  if (!open.length) return { text: 'NONE', cls: 'ok' };
  const worst: Tone = open.some((n) => n.severity === 'critical') ? 'bad'
    : open.some((n) => n.severity === 'warning') ? 'busy' : '';
  const unseen = open.filter((n) => !n.acked).length;
  return { text: open.length + ' OPEN' + (unseen && unseen < open.length ? ' · ' + unseen + ' NEW' : ''), cls: worst };
}

/** An hour of the day one step on or back, round midnight. */
export function hourStep(h: unknown, by: number): number {
  return ((((+(h as number) || 0) + by) % 24) + 24) % 24;
}

/** Since when, and what has happened to it since: one line under the sentence. */
export function meta(n: any, now: number): string {
  const parts: string[] = [];
  if (n.cleared) {
    parts.push('over since ' + fmtClock(n.cleared) + ' · lasted ' + fmtAgo(n.raised, n.cleared));
  } else {
    parts.push('since ' + fmtClock(n.raised) + ' · ' + fmtAgo(n.raised, now));
    if (n.times > 1) parts.push('got worse');
    parts.push(n.acked ? 'seen' : n.told ? 'Douglas has mentioned it' : 'not yet mentioned');
    if (n.spoken) parts.push('said aloud');
  }
  return parts.join(' · ');
}
