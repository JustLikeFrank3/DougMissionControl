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
const mem = await import(pathToFileURL(path.join(UI, 'memory.js')));
const fleet = await import(pathToFileURL(path.join(UI, 'fleet.js')));
const ntc = await import(pathToFileURL(path.join(UI, 'notices.js')));
const own = await import(pathToFileURL(path.join(UI, 'owner.js')));
const convo = await import(pathToFileURL(path.join(UI, 'convo.js')));

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

/* ── what Douglas remembers ─────────────────────────────────────────────── */

test('a memory says where it came from, how old it is and how sure, in that order', () => {
  const day = 86400, now = 2000000000;
  assert.equal(mem.provenance({ source: 'said', via: 'phone', updated: now - 3 * day,
                                confidence: 0.8, uses: 4 }, now),
               'you said so · phone · 3 d ago · sure 80% · used 4×');
  assert.equal(mem.provenance({ source: 'owner', via: 'deck', updated: now - 30, confidence: 1 }, now),
               'you set this · deck · 30 s ago · sure 100%');
  assert.equal(mem.provenance({ source: 'inferred', updated: now - 7200, confidence: 0.55 }, now),
               'he worked it out · 2 h 0 m ago · sure 55%',
               'a guess of his is called one, and never rounded up to certain');
  assert.equal(mem.provenance({ source: 'observed', updated: now - 60, confidence: 0.9,
                                done: now - 60 }, now),
               'he saw it happen · 60 s ago · sure 90% · done 60 s ago');
  assert.equal(mem.provenance({ source: 'elsewhere', updated: now, confidence: null }, now),
               'elsewhere · 0 s ago · sure 0%',
               'a source this page does not know is shown as it came, not dressed up');
});

test('what happened to a memory earlier keeps the words it had then', () => {
  const now = 2000000000, m = { text: 'Sam takes his coffee with milk.' };
  assert.equal(mem.pastLine({ action: 'corrected', who: 'owner', at: now - 120,
                              before: 'Sam takes his coffee black.' }, m, now),
               'corrected by you · 2 m ago · was: Sam takes his coffee black.');
  assert.equal(mem.pastLine({ action: 'restored', who: 'owner', at: now - 60, before: m.text }, m, now),
               'brought back by you · 60 s ago', 'the same words are not repeated as what it was');
  assert.equal(mem.pastLine({ action: 'kept over a weaker note', who: 'douglas', at: now - 60,
                              after: 'Sam takes tea.' }, m, now),
               'kept over a weaker note by him · 60 s ago · the note: Sam takes tea.');
  assert.equal(mem.pastLine({ action: 'let go for room', who: 'memory', at: now - 60 }, m, now),
               'let go for room by the memory itself · 60 s ago');
});

/* ── what the machines report ───────────────────────────────────────────── */

test('services: how many should be up and are, what is down, and never a fault made of one switched off', () => {
  const u = (unit, state, more) => Object.assign({ unit, state, restarts: 0 }, more);
  assert.deepEqual(fleet.servicesLine([u('a', 'active'), u('b', 'active')]),
                   { text: '2 of 2 services up', bad: false, known: true });
  assert.deepEqual(fleet.servicesLine([u('a', 'active'), u('eyes', 'inactive', { enabled: 'disabled' })]),
                   { text: '1 of 1 services up · off: eyes', bad: false, known: true },
                   'switched off on purpose is not down');
  assert.deepEqual(fleet.servicesLine([u('a', 'active', { restarts: 14 }), u('b', 'failed'),
                                       u('c', 'inactive', { enabled: 'enabled' })]),
                   { text: '1 of 3 services up · DOWN: b, c · restarted: a 14×', bad: true, known: true },
                   'one that is set to run and is not running is down');
  assert.deepEqual(fleet.servicesLine(null), { text: 'services not readable', bad: false, known: false },
                   'not asked is not the same as none, and is not a fault either');
  assert.equal(fleet.servicesLine([]).text, '');
});

test('a machine line carries only what was read, and says when it is old', () => {
  const now = 2000000000;
  assert.deepEqual(fleet.machineLine({ id: 'node1', up: true, as_of: now, services: [
    { unit: 'k3s', state: 'active', restarts: 14 }],
    reports: { temp_c: 51.6, load: 0.41, disk: { total_bytes: 28e9, used_bytes: 21e9 } } }, now),
    { text: '52 °C · load 0.41 · disk 75 % · 1 of 1 services up · restarted: k3s 14×', bad: false });
  assert.deepEqual(fleet.machineLine({ id: 'douglas', up: true, as_of: now - 400, services: [
    { unit: 'voice', state: 'failed', restarts: 0 }],
    reports: { host: { temp_c: 40.2, power_w: 48.3 }, disks: [{ total_bytes: 2e12, used_bytes: 3e11 }] } }, now),
    { text: '40 °C · 48 W · disk 15 % · 0 of 1 services up · DOWN: voice · 6 m old', bad: true });
  assert.deepEqual(fleet.machineLine({ id: 'workstation', up: true, as_of: now,
    reports: { gpu_temp_c: 47, gpu_util_pct: 0 } }, now), { text: 'GPU 47 °C · GPU 0 %', bad: false },
    'a reading of zero is a reading');
  assert.deepEqual(fleet.machineLine({ id: 'dougvoice', up: true, doors: {} }, now), { text: '', bad: false },
    'a machine that reports nothing gets no line, not a row of dashes or zeros');
  assert.deepEqual(fleet.machineLine({ id: 'workstation', up: false, reports: { gpu_temp_c: 47 } }, now),
    { text: '', bad: false }, 'a machine that is off is not shown its last numbers');
  assert.equal(fleet.machineLine({ up: true, reports: { temp_c: null, load: undefined } }, now).text, '');
  assert.equal(fleet.diskPct({ total_bytes: 0, used_bytes: 5 }), null);
  assert.equal(fleet.diskPct(null), null);
});

/* ── an answer, and the switches ─────────────────────────────────────────── */

test('how long a change still waits, and that one whose time is up is not said to wait', () => {
  assert.equal(own.left(1000 + 130, 1000), '2 m 10 s left');
  assert.equal(own.left(1000 + 59.9, 1000), '59 s left');
  assert.equal(own.left(1000, 1000), 'no longer waiting');
  assert.equal(own.left(900, 1000), 'no longer waiting');
  assert.equal(own.left(undefined, 1000), 'no longer waiting', 'no time known is not time left');
});

test('what is waiting says what it is and where it was asked for', () => {
  assert.equal(own.waitingHead({ kind: 'push', via: 'webui' }), 'A PUSH · asked on the chat page');
  assert.equal(own.waitingHead({ kind: 'forget', via: 'phone' }), 'FORGETTING · asked on the phone');
  assert.equal(own.waitingHead({ kind: 'something new', via: 'elsewhere' }), 'A CHANGE · asked on another line');
  assert.equal(own.waitingHead(null), 'A CHANGE · asked on another line');
});

test('the job in his workshop is a line: under way and for how long, or ended and how long ago', () => {
  assert.equal(own.jobLine({ running: true, began: 1000 - 240 }, 1000), 'IN THE WORKSHOP · under way for 4 m');
  assert.equal(own.jobLine({ running: true }, 1000), 'IN THE WORKSHOP · under way');
  assert.equal(own.jobLine({ running: false, ended: 1000 - 720 }, 1000), 'IN THE WORKSHOP · ended 12 m ago');
  assert.equal(own.jobLine({ running: false }, 1000), 'IN THE WORKSHOP · ended a while ago');
  assert.equal(own.jobLine(null, 1000), '', 'no job is no line');
});

test('a job handed requirements says how many are done, and one that ended with some left is not shown as done', () => {
  const needs = [{ n: 1, text: 'Retry the fetch', accept: 'it retries twice', state: 'done', note: 'server.py' },
                 { n: 2, text: 'Add a test', accept: 'a test passes', state: 'outstanding', note: 'not written' },
                 { n: 3, text: 'Publish it', accept: '', state: 'blocked', note: 'no network' }];
  const job = { running: false, ended: 1000 - 720, report: { needs } };
  assert.equal(own.needsLine(job.report), '1 OF 3 DONE · 1 OUTSTANDING · 1 BLOCKED');
  assert.equal(own.jobLine(job, 1000), 'IN THE WORKSHOP · ended 12 m ago · 1 OF 3 DONE · 1 OUTSTANDING · 1 BLOCKED');
  assert.equal(own.jobShort(job), true);
  const all = { running: false, ended: 900, report: { needs: needs.map(n => ({ ...n, state: 'done' })) } };
  assert.equal(own.needsLine(all.report), '3 OF 3 DONE');
  assert.equal(own.jobShort(all), false);
  const going = { running: true, began: 760, report: { needs: needs.map(n => ({ ...n, state: 'pending' })) } };
  assert.equal(own.jobLine(going, 1000), 'IN THE WORKSHOP · under way for 4 m · 3 REQUIREMENTS');
  assert.equal(own.jobShort(going), false, 'a job under way has fallen short of nothing yet');
  assert.equal(own.needsLine({ needs: [] }), '');
  assert.equal(own.needsLine(null), '');
  assert.equal(own.jobShort({ running: false, report: null }), false, 'a job handed none is as it was');
});

test('what a job was handed and what became of it are rows: its words, its requirements, the files, what was run', () => {
  const rows = own.jobRows({
    asked: 'Then create a new branch off main and execute those improvements.', repo: 'acme/kettle',
    base_branch: 'main', base: 'f64e28a4de9d', branch: 'douglas/then-create-a-new-branch-875c0c', state: 'done',
    before: ['Look into it.'], context: [{ from: 'the report of job aa11bb22', text: 'Three things.' }],
    needs: [{ n: 1, text: 'Retry the fetch', accept: 'it retries twice', state: 'done', note: '' },
            { n: 2, text: 'Add a test', accept: '', state: 'outstanding', note: 'not written' }],
    stat: '1 file changed', files: ['server.py'], checks: [{ what: 'Python syntax', ok: false, said: 'line 3' }],
    ran: [{ cmd: 'pytest -q', code: 1 }, { cmd: 'sleep 999', code: null }], looked: 'found',
    summary: 'Made it retry.', error: '', pr: '' });
  assert.deepEqual(rows.filter(r => r[0] === 'k').map(r => r[1]), [
    'ASKED', 'WHERE', 'SAID JUST BEFORE', 'HANDED OVER WITH IT · the report of job aa11bb22',
    'REQUIREMENTS · 1 OF 2 DONE · 1 OUTSTANDING · his own word on each', 'CHANGED', 'CHECKS AND WHAT WAS RUN',
    'HIS OWN ACCOUNT · not checked']);
  assert.deepEqual(rows.filter(r => r[0] === 'n'), [
    ['n', '1. Retry the fetch\nDone when: it retries twice', 'done'],
    ['n', '2. Add a test\nnot written', 'outstanding']]);
  const text = rows.map(r => r[1]).join('\n');
  assert.ok(text.includes('acme/kettle · from main @ f64e28a4de9d · on douglas/then-create-a-new-branch-875c0c'));
  assert.ok(text.includes('FAILED · Python syntax (line 3)\nexit 1 · pytest -q\ndid not finish · sleep 999\n'
                          + 'a second look found things to put right, and he went back to them'));
  assert.deepEqual(own.jobRows({ state: 'done', kind: 'task' }),
                   [['k', 'CHECKS AND WHAT WAS RUN'], ['v', 'nothing was run on the result']]);
  assert.deepEqual(own.jobRows({ state: 'running' }), [], 'a job under way has no result to have run anything on');
  assert.deepEqual(own.jobRows(null), []);
});

test('his conversations are rows, the one in front of him marked, each with how much was said and when', () => {
  const chats = { id: 'bbbbbbbbbbbb', title: 'The boiler', kept: 2, list: [
    { id: 'aaaaaaaaaaaa', title: 'Goat names', updated: 1000 - 240, turns: 1, via: 'webui' },
    { id: 'bbbbbbbbbbbb', title: '', updated: 1000 - 7200, turns: 3, via: 'phone' }] };
  assert.deepEqual(own.chatRows(chats, 1000), [
    { id: 'aaaaaaaaaaaa', title: 'Goat names', here: false, sub: '1 exchange · 4 m ago · chat page' },
    { id: 'bbbbbbbbbbbb', title: '(no name yet)', here: true, sub: '3 exchanges · 2 h 0 m ago · phone' }]);
  assert.equal(own.chatsCount(chats), '2 KEPT');
  assert.equal(own.chatsCount(null), '', 'where none are kept there is no count');
  assert.deepEqual(own.chatRows(null, 1000), []);
  assert.deepEqual(own.chatRows({ id: 'x' }, 1000), [], 'a state with no list in it is no rows');
});

test('every tool he has is a row with its state, and one this page has no words for is not hidden', () => {
  const rows = own.switchRows({ search: 'off', memory: 'on', draw: 'absent', teleport: 'on' });
  assert.deepEqual(rows.map(r => [r.name, r.state]),
                   [['memory', 'on'], ['search', 'off'], ['draw', 'absent'], ['teleport', 'on']]);
  assert.equal(rows[3].label, 'teleport');
  assert.deepEqual(own.switchRows(null), [], 'not read is no rows, which the view says in words');
  assert.equal(new Set(own.TOOLS.map(t => t[0])).size, own.TOOLS.length, 'no tool is listed twice');
});

/* ── what the watch raised ──────────────────────────────────────────────── */

test('how many notices are open, and how bad the worst is; not read is not none', () => {
  assert.deepEqual(ntc.summary({ ok: true, open: [] }), { text: 'NONE', cls: 'ok' });
  assert.deepEqual(ntc.summary({ ok: true, open: [{ severity: 'warning' }, { severity: 'information' }] }),
                   { text: '2 OPEN', cls: 'busy' });
  assert.deepEqual(ntc.summary({ ok: true, open: [{ severity: 'warning', acked: 1 }, { severity: 'critical' }] }),
                   { text: '2 OPEN · 1 NEW', cls: 'bad' });
  assert.deepEqual(ntc.summary({ ok: true, open: [{ severity: 'information' }] }), { text: '1 OPEN', cls: '' });
  assert.deepEqual(ntc.summary({ ok: false, text: 'Douglas has no memory installed.' }), { text: '—', cls: '' });
  assert.deepEqual(ntc.summary(null), { text: '—', cls: '' }, 'no answer is a dash, never NONE');
});

test('the top bar says how many notices nobody has seen, and nothing when there is none or it is not known', () => {
  assert.deepEqual(ntc.navNotices({ open: 3, new: 2, worst: 'critical' }), { text: '2 notices', critical: true });
  assert.deepEqual(ntc.navNotices({ open: 1, new: 1, worst: 'warning' }), { text: '1 notice', critical: false });
  assert.equal(ntc.navNotices({ open: 2, new: 0, worst: null }), null, 'seen ones are not for the bar');
  assert.equal(ntc.navNotices(null), null, 'not read: the bar says what it always said');
  assert.equal(ntc.navNotices(undefined), null);
});

test('the quiet hours step an hour at a time, round midnight', () => {
  assert.equal(ntc.hourStep(22, 1), 23);
  assert.equal(ntc.hourStep(23, 1), 0);
  assert.equal(ntc.hourStep(0, -1), 23);
  assert.equal(ntc.hourStep(8, -1), 7);
  assert.equal(ntc.hourStep(undefined, 1), 1, 'an hour not known is taken as midnight, never NaN');
});

test('a notice says since when, and what has become of it', () => {
  const now = 2000000000, at = fmt.fmtClock(now - 600);
  assert.equal(ntc.meta({ raised: now - 600 }, now), 'since ' + at + ' · 10 m · not yet mentioned');
  assert.equal(ntc.meta({ raised: now - 600, told: now - 500, times: 2 }, now),
               'since ' + at + ' · 10 m · got worse · Douglas has mentioned it');
  assert.equal(ntc.meta({ raised: now - 600, acked: now - 100, spoken: now - 400 }, now),
               'since ' + at + ' · 10 m · seen · said aloud');
  assert.equal(ntc.meta({ raised: now - 600, cleared: now - 300, acked: now - 400 }, now),
               'over since ' + fmt.fmtClock(now - 300) + ' · lasted 5 m',
               'one that is over says how long it lasted, not how long ago it began');
});

/* ── the conversation log, scrolled back ─────────────────────────────────── */

test('earlier lines and the newest merge oldest first, each line once', () => {
  const a = { ts: 10, who: 'user', text: 'Is the kettle on?', via: 'phone' };
  const b = { ts: 11, who: 'douglas', text: 'It is.', via: 'phone' };
  const c = { ts: 12, who: 'user', text: 'Thank you.', via: 'voice' };
  const d = { ts: 13, who: 'douglas', text: 'You are welcome.', via: 'voice' };
  assert.deepEqual(convo.mergeTurns([a, b, c], [c, d]), [a, b, c, d], 'the overlap is kept once');
  assert.deepEqual(convo.mergeTurns([c, d], [a, b]), [a, b, c, d], 'whichever way round they come');
  assert.deepEqual(convo.mergeTurns([], [b, a]), [a, b]);
  assert.deepEqual(convo.mergeTurns(null, undefined), []);
  const same = { ts: 11, who: 'user', text: 'It is.', via: 'phone' };
  assert.deepEqual(convo.mergeTurns([b], [same]), [b, same], 'two speakers in the same second are two lines');
  const untimed = { who: 'douglas', text: 'no clock' };
  assert.deepEqual(convo.mergeTurns([untimed, a], []), [a, untimed], 'a line without a time goes last');
  assert.equal(convo.oldestTs([untimed, b, a]), 11);
  assert.equal(convo.oldestTs([]), null);
});

test('only a log already at its newest line follows a new one down', () => {
  assert.equal(convo.atBottom(600, 1000, 400), true);
  assert.equal(convo.atBottom(590, 1000, 400), true, 'a finger short of the end is still the end');
  assert.equal(convo.atBottom(300, 1000, 400), false, 'scrolled up to read stays where it is');
  assert.equal(convo.atBottom(0, 300, 400), true, 'a log shorter than its box is at its end');
});

test('the earlier lines are asked for by a number only', () => {
  assert.equal(convo.earlierPath(1795566756.75), '/api/douglas/transcript?before=1795566756.75&limit=30');
  assert.equal(convo.earlierPath(5, 10), '/api/douglas/transcript?before=5&limit=10');
  assert.equal(convo.earlierPath(5, 10, 'a1b2c3d4e5f6'), '/api/douglas/transcript?before=5&limit=10&chat=a1b2c3d4e5f6');
});

test('a line ChatGPT said is named CHATGPT, not YOU', () => {
  assert.equal(convo.whoLabel({ who: 'user', via: 'chatgpt' }), 'CHATGPT');
  assert.equal(convo.whoLabel({ who: 'user', via: 'phone' }), 'YOU');
  assert.equal(convo.whoLabel({ who: 'douglas', via: 'chatgpt' }), 'DOUGLAS', 'his reply to it is still his');
  assert.equal(convo.whoLabel(null), 'DOUGLAS');
});

if (failures.length) {
  console.error(`FAIL: deck-ui pure logic (${failures.length} of ${passed + failures.length})`);
  failures.forEach(f => console.error('  - ' + f));
  process.exit(1);
}
console.log(`deck-ui pure logic tests passed (${passed} assertions groups)`);
