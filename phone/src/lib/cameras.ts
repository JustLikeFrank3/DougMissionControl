/* cameras.ts — DougRing in the app: the pure part of
   pi/deck-ui/js/cameras.js. */

import { fmtAgo } from './format';

/** A time in milliseconds as "… ago", or "never". */
export function ago(ms: number | null | undefined, now?: number): string {
  return ms ? fmtAgo(ms / 1000, now) + ' ago' : 'never';
}

/** What the picture is, in one line: camera, when, how old, and whether it
    is of now. */
export function obsLine(o: any, now?: number): string {
  if (!o) return '';
  const taken = o.captured_ms ? new Date(o.captured_ms) : null;
  const age = o.captured_ms ? Math.max(0, Math.round(((now || Date.now()) - o.captured_ms) / 1000)) : null;
  const live = o.source === 'live-triggered' && age != null && age < 120;
  return (o.camera_name || 'camera') + ' · ' +
    (taken ? 'taken ' + taken.toLocaleTimeString() + ' (' + age + ' s old)' : 'time unknown') + ' · ' +
    (live ? 'recorded now, on request' : 'NOT LIVE: the latest picture Ring had');
}

/** The line under a camera's name. */
export function cameraSub(c: any): string {
  return (c.online ? 'online' : 'OFFLINE') + (c.battery ? ' · battery' : '') + (c.doorbell ? ' · doorbell' : '') +
    (c.ring_name && c.ring_name !== c.name ? ' · Ring calls it ' + c.ring_name : '');
}

/** The link to Ring and the service's health, as [label, text, bad] rows. */
export function healthRows(st: any, now?: number): [string, string, boolean][] {
  const acct = (st.accounts || [])[0];
  const h = st.health || {}, t = st.telemetry || {};
  const rows: [string, string][] = [
    ['Ring link', acct ? acct.email + ' · ' + (acct.state === 'linked' ? 'linked' : 'LINK AGAIN') +
      ' · token refreshed ' + ago((acct.refreshed_at || 0) * 1000, now) : 'not linked'],
    ['last picture', ago(h.last_success, now)],
    ['last access', ago(h.last_access, now)],
    ['last error', h.last_error ? h.last_error.status + ' ' + (h.last_error.code || '') + ', ' +
      ago(h.last_error.at, now) : 'none'],
    ['service', 'DougRing ' + (st.version || '') + ' · ' + (t.pictures || 0) + ' pictures · ' + (t.ring_calls || 0) +
      ' calls to Ring · ' + (t.ring_errors || 0) + ' errors' +
      (st.throttled_for_s ? ' · Ring asked to wait ' + st.throttled_for_s + ' s' : '')],
  ];
  return rows.map((r) => [r[0], r[1], /AGAIN|^[45]\d\d /.test(r[1])]);
}

/** One line of the journal of who looked. */
export function journalLine(j: any, names: Record<string, string>, now?: number): { text: string; bad: boolean } {
  return {
    text: ago(j.at, now) + ' · ' + (j.role === 'douglas' ? 'Douglas' : 'you') + ' · ' + j.what +
      (j.camera ? ' · ' + (names[j.camera] || String(j.camera).slice(-6)) : '') +
      (j.result && j.result !== 'ok' ? ' · ' + j.result : '') +
      (j.source ? ' · ' + j.source + (j.age_s != null ? ', ' + j.age_s + ' s old' : '') : ''),
    bad: /^(refused|failed)/.test(j.result || ''),
  };
}
