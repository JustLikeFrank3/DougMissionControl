/* geo.js — the map's arithmetic. Pure functions over numbers, so the moving
   map's behaviour can be tested without a canvas, a tile server or a sim. */

export const NM_PER_DEG = 60;
export const TILE = 256;

/** Manual zoom range. The auto-fit stops at 15; manual goes one closer, which
    is the level that shows taxiways. */
export const ZMIN = 8;
export const ZMAX = 16;

/** Implied ground speed above which the aircraft did not fly there, it was put
    there — a relocate, a new departure point, a repositioned flight. Comfortably
    above anything the sim's aircraft do and orders below what a continental jump
    implies, so it is a threshold rather than a tuning knob. */
export const TELEPORT_KT = 1500;

/**
 * Local flat projection in nautical miles around a reference point. Exact
 * enough for a wall map at the distances this flies, and vastly cheaper than
 * real geodesy on a Pi.
 */
export function xy(lat, lon, lat0, lon0) {
  var k = Math.cos(lat0 * Math.PI / 180);
  return { x: (lon - lon0) * NM_PER_DEG * k, y: (lat - lat0) * NM_PER_DEG };
}

/** Distance in nm and true bearing in degrees from point 1 to point 2. */
export function distBrg(lat1, lon1, lat2, lon2) {
  var p = xy(lat2, lon2, lat1, lon1);
  return {
    dist: Math.sqrt(p.x * p.x + p.y * p.y),
    brg: (Math.atan2(p.x, p.y) * 180 / Math.PI + 360) % 360,
  };
}

/** Web-Mercator world pixel coordinates at a slippy zoom level. */
export function world(lat, lon, z) {
  var n = TILE * Math.pow(2, z);
  var rad = lat * Math.PI / 180;
  return {
    x: (lon + 180) / 360 * n,
    y: (1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2 * n,
  };
}

export function clampZoom(z) {
  return Math.max(ZMIN, Math.min(ZMAX, z));
}

/**
 * Did the aircraft fly between these two fixes, or was it put there?
 *
 * Judged on IMPLIED GROUND SPEED, never raw distance. A backgrounded tab or a
 * missed poll produces a legitimately large gap, and a distance threshold would
 * throw away a real trail because of it. Dividing by elapsed time separates the
 * two: real flight stays under a few hundred knots however long the gap, while
 * a relocate across a continent implies hundreds of thousands.
 */
export function isTeleport(prev, next, elapsedMs) {
  if (!prev || !next) return false;
  var moved = distBrg(prev.lat, prev.lon, next.lat, next.lon).dist;
  var hours = Math.max(elapsedMs, 1000) / 3600000;
  return moved / hours > TELEPORT_KT;
}

/**
 * The integer slippy zoom whose scale best fits `spanNm` across `px` pixels.
 * The map steps between whole levels as a flight closes in, the way any slippy
 * map does, rather than scaling smoothly to a blurry in-between.
 */
export function zoomForSpan(spanNm, px, lat, max) {
  var mPerPx = spanNm * 1852 / px;
  var z = Math.floor(
    Math.log(156543.03 * Math.cos(lat * Math.PI / 180) / mPerPx) / Math.LN2);
  return Math.max(ZMIN, Math.min(max === undefined ? 15 : max, z));
}
