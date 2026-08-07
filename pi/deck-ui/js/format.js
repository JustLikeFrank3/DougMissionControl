/* format.js — every string the panel renders that is not a raw value.
   Pure: no DOM, no clock of its own except where a caller passes one in, so
   all of it is testable in node without a browser. */

/** Seconds as "m:ss" once past a minute, plain seconds below it. */
export function fmtDur(s) {
  s = Math.max(0, Math.round(s || 0));
  var m = Math.floor(s / 60);
  return m ? m + ':' + String(s % 60).padStart(2, '0') : s + ' s';
}

/** How long ago a unix timestamp was, in the coarsest unit that still reads
    honestly. `now` is injectable so this can be tested without faking a clock. */
export function fmtAgo(ts, now) {
  if (!ts) return '—';
  var s = Math.max(0, Math.round((now === undefined ? Date.now() / 1000 : now) - ts));
  if (s < 90) return s + ' s';
  var m = Math.floor(s / 60);
  if (m < 90) return m + ' m';
  var h = Math.floor(m / 60);
  return h < 48 ? h + ' h ' + (m % 60) + ' m' : Math.floor(h / 24) + ' d';
}

/** Wall clock, 24h, zero-padded. */
export function fmtClock(ts) {
  var d = new Date((ts || Date.now() / 1000) * 1000);
  return String(d.getHours()).padStart(2, '0') + ':' +
         String(d.getMinutes()).padStart(2, '0');
}

/** A metric with its unit. Null and undefined are an em dash, never a zero —
    a missing reading is not the same fact as a reading of nothing. */
export function fmtVal(v, unit) {
  if (v === null || v === undefined) return '—';
  var s = Math.abs(v) >= 100 ? Math.round(v) : Math.round(v * 10) / 10;
  return s + (unit ? ' ' + unit : '');
}

/** Track position/duration as "m:ss". */
export function fmtTime(s) {
  if (!s && s !== 0) return '';
  return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2);
}

/** Estimated time en route. Below 5 kt there is no honest estimate — dividing
    by a ground speed that small produces hours of nonsense — so it says so. */
export function fmtEte(nm, gs) {
  if (!gs || gs < 5) return 'ETE — · ' + gs + ' kt';
  var min = nm / gs * 60;
  return 'ETE ' + (min >= 90 ? (min / 60).toFixed(1) + ' h' : Math.round(min) + ' min');
}
