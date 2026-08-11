/* Unit tests for the panel's pure logic — the parts that decide what a number
   means before anything is drawn. No DOM, no browser, no network: run with
   `node tests/test_deck_ui.mjs`, or via tests/test_deck_ui.sh which skips
   cleanly when node is not installed.

   These cover the behaviours that have actually gone wrong on this panel:
   a trail drawn across a relocate, a sparkline carrying a value through an
   outage, and an ETE invented from a ground speed too small to divide by. */

import assert from 'node:assert/strict';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const UI = path.join(here, '..', 'pi', 'deck-ui', 'js');

const fmt = await import(pathToFileURL(path.join(UI, 'format.js')));
const geo = await import(pathToFileURL(path.join(UI, 'geo.js')));
const { createSeries } = await import(pathToFileURL(path.join(UI, 'series.js')));
const sim = await import(pathToFileURL(path.join(UI, 'simmath.js')));
const descent = await import(pathToFileURL(path.join(UI, 'descent.js')));
const spd = await import(pathToFileURL(path.join(UI, 'speedprofile.js')));
const nav = await import(pathToFileURL(path.join(UI, 'nav.js')));

let passed = 0;
const failures = [];
function test(name, fn) {
  try { fn(); passed++; } catch (e) { failures.push(name + '\n     ' + e.message); }
}

/* ── format ─────────────────────────────────────────────────────────────── */

test('fmtDur reads as seconds below a minute and m:ss above', () => {
  assert.equal(fmt.fmtDur(0), '0 s');
  assert.equal(fmt.fmtDur(37), '37 s');
  assert.equal(fmt.fmtDur(60), '1:00');
  assert.equal(fmt.fmtDur(95), '1:35');
  assert.equal(fmt.fmtDur(-5), '0 s', 'negative durations clamp rather than print a minus');
});

test('fmtAgo coarsens as the gap grows', () => {
  const now = 1_000_000;
  assert.equal(fmt.fmtAgo(0, now), '—', 'no timestamp is unknown, not "now"');
  assert.equal(fmt.fmtAgo(now - 30, now), '30 s');
  assert.equal(fmt.fmtAgo(now - 600, now), '10 m');
  assert.equal(fmt.fmtAgo(now - 7200, now), '2 h 0 m');
  assert.equal(fmt.fmtAgo(now - 86400 * 3, now), '3 d');
});

test('fmtVal keeps a missing reading distinct from zero', () => {
  assert.equal(fmt.fmtVal(null, '°C'), '—');
  assert.equal(fmt.fmtVal(undefined, '°C'), '—');
  assert.equal(fmt.fmtVal(0, '°C'), '0 °C', 'zero is a reading and must render as one');
  assert.equal(fmt.fmtVal(47.44, '°C'), '47.4 °C');
  assert.equal(fmt.fmtVal(1234.6, 'rpm'), '1235 rpm', 'past 100 the decimal is noise');
});

test('fmtTime pads seconds', () => {
  assert.equal(fmt.fmtTime(0), '0:00');
  assert.equal(fmt.fmtTime(65), '1:05');
  assert.equal(fmt.fmtTime(null), '');
});

test('fmtEte refuses to divide by a ground speed too small to mean anything', () => {
  assert.equal(fmt.fmtEte(100, 0), 'ETE — · 0 kt');
  assert.equal(fmt.fmtEte(100, 3), 'ETE — · 3 kt');
  assert.equal(fmt.fmtEte(100, 120), 'ETE 50 min');
  assert.equal(fmt.fmtEte(600, 120), 'ETE 5.0 h', 'past 90 min it switches to hours');
});

/* ── geo ────────────────────────────────────────────────────────────────── */

test('distBrg gets the cardinal directions right', () => {
  const north = geo.distBrg(28.0, -81.0, 29.0, -81.0);
  assert.ok(Math.abs(north.dist - 60) < 0.5, 'a degree of latitude is 60 nm');
  assert.ok(Math.abs(north.brg - 0) < 0.5 || Math.abs(north.brg - 360) < 0.5);
  assert.ok(Math.abs(geo.distBrg(28, -81, 28, -80).brg - 90) < 0.5, 'east');
  assert.ok(Math.abs(geo.distBrg(28, -81, 27, -81).brg - 180) < 0.5, 'south');
  assert.ok(Math.abs(geo.distBrg(28, -81, 28, -82).brg - 270) < 0.5, 'west');
});

test('longitude compresses with latitude', () => {
  const equator = geo.distBrg(0, 0, 0, 1).dist;
  const florida = geo.distBrg(28, -81, 28, -80).dist;
  assert.ok(florida < equator, 'a degree of longitude is shorter away from the equator');
  assert.ok(Math.abs(florida - 60 * Math.cos(28 * Math.PI / 180)) < 0.5);
});

test('world projection is monotonic and inverts latitude', () => {
  const a = geo.world(28, -81, 12), b = geo.world(28, -80, 12);
  assert.ok(b.x > a.x, 'east is right');
  assert.ok(geo.world(29, -81, 12).y < a.y, 'north is up, so y decreases');
  assert.ok(geo.world(28, -81, 13).x > a.x, 'a deeper zoom is a bigger world');
});

test('clampZoom holds the manual range', () => {
  assert.equal(geo.clampZoom(3), geo.ZMIN);
  assert.equal(geo.clampZoom(99), geo.ZMAX);
  assert.equal(geo.clampZoom(12), 12);
});

test('a pinch of double the span is exactly one zoom level', () => {
  // The gesture measures against the span at touchdown: z = z0 + log2(now/then)
  const z0 = 11, step = (ratio) => geo.clampZoom(Math.round(z0 + Math.log(ratio) / Math.LN2));
  assert.equal(step(1), 11);
  assert.equal(step(2), 12);
  assert.equal(step(4), 13);
  assert.equal(step(0.5), 10);
  assert.equal(step(1000), geo.ZMAX, 'and it stops at the limit rather than running away');
});

test('ATC handoff distinguishes staging, swapping, and already-active', () => {
  assert.deepEqual(sim.atcHandoff(124.85, { act: 126.725, sby: 121.9 }),
    { frequency: 124.85, action: 'stage' });
  assert.deepEqual(sim.atcHandoff(124.85, { act: 126.725, sby: 124.85 }),
    { frequency: 124.85, action: 'swap' });
  assert.equal(sim.atcHandoff(126.725, { act: 126.725, sby: 124.85 }), null,
    'no prompt when COM1 is already tuned');
  assert.equal(sim.atcHandoff(null, { act: 126.725, sby: 124.85 }), null,
    'no prompt without a valid simulator handoff');
});

test('isTeleport separates a relocate from a slow poll, not a big number', () => {
  const orlando = { lat: 28.43, lon: -81.31 };
  const seattle = { lat: 47.45, lon: -122.31 };
  const northOfOrlando = { lat: 29.43, lon: -81.31 };   // 60 nm away

  assert.equal(geo.isTeleport(orlando, seattle, 2000), true,
    'a continental jump in two seconds is a relocate');
  assert.equal(geo.isTeleport(orlando, northOfOrlando, 1200_000), false,
    '60 nm in twenty minutes is a backgrounded tab, and its trail is real');
  assert.equal(geo.isTeleport(orlando, northOfOrlando, 2000), true,
    'the same 60 nm in two seconds is not');
  assert.equal(geo.isTeleport(null, orlando, 2000), false, 'no previous fix, nothing to compare');
});

test('isStaleGpsLeg rejects a departure fix left hundreds of miles behind', () => {
  const jfk = { lat: 40.64232, lon: -73.76972 };
  const collapsed = { prev: jfk, next: jfk };
  assert.equal(geo.isStaleGpsLeg(collapsed, { lat: 42.04728, lon: -88.39906 }), true,
    'the live CJ4 failure: both fixes at JFK while the aircraft is near Chicago');
  assert.equal(geo.isStaleGpsLeg(collapsed, { lat: 40.7, lon: -73.8 }), false,
    'a collapsed first leg is legitimate while still near departure');
  assert.equal(geo.isStaleGpsLeg({ prev: jfk, next: { lat: 41.0, lon: -75.0 } },
    { lat: 42.0, lon: -88.0 }), false, 'a real leg is not rejected merely for being distant');
});

test('isStaleGpsLeg rejects the live short KJFK departure leg left behind', () => {
  const gps = {
    index: 1,
    prev: { lat: 40.63993, lon: -73.77869 },
    next: { lat: 40.6258, lon: -73.78269 },
  };
  assert.equal(geo.isStaleGpsLeg(gps, { lat: 40.54714, lon: -74.80705 }), true,
    'a sub-mile departure leg 50 nm behind must not pull NAV back to JFK');
  assert.equal(geo.isStaleGpsLeg(gps, { lat: 40.64, lon: -73.8 }), false,
    'the same leg remains valid while the aircraft is still near departure');
});

test('a cruising aircraft never trips the teleport threshold', () => {
  // 500 kt for two seconds, sampled at the worst case of a whole poll late.
  const nm = 500 * (2 / 3600);
  const from = { lat: 28, lon: -81 };
  const to = { lat: 28 + nm / 60, lon: -81 };
  assert.equal(geo.isTeleport(from, to, 2000), false);
});

/* ── descent advisory ──────────────────────────────────────────────────── */

const todRoute = [{ name: 'DEST', lat: 0, lon: 2 }]; // 120 nm due east
const todAircraft = { altitudeFt: 30000, lat: 0, lon: 0,
  groundspeedKt: 380, verticalSpeedFpm: 0, selectedAltitudeFt: 3000 };

test('TOD uses geometric profile plus buffer against the existing route', () => {
  const d = descent.calculateDescent({ aircraft: todAircraft, route: todRoute,
    config: { targetFt: 3000, angleDeg: 3, bufferNm: 10 } });
  assert.equal(d.state, 'cruise');
  assert.ok(Math.abs(d.descentDistanceNm - 84.8) < 0.2);
  assert.ok(Math.abs(d.requiredDistanceNm - 94.8) < 0.2);
  assert.ok(Math.abs(d.distanceToTodNm - 25.2) < 0.3);
  assert.ok(Math.abs(d.timeToTodMin - 4.0) < 0.1);
  assert.ok(Math.abs(d.requiredVsFpm + 2016) < 2);
  assert.equal(d.targetSource, 'terminal_default');
});

test('TOD profile angle changes distance and required vertical speed', () => {
  const shallow = descent.calculateDescent({ aircraft: todAircraft, route: todRoute,
    config: { angleDeg: 2.5 } });
  const steep = descent.calculateDescent({ aircraft: todAircraft, route: todRoute,
    config: { angleDeg: 3.5 } });
  assert.ok(shallow.requiredDistanceNm > steep.requiredDistanceNm);
  assert.ok(Math.abs(shallow.requiredVsFpm) < Math.abs(steep.requiredVsFpm));
});

test('TOD selects the first real remaining altitude constraint', () => {
  const route = [
    { name: 'BASE', lat: 0, lon: 1.5, altitude_ft: 5000 },
    { name: 'FINAL', lat: 0, lon: 1.8, altitude_ft: 3000 },
    { name: '10L', lat: 0, lon: 2 },
  ];
  const d = descent.calculateDescent({ aircraft: todAircraft, route });
  assert.equal(d.targetName, 'BASE');
  assert.equal(d.targetAltitudeFt, 5000);
  assert.equal(d.targetSource, 'constraint');
  const afterBase = descent.calculateDescent({ aircraft: todAircraft, route, activeIndex: 1 });
  assert.equal(afterBase.targetName, 'FINAL');
});

test('TOD never invents a waypoint constraint', () => {
  const d = descent.calculateDescent({ aircraft: todAircraft,
    route: [{ name: 'BASE', lat: 0, lon: 1 }, { name: 'FINAL', lat: 0, lon: 2 }] });
  assert.equal(d.targetName, 'FINAL');
  assert.equal(d.targetAltitudeFt, 3000);
  assert.equal(d.targetSource, 'terminal_default');
});

test('manual TOD target overrides route constraints without changing the route', () => {
  const route = [{ name: 'BASE', lat: 0, lon: 1.5, altitude_ft: 5000 }];
  const d = descent.calculateDescent({ aircraft: todAircraft, route,
    config: { targetMode: 'manual', targetFt: 7000 } });
  assert.equal(d.targetAltitudeFt, 7000);
  assert.equal(d.targetSource, 'manual');
  assert.equal(route[0].altitude_ft, 5000);
});

test('TOD state thresholds cover cruise through missed', () => {
  function at(lon) {
    return descent.calculateDescent({ aircraft: { ...todAircraft, lon: lon }, route: todRoute });
  }
  assert.equal(at(-1).state, 'cruise');
  assert.equal(at(0.2).state, 'approaching');
  assert.equal(at(0.42).state, 'imminent');
  assert.equal(at(0.5).state, 'tod_now');
  assert.equal(at(0.7).state, 'missed');
});

test('TOD reports complete when no descent is required', () => {
  const d = descent.calculateDescent({
    aircraft: { ...todAircraft, altitudeFt: 3050 }, route: todRoute });
  assert.equal(d.state, 'complete');
  assert.equal(d.altitudeToLoseFt, 50);
});

test('TOD stays unavailable during takeoff and climb', () => {
  for (const flightPhase of ['TAKEOFF', 'CLIMB']) {
    const d = descent.calculateDescent({
      aircraft: { ...todAircraft, altitudeFt: 5000, verticalSpeedFpm: 1200 },
      route: todRoute,
      flightPhase,
    });
    assert.equal(d.state, 'unavailable');
    assert.equal(d.reason, `during ${flightPhase.toLowerCase()}`);
    assert.equal(d.pathStatus, undefined);
  }
});

test('mission phase distinguishes takeoff from en-route climb', () => {
  assert.equal(nav.missionPhase({ on_ground: false, agl_ft: 800, vs_fpm: 1200 }, 40),
    'TAKEOFF');
  assert.equal(nav.missionPhase({ on_ground: false, agl_ft: 1800, vs_fpm: 1200 }, 40),
    'CLIMB');
  assert.equal(nav.missionPhase({ on_ground: false, agl_ft: 800, vs_fpm: -500 }, 5),
    'APPROACH');
});

test('descending path deviation uses quiet 300 and 750 foot bands', () => {
  function deviation(feet) {
    const gradient = Math.tan(3 * Math.PI / 180) * 6076.12;
    const remainingNm = 50;
    const pathAltitude = 3000 + (remainingNm - 10) * gradient;
    return descent.calculateDescent({
      aircraft: { altitudeFt: pathAltitude + feet, lat: 0, lon: 0,
        groundspeedKt: 300, verticalSpeedFpm: -1500 },
      route: [{ name: 'DEST', lat: 0, lon: remainingNm / 60 }],
    });
  }
  assert.equal(deviation(200).pathStatus, 'on_path');
  assert.equal(deviation(500).pathStatus, 'slightly_high');
  assert.equal(deviation(-500).pathStatus, 'slightly_low');
  assert.equal(deviation(900).pathStatus, 'high');
  assert.equal(deviation(-900).pathStatus, 'low');
});

test('TOD degrades without route or usable aircraft state', () => {
  assert.equal(descent.calculateDescent({ aircraft: todAircraft, route: [] }).state,
    'unavailable');
  assert.equal(descent.calculateDescent({ aircraft: {}, route: todRoute }).state,
    'unavailable');
});

test('cockpit warning prioritizes stall and reports observed overspeed IAS', () => {
  assert.deepEqual(sim.cockpitWarning({
    warnings: { overspeed: true }, readouts: { ias_kt: 332 },
  }), { kind: 'overspeed', label: 'OVERSPEED', detail: '332 KT' });
  assert.deepEqual(sim.cockpitWarning({
    warnings: { overspeed: true, stall: true }, readouts: { ias_kt: 90 },
  }), { kind: 'stall', label: 'STALL' });
  assert.deepEqual(sim.cockpitWarning({
    warnings: { engine_fire: [2], stall: true },
  }), { kind: 'engine_fire', label: 'ENGINE FIRE', detail: 'ENG 2' });
  assert.deepEqual(sim.cockpitWarning({
    warnings: { gear_damage: true, gear_speed_exceeded: true },
  }), { kind: 'gear_damage', label: 'GEAR DAMAGE' });
  assert.deepEqual(sim.cockpitWarning({
    warnings: { gear_warning: 'gear_up', overspeed: true },
  }), { kind: 'gear_warning', label: 'GEAR UP' });
  assert.deepEqual(sim.cockpitWarning({
    warnings: { gear_speed_exceeded: true },
  }), { kind: 'gear_overspeed', label: 'GEAR OVERSPEED' });
  assert.equal(sim.cockpitWarning({ warnings: {} }), null);
  assert.equal(sim.cockpitWarning({}), null);
});

/* ── series ─────────────────────────────────────────────────────────────── */

test('an absent key records a gap, never a zero or the last value', () => {
  const s = createSeries();
  s.push({ telemetry: { ts: 100, ws: { gpu_temp_c: 47 } } }, ['ws.gpu_temp_c']);
  s.push({ telemetry: { ts: 101, ws: {} } }, ['ws.gpu_temp_c']);   // mid-reboot
  const pts = s.points('ws.gpu_temp_c');
  assert.equal(pts.length, 2);
  assert.equal(pts[1].v, null, 'the machine was off; that is a hole, not a reading');
  assert.equal(s.latest('ws.gpu_temp_c').v, 47, 'but the last real reading is still findable');
});

test('a repeated timestamp is not a new sample', () => {
  const s = createSeries();
  assert.equal(s.push({ telemetry: { ts: 100, ws: { v: 1 } } }, ['ws.v']), true);
  assert.equal(s.push({ telemetry: { ts: 100, ws: { v: 2 } } }, ['ws.v']), false,
    'deck-api re-sending the same frame must not double-plot it');
  assert.equal(s.points('ws.v').length, 1);
});

test('samples older than the window are evicted', () => {
  const s = createSeries({ windowS: 60 });
  for (let t = 0; t <= 120; t += 10) s.record('k', t, t);
  const pts = s.points('k');
  assert.ok(pts[0].t >= 60, 'nothing older than the window survives');
  assert.equal(pts[pts.length - 1].t, 120);
});

test('the hard cap holds regardless of sample rate', () => {
  const s = createSeries({ maxPts: 10 });
  for (let i = 0; i < 500; i++) s.record('k', i, i);
  assert.equal(s.points('k').length, 10, 'a fast agent cannot grow this without bound');
  assert.equal(s.points('k')[9].v, 499, 'and it keeps the newest, not the oldest');
});

test('non-numeric readings become gaps', () => {
  const s = createSeries();
  s.record('k', 1, 'warm');
  s.record('k', 2, NaN);
  s.record('k', 3, Infinity);
  assert.deepEqual(s.points('k').map(p => p.v), [null, null, null]);
  assert.equal(s.latest('k'), null, 'a trace of nothing has no latest value');
});

/* ── sim steppers ───────────────────────────────────────────────────────── */

test('a flap tap resolves to an absolute detent, clamped at both ends', () => {
  assert.equal(sim.nextDetent(0, 1, 4), 1);
  assert.equal(sim.nextDetent(2, -1, 4), 1);
  assert.equal(sim.nextDetent(3, 1, 4), 3,
    'a + tap at full flap must NOT come round to clean on short final');
  assert.equal(sim.nextDetent(0, -1, 4), 0);
  assert.equal(sim.nextDetent(0, 1, 1), 0, 'an airframe with one position has nowhere to go');
  assert.equal(sim.nextDetent(1.0, 1, 4), 2, 'the sim reports the index as a double');
});

test('the heading bug wraps and the rest clamp', () => {
  const hdg = { min: 0, max: 359, wrap: true };
  assert.equal(sim.stepBug(355, 10, hdg), 5, '359 + 1 is 000, not 360');
  assert.equal(sim.stepBug(5, -10, hdg), 355);
  assert.equal(sim.stepBug(212, 10, hdg), 222);

  const alt = { min: 0, max: 60000 };
  assert.equal(sim.stepBug(59500, 1000, alt), 60000, 'clamped, not wrapped');
  assert.equal(sim.stepBug(500, -1000, alt), 0);

  const vs = { min: -8000, max: 8000 };
  assert.equal(sim.stepBug(-500, -500, vs), -1000, 'a descent bug goes further negative');
  assert.equal(sim.stepBug(-7900, -500, vs), -8000);
});

/* ── speed profiles ─────────────────────────────────────────── */

test('profiles match by title and unknown airframes get no advice', () => {
  assert.equal(spd.speedProfile('747-8i').name, '747');
  assert.equal(spd.speedProfile('Salty Boeing 747-8').name, '747');
  assert.equal(spd.speedProfile('Cessna CJ4 Citation').name, 'CJ4');
  assert.equal(spd.speedProfile('Cessna 152'), null, 'no profile beats a wrong one');
  assert.equal(spd.speedProfile(null), null);
});

test('the 747 climb schedule: 250 below 10k, 290 to transition, then Mach', () => {
  const p = spd.speedProfile('747-8i');
  assert.deepEqual(spd.speedTarget(p, 'CLIMB', 4000), { mode: 'KIAS', kt: 250 });
  assert.deepEqual(spd.speedTarget(p, 'TAKEOFF', 1200), { mode: 'KIAS', kt: 250 });
  assert.deepEqual(spd.speedTarget(p, 'CLIMB', 15000), { mode: 'KIAS', kt: 290 });
  assert.deepEqual(spd.speedTarget(p, 'CLIMB', 31000), { mode: 'MACH', mach: 0.85 });
  assert.deepEqual(spd.speedTarget(p, 'CRUISE', 35000), { mode: 'MACH', mach: 0.85 });
});

test('the descent schedule mirrors: Mach, then 280, then 250', () => {
  const p = spd.speedProfile('747-8i');
  assert.deepEqual(spd.speedTarget(p, 'DESCENT', 35000), { mode: 'MACH', mach: 0.85 });
  assert.deepEqual(spd.speedTarget(p, 'DESCENT', 20000), { mode: 'KIAS', kt: 280 });
  assert.deepEqual(spd.speedTarget(p, 'DESCENT', 8000), { mode: 'KIAS', kt: 250 });
  assert.deepEqual(spd.speedTarget(p, 'APPROACH', 3000), { mode: 'KIAS', kt: 250 });
});

test('the schedule is silent on the ground and without an altitude', () => {
  const p = spd.speedProfile('747-8i');
  assert.equal(spd.speedTarget(p, 'TAXI', 600), null);
  assert.equal(spd.speedTarget(p, 'RAMP', 600), null);
  assert.equal(spd.speedTarget(p, null, 600), null);
  assert.equal(spd.speedTarget(p, 'CLIMB', undefined), null);
  assert.equal(spd.speedTarget(null, 'CLIMB', 5000), null);
});

test('deviation is signed, and never invented from a reading the mode lacks', () => {
  const kias = { mode: 'KIAS', kt: 290 };
  assert.deepEqual(spd.speedDeviation(kias, 171, undefined), { off: -119, unit: 'KT' });
  assert.deepEqual(spd.speedDeviation(kias, 305, undefined), { off: 15, unit: 'KT' });
  const mach = { mode: 'MACH', mach: 0.85 };
  assert.deepEqual(spd.speedDeviation(mach, 250, 0.81), { off: -0.04, unit: 'MACH' });
  assert.equal(spd.speedDeviation(mach, 250, undefined), null,
    'an old agent sends no mach — no deviation, not a lie from IAS');
});

if (failures.length) {
  console.error(`FAIL: deck-ui pure logic (${failures.length} of ${passed + failures.length})`);
  failures.forEach(f => console.error('  - ' + f));
  process.exit(1);
}
console.log(`deck-ui pure logic tests passed (${passed} assertions groups)`);
