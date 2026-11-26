/* format.ts — every string the app shows that is not a raw value.
   A port of pi/deck-ui/js/format.js, kept the same so the phone and the
   panel word a reading the same way. Pure: no clock of its own except where
   a caller passes one in. */

/** Seconds as "m:ss" once past a minute, plain seconds below it. */
export function fmtDur(s: number | null | undefined): string {
  const n = Math.max(0, Math.round(s || 0));
  const m = Math.floor(n / 60);
  return m ? m + ':' + String(n % 60).padStart(2, '0') : n + ' s';
}

/** How long ago a unix timestamp was, in the coarsest unit that still reads
    honestly. `now` is injectable so this can be tested without a clock. */
export function fmtAgo(ts: number | null | undefined, now?: number): string {
  if (!ts) return '—';
  const s = Math.max(0, Math.round((now === undefined ? Date.now() / 1000 : now) - ts));
  if (s < 90) return s + ' s';
  const m = Math.floor(s / 60);
  if (m < 90) return m + ' m';
  const h = Math.floor(m / 60);
  return h < 48 ? h + ' h ' + (m % 60) + ' m' : Math.floor(h / 24) + ' d';
}

/** Wall clock, 24h, zero-padded. */
export function fmtClock(ts?: number | null): string {
  const d = new Date((ts || Date.now() / 1000) * 1000);
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}

/** Track position/duration as "m:ss". */
export function fmtTime(s: number | null | undefined): string {
  if (!s && s !== 0) return '';
  return Math.floor(s / 60) + ':' + ('0' + (Math.floor(s) % 60)).slice(-2);
}

/** Elapsed milliseconds as "m:ss", for a count under the ask box. */
export function clock(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2);
}

/** A number to `dp` places, or null when there is no reading: a missing
    reading is a dash on the screen, never a zero. */
export function num(v: unknown, dp: number): string | null {
  return v == null || typeof v !== 'number' || !isFinite(v) ? null : v.toFixed(dp);
}

export function gb(b: number | null | undefined): number | null {
  return b == null ? null : b / 1073741824;
}
