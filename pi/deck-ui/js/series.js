/* series.js — the rolling telemetry window.

   This buffer is the ONLY history that exists anywhere in Flight Deck. deck-api
   sends the latest reading and keeps nothing; the Pi boots off a thumb drive and
   nothing here is written to it. The window dies with the page, on purpose.

   Built as a factory over plain objects so it can be exercised in node without
   a DOM: the panel's history behaviour is testable, which matters because a
   silent gap and a carried-forward value look identical once drawn. */

export const WINDOW_S = 3600;    // 60 min
export const MAX_PTS = 1200;     // hard cap regardless of sample rate

export function createSeries(opts) {
  opts = opts || {};
  const windowS = opts.windowS === undefined ? WINDOW_S : opts.windowS;
  const maxPts = opts.maxPts === undefined ? MAX_PTS : opts.maxPts;
  const data = Object.create(null);
  let lastTs = 0;

  /** Append one sample, then evict anything outside the window or over the cap. */
  function record(key, t, v) {
    const a = data[key] || (data[key] = []);
    a.push({ t: t, v: (typeof v === 'number' && isFinite(v)) ? v : null });
    const cut = t - windowS;
    let drop = 0;
    while (drop < a.length && a[drop].t < cut) drop++;
    if (drop) a.splice(0, drop);
    if (a.length > maxPts) a.splice(0, a.length - maxPts);
  }

  /**
   * Take one telemetry frame for the given keys. Returns false when the frame
   * carries no new timestamp, so callers can skip a redraw.
   *
   * A key absent from this frame records a null, not a zero and not the
   * previous value: the machine was off or mid-reboot, and a sparkline that
   * carried the last reading across a reboot would draw a flat line through an
   * outage that never happened.
   */
  function push(frame, keys) {
    const tel = (frame && frame.telemetry) || {};
    if (!tel.ts || tel.ts === lastTs) return false;
    lastTs = tel.ts;
    keys.forEach(function (key) {
      const parts = key.split('.');
      const src = tel[parts[0]] || {};
      record(key, tel.ts, parts[1] in src ? src[parts[1]] : null);
    });
    return true;
  }

  /** Most recent non-null sample for a key, or null if the trace has none. */
  function latest(key) {
    const a = data[key];
    if (!a) return null;
    for (let i = a.length - 1; i >= 0; i--) if (a[i].v !== null) return a[i];
    return null;
  }

  function points(key) { return data[key] || []; }

  return { record, push, latest, points };
}
