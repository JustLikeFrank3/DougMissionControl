/* memory.ts — what Douglas remembers: the pure part of
   pi/deck-ui/js/memory.js. Every line is what his memory holds, as it gave
   it, and a memory that could not be read is said so, never shown as an
   empty one. */

import { fmtAgo } from './format';

export const MEMORY_ROUTE = '/api/douglas/memory';
export const MEMORY_PAGE = 30;
export const STATES: [string, string][] = [['active', 'KEPT'], ['superseded', 'REPLACED'], ['deleted', 'DELETED']];
export const KINDS = ['preference', 'fact', 'project', 'decision', 'task', 'lesson', 'fleet', 'summary', 'place',
                      'observation'];
const HOW: Record<string, string> = { owner: 'you set this', said: 'you said so', observed: 'he saw it happen',
                                      inferred: 'he worked it out' };
const WHO: Record<string, string> = { owner: 'you', douglas: 'him', memory: 'the memory itself' };

/** One earlier thing that happened to a memory: what, by whom, when, and
    what it said before where that is not what it says now. */
export function pastLine(h: any, m: any, now: number): string {
  return (h.action === 'restored' ? 'brought back' : h.action) + ' by ' + (WHO[h.who] || h.who) +
    ' · ' + fmtAgo(h.at, now) + ' ago' +
    (h.before && h.before !== m.text ? ' · was: ' + h.before : '') +
    (h.action === 'kept over a weaker note' && h.after ? ' · the note: ' + h.after : '');
}

/** Where a memory came from and how far to trust it, in one line. */
export function provenance(m: any, now: number): string {
  const parts = [HOW[m.source] || m.source || 'unknown'];
  if (m.via) parts.push(m.via);
  parts.push(fmtAgo(m.updated, now) + ' ago');
  parts.push('sure ' + Math.round((m.confidence || 0) * 100) + '%');
  if (m.uses) parts.push('used ' + m.uses + '×');
  if (m.done) parts.push('done ' + fmtAgo(m.done, now) + ' ago');
  return parts.join(' · ');
}

/** The path one page of the list is asked for with. */
export function listPath(st: { status: string; kind: string; q: string }, offset: number): string {
  return MEMORY_ROUTE + '?op=list&status=' + st.status + '&limit=' + MEMORY_PAGE + '&offset=' + offset +
    (st.kind ? '&kind=' + st.kind : '') + (st.q ? '&q=' + encodeURIComponent(st.q) : '');
}

/** How much he remembers, for the card's pill. */
export function memoryCount(stats: any): string {
  return !stats ? '—' : stats.active + ' KEPT' + (stats.open_tasks ? ' · ' + stats.open_tasks + ' TO DO' : '');
}

/** One line of the record of what he did. */
export function didLine(e: any): string {
  return [e.action, e.ok ? e.detail : 'failed' + (e.detail ? ' (' + e.detail + ')' : ''),
          e.ms ? (e.ms >= 1000 ? (e.ms / 1000).toFixed(1) + ' s' : e.ms + ' ms') : '', e.via]
    .filter(Boolean).join(' · ');
}

/** The words for a list with nothing in it. */
export function emptyWords(st: { status: string; q: string }): string {
  return st.q ? 'Nothing he remembers matches that.'
    : st.status === 'deleted' ? 'Nothing deleted.'
    : st.status === 'superseded' ? 'Nothing has been replaced by something newer.'
    : 'Nothing kept yet. He keeps what is worth keeping once a conversation has gone quiet, ' +
      'and at once when told to remember something.';
}
