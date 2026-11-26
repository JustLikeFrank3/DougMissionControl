/* owner.ts — the things only the owner's screens do to Douglas: the pure
   part of pi/deck-ui/js/owner.js. An answer to a change that is waiting for
   a yes; a switch for each thing he could use; the job in his workshop; his
   conversations. */

import { fmtAgo } from './format';

const WHERE: Record<string, string> = { webui: 'the chat page', phone: 'the phone', deck: 'the panel',
                                        voice: 'by voice', chat: 'a chat window' };
const WHAT: Record<string, string> = { push: 'A PUSH', job: 'A JOB', forget: 'FORGETTING',
                                       records: 'A CHANGE TO YOUR RECORDS' };

/* What each tool is, in a few words, and whether it can change anything. */
export const TOOLS: [string, string, string][] = [
  ['memory', 'his memory', 'notes and recalls'],
  ['fleet', 'the machines at home', 'reads'],
  ['camera', 'the Ring cameras', 'looks through the ones switched on under CAMERAS'],
  ['self', 'his own machine', 'reads'],
  ['weather', 'the weather', 'reads'],
  ['news', 'the news', 'reads'],
  ['wikipedia', 'the Wikipedia lookup', 'reads; with web pages on he can still read its pages'],
  ['read_page', 'web pages', 'reads'],
  ['search', 'web search', 'reads; what is searched for leaves the house'],
  ['reddit', 'Reddit search', 'reads; what is searched for leaves the house'],
  ['look', 'looking at pictures', 'reads'],
  ['draw', 'drawing', 'makes pictures'],
  ['edit', 'editing pictures', 'makes pictures'],
  ['jobcontext', 'your records', 'changes them on a yes'],
  ['repo', 'the workshop', 'clones, runs jobs, pushes on a yes'],
];

/** How long a change still waits, in the few words there is room for. */
export function left(until: unknown, now: number): string {
  const s = Math.floor((+(until as number) || 0) - now);
  if (!(s > 0)) return 'no longer waiting';
  return (s >= 60 ? Math.floor(s / 60) + ' m ' : '') + (s % 60) + ' s left';
}

/** The line over a change that is waiting: what it is and where it was asked for. */
export function waitingHead(p: any): string {
  return (WHAT[p && p.kind] || 'A CHANGE') + ' · asked on ' + (WHERE[p && p.via] || 'another line');
}

/** How the requirements a job was handed stand; '' for a job handed none. */
export function needsLine(report: any): string {
  const needs: any[] = (report && report.needs) || [];
  if (!needs.length) return '';
  const count = (s: string) => needs.filter((n) => n.state === s).length;
  const done = count('done'), blocked = count('blocked'), pending = count('pending');
  const out = needs.length - done - blocked - pending;
  if (pending === needs.length) return needs.length + ' REQUIREMENT' + (needs.length === 1 ? '' : 'S');
  return done + ' OF ' + needs.length + ' DONE' + (out ? ' · ' + out + ' OUTSTANDING' : '') +
    (blocked ? ' · ' + blocked + ' BLOCKED' : '');
}

/** The job in his workshop, in a line. */
export function jobLine(job: any, now: number): string {
  if (!job) return '';
  const needs = needsLine(job.report);
  return (job.running ? 'IN THE WORKSHOP · under way' + (job.began ? ' for ' + fmtAgo(job.began, now) : '')
    : 'IN THE WORKSHOP · ended ' + (job.ended ? fmtAgo(job.ended, now) + ' ago' : 'a while ago')) +
    (needs ? ' · ' + needs : '');
}

/** Does a job that has ended fall short of what it was handed? */
export function jobShort(job: any): boolean {
  const needs: any[] = (job && job.report && job.report.needs) || [];
  return !!job && !job.running && needs.some((n) => n.state !== 'done');
}

export type JobRow = ['k' | 'v' | 'n', string, string?];

/** What a job was handed and what became of it, as rows to show under its
    line. 'k' is a heading, 'v' text under it, 'n' a requirement with its
    state. Whose words each is, is in the heading. */
export function jobRows(report: any): JobRow[] {
  const r = report || {};
  const rows: JobRow[] = [];
  const add = (k: string, v: unknown) => {
    if (v) { rows.push(['k', k]); rows.push(['v', String(v)]); }
  };
  add('ASKED', r.asked);
  add('WHERE', r.repo ? r.repo + (r.base_branch ? ' · from ' + r.base_branch : '') +
    (r.base ? ' @ ' + r.base : '') + (r.branch ? ' · on ' + r.branch : '') : '');
  (r.before || []).forEach((b: string, i: number) => add(i ? '' : 'SAID JUST BEFORE', b));
  (r.context || []).forEach((c: any) => add('HANDED OVER WITH IT · ' + (c.from || 'the conversation'), c.text));
  if ((r.needs || []).length) rows.push(['k', 'REQUIREMENTS · ' + needsLine(r) + ' · his own word on each']);
  (r.needs || []).forEach((n: any) => {
    rows.push(['n', n.n + '. ' + n.text + (n.accept ? '\nDone when: ' + n.accept : '') + (n.note ? '\n' + n.note : ''),
               n.state || 'outstanding']);
  });
  add('CHANGED', (r.stat || '') + ((r.files || []).length ? (r.stat ? '\n' : '') + r.files.join('\n') : ''));
  const ran: string[] = (r.checks || []).map((c: any) =>
    (c.ok ? 'passed' : 'FAILED') + ' · ' + c.what + (c.ok || !c.said ? '' : ' (' + c.said + ')'))
    .concat((r.ran || []).map((c: any) =>
      (c.code === null || c.code === undefined ? 'did not finish' : 'exit ' + c.code) + ' · ' + c.cmd));
  if (r.looked === 'ok') ran.push('a second look at the change found nothing to put right');
  if (r.looked === 'found') ran.push('a second look found things to put right, and he went back to them');
  if (r.state && r.state !== 'running' && r.state !== 'queued' && r.kind !== 'look') {
    add('CHECKS AND WHAT WAS RUN', ran.join('\n') || 'nothing was run on the result');
  }
  add('WHAT WENT WRONG', r.error);
  add('HIS OWN ACCOUNT · not checked', r.summary);
  add('PULL REQUEST', r.pr);
  return rows;
}

export type SwitchRow = { name: string; label: string; does: string; state: string };

/** The switches as rows: every tool there is a word for, then any the
    pipeline has that this app has not heard of, so that none is hidden. */
export function switchRows(tools: Record<string, string> | null | undefined): SwitchRow[] {
  if (!tools) return [];
  const named = TOOLS.filter((t) => t[0] in tools).map((t) => ({ name: t[0], label: t[1], does: t[2], state: tools[t[0]] }));
  const known = TOOLS.map((t) => t[0]);
  Object.keys(tools).sort().forEach((k) => {
    if (known.indexOf(k) < 0) named.push({ name: k, label: k, does: '', state: tools[k] });
  });
  return named;
}

/** How many are switched off, for the card's pill; null when not readable. */
export function switchesPill(tools: Record<string, string> | null | undefined): { text: string; cls: '' | 'ok' | 'busy' } {
  if (!tools) return { text: '—', cls: '' };
  const off = Object.keys(tools).filter((k) => tools[k] === 'off').length;
  return off ? { text: off + ' OFF', cls: 'busy' } : { text: 'ALL ON', cls: 'ok' };
}

const CHAT_WHERE: Record<string, string> = { webui: 'chat page', phone: 'phone', deck: 'panel', voice: 'voice',
                                             chat: 'chat window' };

export type ChatRow = { id: string; title: string; here: boolean; sub: string };

/** His conversations as rows, the one last spoken in first. */
export function chatRows(chats: any, now: number): ChatRow[] {
  if (!chats || !Array.isArray(chats.list)) return [];
  return chats.list.map((c: any) => {
    const n = c.turns || 0;
    return { id: c.id, title: c.title || '(no name yet)', here: c.id === chats.id,
             sub: n + (n === 1 ? ' exchange' : ' exchanges') +
               (c.updated ? ' · ' + fmtAgo(c.updated, now) + ' ago' : '') +
               (CHAT_WHERE[c.via] ? ' · ' + CHAT_WHERE[c.via] : '') };
  });
}

export function chatsCount(chats: any): string {
  return chats ? (chats.kept || 0) + ' KEPT' : '';
}
