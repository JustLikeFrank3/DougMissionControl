/* nav.js — the NAV surface: moving map, flight plan, steer, and the mission
   strip SIM shares.

   Owns its own state — plan, zoom, trail, GPS sync — because all of it is
   NAV's and none of it is anyone else's. deck.js hands it the plan from each
   state frame and otherwise leaves it alone.

   Position is OBSERVED, always. The trail is where the aircraft has been, the
   steer arrow is derived from observed track, and nothing here draws a
   commanded anything. */

import { $, wireHold, post, getJSON } from './ui.js';
import { fmtEte, fmtAgo, fmtDur, fmtClock } from './format.js';
import { paintAtcCue } from './atc.js';
import { calculateDescent } from './descent.js';
import { xy as navXY, distBrg as navDistBrg, world as navWorld, clampZoom as navClampZ,
         isTeleport, isStaleGpsLeg, zoomForSpan, TILE, NM_PER_DEG,
         ZMIN as NAV_ZMIN, ZMAX as NAV_ZMAX } from './geo.js';

/* ── NAV ───────────────────────────────────────────────────────────────
   Moving map from the sim agent's observed position, via the same
   /api/sim pass-through the SIM surface uses. The trail is where the
   aircraft HAS been; the steer arrow is (true bearing to waypoint) minus
   (true track) — fly the arrow to the top and the waypoint arrives.
   Nothing here is commanded state, and there are no map tiles: a vector
   trail on dark is cheaper for the Pi and more EFIS anyway. */
// The flight plan is the operator's, not ours: an ordered list of
// {name, lat, lon} held by deck-api (so a kiosk reload keeps it), edited
// from the panel via search. The selected entry is the steer target.
var navPlan = [];
/** deck-api owns the plan and the SSE frame delivers it; NAV holds the copy it
    draws from. A setter rather than an exported binding, so the module stays
    the only thing that can change it. */
export function setNavPlan(p) { navPlan = p || []; }
export function getNavPlan() { return navPlan; }
/** SAVE is disabled with an empty plan. Driven from the state frame rather
    than the NAV paint, because that paint only runs once the sim is feeding a
    position — with no sim the button sat enabled and did nothing on tap. */
export function setNavPlanDisabled() {
  var b = $('navp-save');
  if (b) b.disabled = !navPlan.length;
}
var navSelIdx = 0;
var navPlanSig = null;
// SYNC: the sim's GPS dictates the plan. SimConnect only exposes the leg
// being flown — prev + next — so the route accumulates as legs sequence:
// departure and first waypoint at once, the rest as the flight reaches them.
var navSync = false;
try { navSync = localStorage.getItem('navSync') === '1'; } catch (e) { }
var navSyncWps = {};             // gps waypoint index -> {name, lat, lon}
var navSyncCount = 0;            // plan shape; a change means a new plan
var navSyncGpsIndex = null;       // changes select a leg; steady polls do not undo a manual target
var navSyncInvalid = false;      // collapsed departure leg far behind aircraft
var navTrail = [];               // {t, lat, lon} — browser memory only
var navTimer = null;
var navZoom = null;              // null = auto-fit; integer = manual slippy z
var navLastZ = 12;               // whatever the auto-fit last chose
var navMapFocus = null;          // explicit map center; independent of steer target
var TOD_DEFAULT_TARGET_FT = 3000;
var todTargetFt = 3000;
var todAngleDeg = 3;
var todTargetMode = 'auto';
var todOpen = false;
try {
  todTargetFt = Math.max(0, Math.min(50000,
    Number(localStorage.getItem('todTargetFt')) || 3000));
  var storedAngle = Number(localStorage.getItem('todAngleDeg'));
  if ([2.5, 3, 3.5].indexOf(storedAngle) !== -1) todAngleDeg = storedAngle;
  todTargetMode = localStorage.getItem('todTargetMode') === 'manual' ? 'manual' : 'auto';
} catch (e) { }

export function navPoll(on) {
  if (on && !navTimer) { navTick(); navTimer = setInterval(navTick, 500); }
  else if (!on && navTimer) { clearInterval(navTimer); navTimer = null; }
}

export function navTick() {
  // The catch covers the FETCH only. Wrapping the paint in it too means any
  // exception while drawing gets repainted as "the link is down", which is a
  // lie about a different machine and sends you debugging the agent. Same bug
  // simTick had; it cost an evening there.
  fetch('/api/sim', { cache: 'no-store' })
    .then(function (r) { return r.json(); })
    .catch(function () { paintNav({ link: false }); return null; })
    .then(function (d) { if (d) paintNav(d); });
}

/* Grow the plan from the GPS's observed legs. deck-api still owns the plan
   — sync just becomes its author — so PLANS/SAVE/reloads all keep working. */
function navSyncFeed(g, r) {
  if (!navSync || !g || !g.count) return;
  if (Array.isArray(g.plan) && g.plan.length) {
    navSyncInvalid = false;
    $('navp-sync').textContent = 'SYNC';
    $('navp-sync').classList.remove('bad');
    var full = g.plan.filter(function (w) {
      return w && typeof w.lat === 'number' && typeof w.lon === 'number';
    }).map(function (w, i) {
      var waypoint = { name: String(w.id || 'WP' + (i + 1)).slice(0, 12),
                       lat: w.lat, lon: w.lon };
      if (typeof w.altitude_ft === 'number') waypoint.altitude_ft = w.altitude_ft;
      return waypoint;
    });
    if (g.plan_source !== 'visual_pattern' && g.index !== navSyncGpsIndex) {
      navSyncGpsIndex = g.index;
      navSelIdx = Math.max(0, Math.min(full.length - 1, Number(g.index) || 0));
    }
    var fullSig = full.map(waypointSignature).join('|');
    var planSig = navPlan.map(waypointSignature).join('|');
    if (fullSig !== planSig) {
      if (g.plan_source === 'visual_pattern') navSelIdx = 0;
      post('/api/nav/plan', { waypoints: full });
    }
    return;
  }
  if (isStaleGpsLeg(g, r)) {
    navSyncInvalid = true;
    navSync = false;
    navSyncWps = {}; navSyncCount = 0; navSyncGpsIndex = null;
    try { localStorage.setItem('navSync', '0'); } catch (e) { }
    $('navp-sync').classList.remove('on');
    $('navp-sync').textContent = 'GPS STALE';
    $('navp-sync').classList.add('bad');
    return;
  }
  if (navSyncInvalid) {
    navSyncInvalid = false;
    $('navp-sync').textContent = 'SYNC';
    $('navp-sync').classList.remove('bad');
  }
  if (g.count !== navSyncCount) { navSyncWps = {}; navSyncCount = g.count; }
  var dirty = false;
  [g.prev, g.next].forEach(function (w) {
    if (!w || typeof w.lat !== 'number' || typeof w.i !== 'number') return;
    // String(): a GPS waypoint id is usually an ident like "KORL", but the
    // agent will hand over whatever the sim gave it, and a numeric id has no
    // .slice — which threw here, took paintNav with it, and got reported as
    // the sim link being down. Nothing about a waypoint name is worth losing
    // the surface over.
    var name = String(w.id || 'WP' + (w.i + 1)).slice(0, 12);
    var cur = navSyncWps[w.i];
    if (!cur || cur.lat !== w.lat || cur.lon !== w.lon) {
      navSyncWps[w.i] = { name: name, lat: w.lat, lon: w.lon };
      dirty = true;
    }
  });
  // Steer what the GPS steers: the active leg's endpoint, by position in
  // the assembled order rather than raw GPS index — early legs may be
  // missing if sync was engaged mid-flight.
  var order = Object.keys(navSyncWps).map(Number).sort(function (a, b) { return a - b; });
  if (g.index !== navSyncGpsIndex && g.next && navSyncWps[g.next.i]) {
    navSyncGpsIndex = g.index;
    navSelIdx = order.indexOf(g.next.i);
  }
  if (!dirty) return;
  var wps = order.map(function (k) { return navSyncWps[k]; });
  var sig = wps.map(waypointSignature).join('|');
  var cur = navPlan.map(waypointSignature).join('|');
  if (sig !== cur) post('/api/nav/plan', { waypoints: wps });
}

/* ── MissionContext ───────────────────────────────────────────────
   The current operational context — not a flight object. Today the only
   mission kind is 'flight'; a train run or a workstation boot slots in as
   another kind with the same shape. NAV writes it, SIM reads it, and no
   second copy of navigation state exists anywhere. Everything in it is
   DERIVED from observed simulator state — nothing is invented. */
var mission = { kind: null, vehicle: null, phase: null, dest: null,
                brg: null, dist: null, ete_s: null, eta: null, xte: null };

export function missionPhase(r, distanceNm = mission.dist) {
  if (r.on_ground === undefined) return null;   // agent predates phase data
  if (r.on_ground) {
    if ((r.gs_kt || 0) > 40) return 'ROLL';
    if ((r.gs_kt || 0) > 2) return 'TAXI';
    return (r.rpm_1 || 0) < 50 ? 'COLD & DARK' : 'RAMP';
  }
  if (distanceNm !== null && distanceNm < 15 && (r.vs_fpm || 0) < 100) return 'APPROACH';
  if (typeof r.agl_ft === 'number' && r.agl_ft < 1500 && (r.vs_fpm || 0) >= 100)
    return 'TAKEOFF';
  if ((r.vs_fpm || 0) > 300) return 'CLIMB';
  if ((r.vs_fpm || 0) < -300) return 'DESCENT';
  return 'CRUISE';
}

export function missionUpdate(d) {
  var st = d && d.session ? d.state : null;
  var r = st && st.readouts;
  if (!r || typeof r.lat !== 'number') {
    mission.kind = null; mission.phase = null;
    return;
  }
  mission.kind = 'flight';
  mission.vehicle = st.aircraft || null;
  var wpt = navPlan[navSelIdx] || null;
  mission.dest = wpt ? wpt.name : null;
  if (wpt) {
    var nav = navDistBrg(r.lat, r.lon, wpt.lat, wpt.lon);
    mission.brg = Math.round(nav.brg);
    mission.dist = nav.dist;
    mission.ete_s = (r.gs_kt > 5) ? nav.dist / r.gs_kt * 3600 : null;
    mission.eta = mission.ete_s !== null ? Date.now() / 1000 + mission.ete_s : null;
    // Cross-track against the leg being flown. No previous waypoint means
    // no leg to be off of — direct-to has no XTK by definition.
    var prev = navSelIdx > 0 ? navPlan[navSelIdx - 1] : null;
    if (prev) {
      var a = navXY(r.lat, r.lon, prev.lat, prev.lon);
      var b = navXY(wpt.lat, wpt.lon, prev.lat, prev.lon);
      var len = Math.sqrt(b.x * b.x + b.y * b.y);
      mission.xte = len > 0.1 ? (a.x * b.y - a.y * b.x) / len : null;
    } else { mission.xte = null; }
  } else {
    mission.brg = mission.dist = mission.ete_s = mission.eta = mission.xte = null;
  }
  mission.phase = missionPhase(r);
}

export function paintMissionStrip() {
  $('ms-phase').textContent = mission.phase || '—';
  $('ms-dest').textContent = mission.dest || '—';
  $('ms-brg').textContent = mission.brg !== null ? ('00' + mission.brg).slice(-3) + '°' : '—';
  $('ms-dist').textContent = mission.dist !== null
    ? (mission.dist >= 10 ? Math.round(mission.dist) : mission.dist.toFixed(1)) + ' nm' : '—';
  $('ms-ete').textContent = mission.ete_s !== null ? fmtDur(mission.ete_s) : '—';
  $('ms-eta').textContent = mission.eta !== null ? fmtClock(mission.eta) : '—';
  $('ms-xte').textContent = mission.xte !== null
    ? Math.abs(mission.xte).toFixed(2) + ' ' + (mission.xte >= 0 ? 'R' : 'L') : '—';
  // Phase-aware presentation: approach promotes what is about to be used.
  // Observation, not advice — the gear tile grows, nobody is told to use it.
  var appr = mission.phase === 'APPROACH';
  $('simt-gear').classList.toggle('hot', appr);
  $('simt-flaps').classList.toggle('hot', appr);
}

export function paintNav(d) {
  d = d || {};
  var st = d.session ? d.state : null;
  var r = st && st.readouts;
  var live = !!(r && typeof r.lat === 'number');

  paintAtcCue(st);

  $('navp-ph').hidden = live;
  $('navp-live').hidden = !live;
  $('navp-pill').textContent = !d.link ? 'NO LINK'
    : !d.session ? 'NO SIM'
    : 'AGENT PREDATES NAV';       // session up but no lat in readouts
  if (!live) return;

  // Trail: record when we have actually moved (~37 m), prune at 45 min.
  var last = navTrail[navTrail.length - 1];
  if (last) {
    // A relocate — new departure airport, repositioned flight — is
    // instantaneous, and the trail is one polyline, so without this the map
    // draws a straight blue line from the old airport to the new one and
    // keeps it for 45 minutes. It also poisons the auto-fit, which bounds
    // the view on the trail: one relocate and the map zooms out to hold two
    // states at once.
    //
    // isTeleport judges it on implied ground speed rather than raw distance,
    // which is what keeps a backgrounded tab or a missed poll — both of which
    // produce a legitimately large gap — from being mistaken for a jump.
    if (isTeleport(last, r, Date.now() - last.t)) navTrail.length = 0;
    last = navTrail[navTrail.length - 1];
  }
  if (!last || navDistBrg(last.lat, last.lon, r.lat, r.lon).dist > 0.02) {
    navTrail.push({ t: Date.now(), lat: r.lat, lon: r.lon });
  }
  var cut = Date.now() - 45 * 60000;
  while (navTrail.length && navTrail[0].t < cut) navTrail.shift();

  navSyncFeed(st.gps, r);
  missionUpdate(d);

  // The aircraft on the NAV rail — same mission, different view.
  $('navp-ac').textContent = (st.aircraft || '—').slice(0, 18);
  var apState = st.controls && st.controls.ap_master ? st.controls.ap_master.state : null;
  $('navp-acs').textContent = r.alt_ft + ' ft · ' + r.gs_kt + ' kt' +
    (apState ? ' · AP ' + (apState === 'engaged' ? 'ON' : 'OFF') : '');

  if (navSelIdx >= navPlan.length) navSelIdx = Math.max(0, navPlan.length - 1);
  var wpt = navPlan[navSelIdx] || null;

  if (wpt) {
    var nav = navDistBrg(r.lat, r.lon, wpt.lat, wpt.lon);
    // Route total: aircraft -> wpt1 -> ... -> N, since the start point is
    // wherever the aircraft actually is.
    var total = 0, prev = { lat: r.lat, lon: r.lon };
    navPlan.forEach(function (w) {
      total += navDistBrg(prev.lat, prev.lon, w.lat, w.lon).dist;
      prev = w;
    });
    $('navp-brg').textContent = ('00' + Math.round(nav.brg)).slice(-3) + '°T · ' +
      (nav.dist >= 10 ? Math.round(nav.dist) : nav.dist.toFixed(1)) + ' nm';
    $('navp-ete').textContent = fmtEte(nav.dist, r.gs_kt) + ' · ' + wpt.name +
      (navPlan.length > 1 ? ' · route ' + Math.round(total) + ' nm' : '');
    $('navp-arrow-g').style.transform =
      'rotate(' + ((nav.brg - (r.trk_true || 0) + 360) % 360) + 'deg)';
    $('nav-nav').textContent = wpt.name.slice(0, 12) + ' ' +
      (nav.dist >= 10 ? Math.round(nav.dist) : nav.dist.toFixed(1)) + ' nm';
  } else {
    $('navp-brg').textContent = '—';
    $('navp-ete').textContent = 'no waypoints';
    $('navp-arrow-g').style.transform = 'rotate(0deg)';
    $('nav-nav').textContent = 'moving map';
  }
  $('navp-trk').textContent = ('00' + (r.trk_true || 0)).slice(-3) + '°T · ' + r.gs_kt + ' kt';
  $('navp-alt').textContent = r.alt_ft + ' ft · ' + r.ias_kt + ' kt ias';
  $('navp-ias').textContent = Number(r.ias_kt).toLocaleString();
  $('navp-flight-alt').textContent = Number(r.alt_ft).toLocaleString();

  paintDescent(r, st.controls || {});

  drawNavMap(r, wpt);
  renderNavPlan(r);
}

function rounded(value, increment) {
  return Math.round(value / increment) * increment;
}

function renderTodOpen() {
  $('tod-card').hidden = !todOpen;
  $('tod-open').classList.toggle('on', todOpen);
  $('tod-open').setAttribute('aria-expanded', todOpen ? 'true' : 'false');
}

function paintDescent(r, controls) {
  var selected = controls.ap_alt && typeof controls.ap_alt.ft === 'number'
    ? controls.ap_alt.ft : null;
  var advisory = calculateDescent({
    aircraft: {
      altitudeFt: r.alt_ft, lat: r.lat, lon: r.lon,
      groundspeedKt: r.gs_kt, verticalSpeedFpm: r.vs_fpm,
      selectedAltitudeFt: selected,
    },
    route: navPlan,
    activeIndex: navSelIdx,
    flightPhase: mission.phase,
    config: { targetFt: todTargetMode === 'manual' ? todTargetFt : TOD_DEFAULT_TARGET_FT,
          targetMode: todTargetMode,
              angleDeg: todAngleDeg, bufferNm: 10 },
  });
  var card = $('tod-card');
  card.className = 'todcard ' + advisory.state;
  $('tod-state').textContent = advisory.state.replace('_', ' ').toUpperCase();
  $('tod-profile').textContent = todAngleDeg.toFixed(1) + '°';
  $('tod-auto').classList.toggle('on', todTargetMode === 'auto');

  [].forEach.call(document.querySelectorAll('.tod-profile-set button'), function (button) {
    button.classList.toggle('on', Number(button.dataset.angle) === todAngleDeg);
  });
  if (advisory.state === 'unavailable') {
    var departure = advisory.reason.indexOf('during ') === 0;
    $('tod-call').textContent = departure ? 'STANDBY' : '—';
    $('tod-sub').textContent = departure
      ? advisory.reason.slice(7).toUpperCase() + ' IN PROGRESS'
      : 'NO USABLE ' + advisory.reason.toUpperCase();
    ['tod-target', 'tod-lose', 'tod-vs', 'tod-path'].forEach(function (id) {
      $(id).textContent = '—';
    });
    $('tod-source').textContent = departure ? 'DESCENT ADVISORY PAUSED' : 'ADVISORY ONLY';
    return;
  }

  var distance = advisory.distanceToTodNm;
  if (advisory.state === 'complete') {
    $('tod-call').textContent = 'NOT REQUIRED';
    $('tod-sub').textContent = 'TARGET ALTITUDE REACHED';
  } else if (advisory.state === 'descending') {
    $('tod-call').textContent = 'DESCENT ACTIVE';
    $('tod-sub').textContent = Math.round(advisory.remainingNm) + ' NM TO ' + advisory.targetName;
  } else if (advisory.state === 'tod_now') {
    $('tod-call').textContent = 'TOD NOW';
    $('tod-sub').textContent = 'BEGIN DESCENT WHEN CLEARED';
  } else if (advisory.state === 'missed') {
    $('tod-call').textContent = 'TOD MISSED';
    $('tod-sub').textContent = Math.round(advisory.requiredDistanceNm) + ' NM REQUIRED · ' +
      Math.round(advisory.remainingNm) + ' NM REMAIN';
  } else {
    $('tod-call').textContent = 'TOD ' + Math.max(0, Math.round(distance)) + ' NM';
    $('tod-sub').textContent = advisory.timeToTodMin === null ? 'TIME —'
      : '~' + Math.max(1, Math.round(advisory.timeToTodMin)) + ' MIN' +
        (advisory.state === 'imminent' ? ' · PREPARE DESCENT' : '');
  }
  $('tod-target').textContent = advisory.targetAltitudeFt.toLocaleString() + ' FT';
  $('tod-lose').textContent = rounded(advisory.altitudeToLoseFt, 100).toLocaleString() + ' FT';
  $('tod-vs').textContent = advisory.requiredVsFpm === null ? '—'
    : rounded(advisory.requiredVsFpm, 50).toLocaleString() + ' FPM';
  $('tod-source').textContent = advisory.targetSource.replace('_', ' ').toUpperCase() +
    ' · ' + advisory.targetName +
    (advisory.selectedAltitudeFt === null ? '' : ' · SEL ' + advisory.selectedAltitudeFt + ' FT');

  card.classList.remove('path-warn', 'path-bad');
  if (advisory.pathStatus === null) {
    $('tod-path').textContent = '—';
  } else if (advisory.pathStatus === 'on_path') {
    $('tod-path').textContent = 'ON PATH';
  } else {
    var deviation = rounded(advisory.pathDeviationFt, 50);
    $('tod-path').textContent = (deviation > 0 ? '+' : '') + deviation.toLocaleString() +
      ' FT ' + (deviation > 0 ? 'HIGH' : 'LOW');
    card.classList.add(Math.abs(deviation) > 750 ? 'path-bad' : 'path-warn');
  }
}

/* The plan list. Rebuilt when the plan itself changes; the per-row live
   distances update every paint without rebuilding the DOM. */
export function renderNavPlan(r) {
  var box = $('navp-plan');
  var sig = navPlan.map(waypointSignature).join('|');
  if (sig !== navPlanSig) {
    navPlanSig = sig;
    if (navMapFocus && !navPlan.some(function (w) {
      return w.lat === navMapFocus.lat && w.lon === navMapFocus.lon;
    })) navMapFocus = null;
    box.innerHTML = '';
    if (!navPlan.length) {
      var e = document.createElement('div');
      e.className = 'empty';
      e.textContent = 'No waypoints. ADD searches the real map — type a place, tap a result.';
      box.appendChild(e);
    }
    navPlan.forEach(function (w, i) {
      var row = document.createElement('div');
      row.className = 'navrow waypoint';
      row.innerHTML = '<span class="n"></span><span class="navrow-main">' +
                      '<span class="t"></span><span class="meta"></span></span>' +
                      '<span class="d"></span><button class="focus">⌖</button>' +
                      '<button class="x">✕</button>';
      row.querySelector('.n').textContent = ('0' + (i + 1)).slice(-2);
      row.querySelector('.t').textContent = w.name;
      row.querySelector('.meta').textContent = formatCoordinate(w.lat, 'N', 'S') +
        '  ·  ' + formatCoordinate(w.lon, 'E', 'W') +
        (typeof w.altitude_ft === 'number' ? '  ·  ' + Math.round(w.altitude_ft) + ' FT' : '');
      row.addEventListener('click', function () { navSelIdx = i; navTick(); });
      var focus = row.querySelector('.focus');
      focus.title = 'Center map on ' + w.name;
      focus.setAttribute('aria-label', 'Center map on ' + w.name);
      focus.addEventListener('click', function (ev) {
        ev.stopPropagation();
        navMapFocus = { lat: w.lat, lon: w.lon };
        navSetZoom(navZoom === null ? navLastZ : navZoom);
      });
      row.querySelector('.x').addEventListener('click', function (ev) {
        ev.stopPropagation();
        var next = navPlan.slice(0, i).concat(navPlan.slice(i + 1));
        post('/api/nav/plan', { waypoints: next });
      });
      box.appendChild(row);
    });
  }
  $('navp-count').textContent = navPlan.length + ' IN PLAN';
  $('navp-tab-count').textContent = navPlan.length;
  [].forEach.call(box.querySelectorAll('.navrow'), function (row, i) {
    row.classList.toggle('on', i === navSelIdx);
    if (r && navPlan[i]) {
      var nav = navDistBrg(r.lat, r.lon, navPlan[i].lat, navPlan[i].lon);
      row.querySelector('.d').textContent = ('00' + Math.round(nav.brg)).slice(-3) +
        '°T · ' + (nav.dist >= 10 ? Math.round(nav.dist) : nav.dist.toFixed(1)) + ' nm';
    }
  });
}

function waypointSignature(waypoint) {
  return waypoint.name + waypoint.lat + waypoint.lon +
    (typeof waypoint.altitude_ft === 'number' ? '@' + waypoint.altitude_ft : '');
}

function formatCoordinate(value, positive, negative) {
  return Math.abs(value).toFixed(5) + '°' + (value >= 0 ? positive : negative);
}

/* Search overlay + on-screen keyboard. Geocoding runs through deck-api's
   Nominatim proxy; a tapped result is appended to the plan. */
var navQ = '';

var navKbdMode = 'wpt';          // 'wpt' = geocode search, 'save' = plan name
var navKbdGoBtn = null;

export function navSearchOpen(on, mode) {
  navKbdMode = mode || 'wpt';
  $('navp-search').hidden = !on;
  if (navKbdGoBtn) navKbdGoBtn.textContent = navKbdMode === 'save' ? 'SAVE' : 'SEARCH';
  if (on) {
    navQ = ''; navQPaint();
    $('navp-results').innerHTML = navKbdMode === 'save'
      ? '<div class="nsx-msg">Name the current ' + navPlan.length +
        '-waypoint plan, then SAVE. It survives reboots.</div>'
      : '';
  }
}

function navQPaint() {
  $('navp-q').textContent = navQ.length ? navQ : ' ';
}

function navSearchGo() {
  if (navQ.trim().length < 2) return;
  if (navKbdMode === 'save') {
    post('/api/nav/plans', { action: 'save', name: navQ.trim() }).then(function (rsp) {
      if (rsp && rsp.ok) { navSearchOpen(false); navPlansOpen(true); }
      else {
        $('navp-results').innerHTML = '<div class="nsx-msg">SAVE FAILED · ' +
          ((rsp && rsp.reason) || 'no reply') + '</div>';
      }
    });
    return;
  }
  var res = $('navp-results');
  res.innerHTML = '<div class="nsx-msg">searching…</div>';
  fetch('/api/nav/geocode?q=' + encodeURIComponent(navQ.trim()), { cache: 'no-store' })
    .then(function (x) { return x.json(); })
    .then(function (d) {
      var hits = (d && d.results) || [];
      res.innerHTML = '';
      if (!hits.length) {
        res.innerHTML = '<div class="nsx-msg">nothing found — OSM knows places, not brands; try adding a city</div>';
        return;
      }
      hits.forEach(function (h) {
        var b = document.createElement('button');
        b.className = 'nsx-r';
        b.innerHTML = '<span class="t"></span><span class="s"></span>';
        b.querySelector('.t').textContent = h.name;
        b.querySelector('.s').textContent = h.detail || (h.lat.toFixed(4) + ', ' + h.lon.toFixed(4));
        b.addEventListener('click', function () {
          var next = navPlan.concat([{ name: h.name.toUpperCase(), lat: h.lat, lon: h.lon }]);
          post('/api/nav/plan', { waypoints: next }).then(function () {
            navSelIdx = next.length - 1;   // new waypoint becomes the target
            navSearchOpen(false);
          });
        });
        res.appendChild(b);
      });
    })
    .catch(function () {
      res.innerHTML = '<div class="nsx-msg">search failed — no internet from the Pi?</div>';
    });
}

export function navKbdBuild() {
  var rows = ['1234567890', 'QWERTYUIOP', 'ASDFGHJKL', 'ZXCVBNM'];
  var kbd = $('navp-kbd');
  // Cleared first, so building twice yields one keyboard rather than two
  // stacked on top of each other. wireNav did exactly that for a while and the
  // waypoint search grew a second full QWERTY underneath the first.
  kbd.innerHTML = '';
  rows.forEach(function (line) {
    var kr = document.createElement('div');
    kr.className = 'krow';
    line.split('').forEach(function (ch) {
      var k = document.createElement('button');
      k.textContent = ch;
      k.addEventListener('click', function () { navQ += ch; navQPaint(); });
      kr.appendChild(k);
    });
    kbd.appendChild(kr);
  });
  var last = document.createElement('div');
  last.className = 'krow';
  [['SPACE', 'space', function () { navQ += ' '; }],
   ['⌫', 'wide', function () { navQ = navQ.slice(0, -1); }],
   ['CLEAR', 'wide', function () { navQ = ''; }],
   ['SEARCH', 'wide go', navSearchGo]].forEach(function (def) {
    var k = document.createElement('button');
    k.textContent = def[0];
    k.className = def[1];
    k.addEventListener('click', function () { def[2](); navQPaint(); });
    if (def[0] === 'SEARCH') navKbdGoBtn = k;   // relabels to SAVE in name mode
    last.appendChild(k);
  });
  kbd.appendChild(last);
}

/* Saved plans overlay: tap to load, ✕ to delete. The plan itself arrives
   back through the SSE state, same as any other plan edit. */
export function navPlansOpen(on) {
  $('navp-plans').hidden = !on;
  if (!on) return;
  var list = $('navp-plans-list');
  list.innerHTML = '<div class="nsx-msg">loading…</div>';
  fetch('/api/nav/plans', { cache: 'no-store' })
    .then(function (x) { return x.json(); })
    .then(function (d) {
      var plans = (d && d.plans) || [];
      list.innerHTML = plans.length ? ''
        : '<div class="nsx-msg">No saved plans yet. Build one, then SAVE it.</div>';
      plans.forEach(function (p) {
        var row = document.createElement('div');
        row.className = 'navrow';
        row.innerHTML = '<span class="t"></span><span class="d"></span>' +
                        '<button class="x">✕</button>';
        row.querySelector('.t').textContent = p.name;
        row.querySelector('.d').textContent = p.count + ' wpts';
        row.addEventListener('click', function () {
          post('/api/nav/plans', { action: 'load', name: p.name }).then(function () {
            navSelIdx = 0;
            navPlansOpen(false);
          });
        });
        row.querySelector('.x').addEventListener('click', function (ev) {
          ev.stopPropagation();
          post('/api/nav/plans', { action: 'delete', name: p.name })
            .then(function () { navPlansOpen(true); });   // refresh the list
        });
        list.appendChild(row);
      });
    })
    .catch(function () {
      list.innerHTML = '<div class="nsx-msg">could not load plans</div>';
    });
}

/* OpenStreetMap raster tiles under the overlays, dimmed so the EFIS
   symbology stays legible on top of real roads, lakes and theme parks.
   One kiosk is comfortably inside OSM's tile usage policy; attribution is
   drawn on the map as required. No internet degrades gracefully: missing
   tiles simply do not draw and the overlays stand alone on dark.
   Projection is Web Mercator throughout so tiles and trail agree. */
var navTiles = {};             // url -> Image
var navTileOrder = [];         // insertion order, for crude eviction

function navTile(url) {
  var img = navTiles[url];
  if (img) return img;
  img = new Image();
  img.src = url;
  navTiles[url] = img;
  navTileOrder.push(url);
  if (navTileOrder.length > 300) delete navTiles[navTileOrder.shift()];
  return img;
}

function drawNavMap(r, wpt) {
  var cv = $('navp-canvas');
  if (cv.width !== cv.clientWidth || cv.height !== cv.clientHeight) {
    cv.width = cv.clientWidth; cv.height = cv.clientHeight;
  }
  var g = cv.getContext('2d');
  var W = cv.width, H = cv.height;
  g.clearRect(0, 0, W, H);

  var latC, lonC, z;
  if (navMapFocus) {
    latC = navMapFocus.lat; lonC = navMapFocus.lon;
    z = navZoom === null ? navLastZ : navZoom;
  } else if (navZoom !== null) {
    // Manual zoom follows the aircraft; the waypoint is allowed offscreen.
    latC = r.lat; lonC = r.lon; z = navZoom;
  } else {
    // Auto: fit aircraft + the whole route + trail, north up, min 6 nm.
    var lats = [r.lat], lons = [r.lon];
    navPlan.forEach(function (w) { lats.push(w.lat); lons.push(w.lon); });
    navTrail.forEach(function (p) { lats.push(p.lat); lons.push(p.lon); });
    latC = (Math.min.apply(0, lats) + Math.max.apply(0, lats)) / 2;
    lonC = (Math.min.apply(0, lons) + Math.max.apply(0, lons)) / 2;
    var spanNm = Math.max(
      (Math.max.apply(0, lats) - Math.min.apply(0, lats)) * NM_PER_DEG,
      (Math.max.apply(0, lons) - Math.min.apply(0, lons)) * NM_PER_DEG *
        Math.cos(latC * Math.PI / 180),
      6) * 1.3;
    // Integer slippy zoom whose scale best fits that span — the map steps
    // between zoom levels as the flight closes in, like any slippy map.
    var mPerPx = spanNm * 1852 / Math.min(W, H);
    z = Math.floor(Math.log(156543.03 * Math.cos(latC * Math.PI / 180) / mPerPx) / Math.LN2);
    z = Math.max(8, Math.min(15, z));
    navLastZ = z;
  }

  var c = navWorld(latC, lonC, z);
  var ox = c.x - W / 2, oy = c.y - H / 2;
  function px(lat, lon) {
    var w = navWorld(lat, lon, z);
    return { x: w.x - ox, y: w.y - oy };
  }

  // Tiles, then a dark wash so the symbology owns the foreground.
  var maxT = Math.pow(2, z);
  for (var tx = Math.floor(ox / TILE); tx <= Math.floor((ox + W) / TILE); tx++) {
    for (var ty = Math.floor(oy / TILE); ty <= Math.floor((oy + H) / TILE); ty++) {
      if (ty < 0 || ty >= maxT) continue;
      var wx = ((tx % maxT) + maxT) % maxT;
      var img = navTile('https://tile.openstreetmap.org/' + z + '/' + wx + '/' + ty + '.png');
      if (img.complete && img.naturalWidth > 0) {
        g.drawImage(img, tx * TILE - ox, ty * TILE - oy);
      }
    }
  }
  g.fillStyle = 'rgba(7, 10, 15, 0.45)';
  g.fillRect(0, 0, W, H);

  var me = px(r.lat, r.lon);
  var scalePxNm = 1852 / (156543.03 * Math.cos(latC * Math.PI / 180) / Math.pow(2, z));

  // Range rings around the aircraft at a tidy step.
  var steps = [1, 2, 5, 10, 20, 50, 100];
  var ring = steps[0];
  for (var i = 0; i < steps.length; i++) {
    if (steps[i] * scalePxNm < Math.min(W, H) / 2.2) ring = steps[i];
  }
  g.strokeStyle = 'rgba(140, 165, 195, 0.55)'; g.fillStyle = '#aebfd4';
  g.font = '12px monospace';
  for (var n = 1; n <= 2; n++) {
    g.beginPath(); g.arc(me.x, me.y, ring * n * scalePxNm, 0, 7); g.stroke();
    g.fillText(ring * n + ' nm', me.x + 6, me.y - ring * n * scalePxNm + 14);
  }

  // Symbology is CASED — a dark outline under every line and glyph, the
  // way charts stay readable over any terrain. Bare magenta on Florida
  // green disappeared; magenta over a dark casing does not.
  function casedLine(path, color, width, dash) {
    [['#070a0f', width + 3.5], [color, width]].forEach(function (pass) {
      g.strokeStyle = pass[0]; g.lineWidth = pass[1];
      if (dash) g.setLineDash(pass[0] === color ? dash : []);
      g.beginPath(); path(); g.stroke();
    });
    g.setLineDash([]); g.lineWidth = 1;
  }
  function casedText(txt, x, y, color, font) {
    g.font = font || 'bold 13px monospace';
    g.lineWidth = 3.5; g.strokeStyle = '#070a0f'; g.strokeText(txt, x, y);
    g.fillStyle = color; g.fillText(txt, x, y);
    g.lineWidth = 1;
  }

  // Trail — where we have actually been.
  if (navTrail.length > 1) {
    casedLine(function () {
      navTrail.forEach(function (p, idx) {
        var q = px(p.lat, p.lon);
        if (idx === 0) g.moveTo(q.x, q.y); else g.lineTo(q.x, q.y);
      });
    }, '#56cfe1', 2.5);
  }

  // The route: the START POINT is the aircraft, always — legs run from the
  // observed position through the waypoints in plan order, Google-Maps
  // style. The selected target additionally gets a dashed direct-to line,
  // which matters when it is not the next waypoint in sequence.
  if (navPlan.length > 0) {
    casedLine(function () {
      g.moveTo(me.x, me.y);
      navPlan.forEach(function (w) {
        var q = px(w.lat, w.lon);
        g.lineTo(q.x, q.y);
      });
    }, '#8fa2b8', 2);
  }
  if (wpt) {
    casedLine(function () {
      var q = px(wpt.lat, wpt.lon);
      g.moveTo(me.x, me.y); g.lineTo(q.x, q.y);
    }, '#ff7ad9', 2.5, [8, 8]);
  }
  navPlan.forEach(function (w, i) {
    var q = px(w.lat, w.lon);
    var on = i === navSelIdx;
    var color = on ? '#ff7ad9' : '#aebfd4';
    g.fillStyle = '#070a0f';
    g.beginPath();
    g.moveTo(q.x, q.y - 9); g.lineTo(q.x + 9, q.y); g.lineTo(q.x, q.y + 9);
    g.lineTo(q.x - 9, q.y); g.closePath(); g.fill();
    g.strokeStyle = color; g.lineWidth = 2.5; g.stroke(); g.lineWidth = 1;
    casedText((i + 1) + ' ' + w.name.slice(0, 14), q.x + 13, q.y + 4, color);
  });

  // The aircraft, rotated to its observed true track.
  g.save();
  g.translate(me.x, me.y);
  g.rotate((r.trk_true || 0) * Math.PI / 180);
  g.fillStyle = '#ffffff'; g.strokeStyle = '#0b0f15';
  g.beginPath();
  g.moveTo(0, -12); g.lineTo(9, 12); g.lineTo(0, 6); g.lineTo(-9, 12);
  g.closePath(); g.fill(); g.stroke();
  g.restore();

  // Required by the tile usage policy, and honest besides.
  g.fillStyle = 'rgba(226, 233, 242, 0.55)'; g.font = '11px monospace';
  g.fillText('© OpenStreetMap contributors', W - 205, H - 8);
}

/* ── wiring ─────────────────────────────────────────────────────────────── */

function navSetZoom(zOrNull) {
  navZoom = zOrNull === null ? null : navClampZ(zOrNull);
  if (zOrNull === null) navMapFocus = null;
  var fit = $('navp-fit');
  fit.classList.toggle('on', navZoom === null);
  fit.textContent = navZoom === null ? 'FIT' : 'Z' + navZoom;
  navTick();
}

export function wireNav() {
  navKbdBuild();   // the on-screen keyboard: this panel has no physical keys
  // NAV zoom. Steps start from wherever the auto-fit currently sits, so the
  // first tap nudges rather than jumps; FIT hands framing back to auto.
  $('navp-zin').addEventListener('click', function () {
    navSetZoom((navZoom === null ? navLastZ : navZoom) + 1);
  });
  $('navp-zout').addEventListener('click', function () {
    navSetZoom((navZoom === null ? navLastZ : navZoom) - 1);
  });
  $('navp-fit').addEventListener('click', function () { navSetZoom(null); });

  $('tod-open').addEventListener('click', function () {
    todOpen = true;
    renderTodOpen();
  });
  $('tod-close').addEventListener('click', function () {
    todOpen = false;
    renderTodOpen();
  });
  renderTodOpen();

  function saveTodSettings() {
    try {
      localStorage.setItem('todTargetFt', String(todTargetFt));
      localStorage.setItem('todAngleDeg', String(todAngleDeg));
      localStorage.setItem('todTargetMode', todTargetMode);
    } catch (e) { }
    navTick();
  }
  $('tod-target-down').addEventListener('click', function () {
    todTargetFt = Math.max(0, todTargetFt - 1000); todTargetMode = 'manual'; saveTodSettings();
  });
  $('tod-target-up').addEventListener('click', function () {
    todTargetFt = Math.min(50000, todTargetFt + 1000); todTargetMode = 'manual'; saveTodSettings();
  });
  $('tod-auto').addEventListener('click', function () {
    todTargetMode = 'auto'; saveTodSettings();
  });
  [].forEach.call(document.querySelectorAll('.tod-profile-set button'), function (button) {
    button.addEventListener('click', function () {
      todAngleDeg = Number(button.dataset.angle); saveTodSettings();
    });
  });

  function navDrawerOpen(on) {
    $('navp-drawer').classList.toggle('open', on);
    $('navp-drawer-open').setAttribute('aria-expanded', on ? 'true' : 'false');
    try { localStorage.setItem('navDrawer', on ? '1' : '0'); } catch (e) { }
  }
  var drawerOpen = true;
  try { drawerOpen = localStorage.getItem('navDrawer') !== '0'; } catch (e) { }
  navDrawerOpen(drawerOpen);
  $('navp-drawer-close').addEventListener('click', function () { navDrawerOpen(false); });
  $('navp-drawer-open').addEventListener('click', function () { navDrawerOpen(true); });

  // Pinch to zoom. The map only ever draws integer slippy zooms, so the
  // gesture is measured continuously and committed each time it crosses a
  // level — the same stepping the auto-fit does as a flight closes in.
  //
  // The base zoom is captured once, at touchdown, and every move is measured
  // against THAT. Reading the current zoom each move would ratchet: each
  // committed step would become the new baseline and a slow steady pinch
  // would run away to the clamp.
  // Pointer events, not touch events: every working control on this panel is
  // wired on pointerdown (see wireHold), and a touchstart/touchmove version of
  // this never fired a single handler on the real hardware.
  (function () {
    // Bound on the map CONTAINER, not the canvas. The canvas is one child among
    // overlays and the rail, and anything sitting over it — a hidden panel that
    // is not quite hidden, a stray padding box — silently eats the gesture.
    // The container is the whole map area and cannot be missed.
    var cv = $('navp-live');
    var live = [];                      // active pointer ids, in contact order
    var pos = {};                       // id -> {x, y}
    var span0 = 0, z0 = 0, zLast = 0;

    function span() {
      var a = pos[live[0]], b = pos[live[1]];
      if (!a || !b) return 0;
      var dx = a.x - b.x, dy = a.y - b.y;
      return Math.sqrt(dx * dx + dy * dy);
    }

    function arm() {
      span0 = span();
      // Starting from the auto-fit's current level means the first pinch
      // nudges from what is on screen, exactly like the +/- buttons.
      z0 = zLast = (navZoom === null ? navLastZ : navZoom);
    }

    cv.addEventListener('pointerdown', function (e) {
      if (e.pointerType === 'mouse') return;   // one mouse cannot pinch
      if (live.indexOf(e.pointerId) === -1) live.push(e.pointerId);
      pos[e.pointerId] = { x: e.clientX, y: e.clientY };
      if (live.length === 2) { e.preventDefault(); arm(); }
    });

    cv.addEventListener('pointermove', function (e) {
      if (!pos[e.pointerId]) return;
      pos[e.pointerId] = { x: e.clientX, y: e.clientY };
      if (live.length !== 2 || !span0) return;
      e.preventDefault();
      var now = span();
      if (now < 8) return;              // fingers together: the ratio goes wild
      // Clamp before comparing, not after. Tracking the requested level past
      // the limit would mean a pinch that overshot had to be given back the
      // overshoot before the map moved again.
      var z = navClampZ(Math.round(z0 + Math.log(now / span0) / Math.LN2));
      if (z !== zLast) {
        zLast = z;
        navSetZoom(z);                  // drops FIT back off
      }
    });

    function lift(e) {
      var i = live.indexOf(e.pointerId);
      if (i !== -1) live.splice(i, 1);
      delete pos[e.pointerId];
      span0 = 0;
      // Lifting one finger of three leaves two still down; re-arm from where
      // they are now, or the next move would be measured against a span that
      // included a finger no longer on the glass.
      if (live.length === 2) arm();
    }
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (t) {
      cv.addEventListener(t, lift);
    });
  })();

  // Flight-plan search overlay. The keyboard it uses is built once at the top
  // of wireNav, not here as well.
  $('navp-add').addEventListener('click', function () { navSearchOpen(true, 'wpt'); });
  $('navp-close').addEventListener('click', function () { navSearchOpen(false); });
  // SAVE sits next to ADD, not behind PLANS. Saving was reachable only by
  // opening a list of saved plans and finding a button at the bottom of it,
  // which reads as "load a plan" — so the feature existed and looked absent.
  $('navp-save').addEventListener('click', function () {
    if (!navPlan.length) return;
    navSearchOpen(true, 'save');
  });
  $('navp-plansbtn').addEventListener('click', function () { navPlansOpen(true); });
  $('navp-sync').classList.toggle('on', navSync);
  $('navp-sync').addEventListener('click', function () {
    navSync = !navSync;
    navSyncWps = {}; navSyncCount = 0; navSyncGpsIndex = null; navSyncInvalid = false;
    try { localStorage.setItem('navSync', navSync ? '1' : '0'); } catch (e) { }
    $('navp-sync').classList.toggle('on', navSync);
    $('navp-sync').classList.remove('bad');
    $('navp-sync').textContent = 'SYNC';
  });
  $('navp-plans-close').addEventListener('click', function () { navPlansOpen(false); });
  $('navp-saveas').addEventListener('click', function () {
    navPlansOpen(false);
    navSearchOpen(true, 'save');
  });
}
