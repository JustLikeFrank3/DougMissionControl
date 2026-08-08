/* sim.js — the SIM surface: observed aircraft state, and the controls that
   command it.

   The rule this file exists to keep: the panel NEVER draws the commanded
   state. A tap shows PENDING over the last observed value, and only the
   simulator moving it changes what is displayed. That is what makes a command
   MSFS declined — over gear speed, on the ground, wrong aircraft — read as a
   failed command instead of a lie. */

import { $, wireTap, wireHold, post } from './ui.js';
import { fmtAgo } from './format.js';
import { simBuildGauges, simPaintGauges } from './gauges.js';
import { missionUpdate, paintMissionStrip } from './nav.js';

/* ── SIM ───────────────────────────────────────────────────────────────
   Polled directly rather than carried on the SSE state. Gear travel takes
   about five seconds end to end and deck-api's state poll runs at 2-5 s, so
   the interesting part — the gear in motion — would fall between samples.
   Only runs while the surface is on screen: there is no reason to hold a
   4 Hz conversation with the workstation to feed a hidden panel.

   Everything below renders OBSERVED state. The agent reports what the
   simulator is doing, never what it was told to do. */
var SIM_MS = 250;
var simTimer = null, simLastOk = 0;
var simPhProse = null;

export function simPoll(on) {
  if (on && !simTimer) {
    simTick();
    simTimer = setInterval(simTick, SIM_MS);
  } else if (!on && simTimer) {
    clearInterval(simTimer);
    simTimer = null;
  }
}

export function simTick() {
  // The catch covers the FETCH only. It used to wrap paintSim too, so any
  // exception while painting — a field the agent stopped sending, a shape
  // that drifted — was repainted as "NO LINK" and read as a missing agent.
  // That sent me chasing a dead sim agent that was answering perfectly.
  // A paint bug now surfaces as an unhandled rejection in the console, where
  // it is a bug, and only a real network failure claims the link is down.
  fetch('/api/sim', { cache: 'no-store' })
    .then(function (r) { return r.json(); })
    .catch(function () {
      paintSim({ link: false, reason: 'deck-api unreachable' });
      return null;
    })
    .then(function (d) { if (d) { simLastOk = Date.now(); paintSim(d); } });
}

/* Commands. The panel NEVER draws the commanded position. A command puts
   PENDING over the last observed state; only the state stream moves what is
   displayed. If nothing moves before the timeout the panel says NO RESPONSE
   and keeps showing the observed position — which is how a command the
   simulator ignored reads as a failed command instead of a lie. */
var SIM_TIMEOUT = { gear: 9000, flaps: 5000, parking_brake: 3000,
                    landing_lights: 3000, ap_master: 3000 };
var simPending = {};          // control -> { id, want, from, sent, dead, reason }
var simSeq = 0;
var simLast = null;

// The observed position of a control, in the same vocabulary the buttons use.
var AP_VARS = {
  ap_hdg: { field: 'deg', min: 0, max: 359, wrap: true },
  ap_alt: { field: 'ft', min: 0, max: 60000 },
  ap_vs: { field: 'fpm', min: -8000, max: 8000 },
  ap_spd: { field: 'kt', min: 0, max: 900 }
};
function simObserved(control, c) {
  c = c || {};
  if (control === 'gear') return c.gear ? c.gear.state : null;
  if (control === 'flaps') return c.flaps ? String(c.flaps.index) : null;
  if (control === 'parking_brake') return c.parking_brake ? c.parking_brake.state : null;
  if (control === 'landing_lights') return c.landing_lights ? c.landing_lights.state : null;
  if (control === 'ap_master') return c.ap_master ? c.ap_master.state : null;
  if (control === 'com1' || control === 'com2' || control === 'nav1' || control === 'nav2')
    return c[control] ? c[control].sby.toFixed(3) : null;
  if (control === 'xpdr') return c.xpdr ? c.xpdr.code : null;
  if (control === 'baro') return c.baro ? c.baro.inhg.toFixed(2) : null;
  var ap = AP_VARS[control];
  if (ap) return c[control] ? String(c[control][ap.field]) : null;
  return null;
}

export function simSend(control, action, value) {
  var c = (simLast && simLast.controls) || {};
  var id = 'c-' + (++simSeq);
  // `from` is what it looked like when we asked — the only way to tell that
  // an incr/decr moved, since we cannot know the target detent up front.
  simPending[control] = { id: id, want: value || null,
                          from: simObserved(control, c),
                          sent: Date.now(), dead: false, reason: null };
  post('/api/sim/command',
       { cmd_id: id, control: control, action: action, value: value })
    .then(function (r) {
      var p = simPending[control];
      if (!p || p.id !== id) return;
      if (!r || !r.accepted) {
        p.dead = true;
        p.reason = (r && r.reason) || 'no reply';
      } else if (r.noop) {
        // Already in the requested position: the agent transmitted nothing,
        // so nothing is going to move and PENDING would hang forever.
        delete simPending[control];
      }
    });
}

function simRenderPending(key, control, c) {
  var el = $('simpd-' + key);
  var p = simPending[control];
  if (!p) { el.className = 'simpend'; el.textContent = ''; return; }

  var now = simObserved(control, c);
  var moved = p.want ? (now === p.want) : (now !== null && now !== p.from);
  if (moved) { delete simPending[control]; el.className = 'simpend'; el.textContent = ''; return; }

  var age = Date.now() - p.sent;
  if (p.dead) {
    el.className = 'simpend dead';
    el.textContent = 'REJECTED · ' + String(p.reason).toUpperCase();
    if (age > 6000) delete simPending[control];
    return;
  }
  var limit = SIM_TIMEOUT[control] || 4000;
  if (age > limit) {
    el.className = 'simpend dead';
    el.textContent = 'NO RESPONSE · SHOWING OBSERVED';
    if (age > limit + 6000) delete simPending[control];
    return;
  }
  el.className = 'simpend wait';
  el.textContent = 'PENDING' + (p.want ? ' · ' + p.want.toUpperCase() : '');
}

// Absent from `controls` means the aircraft has not got it — skids instead of
// wheels, no flaps. Grey it and name it; never render a dead control.
function simAvail(key, present) {
  $('simt-' + key).classList.toggle('na', !present);
  [].forEach.call($('simt-' + key).querySelectorAll('.simbtn, .simstep'), function (b) {
    b.disabled = !present;
  });
  if (!present) {
    $('simv-' + key).textContent = 'NOT FITTED';
    var s = $('sims-' + key); if (s) s.textContent = '';
    var u = $('simu-' + key); if (u) u.textContent = '';
  }
  return present;
}

function simSimple(key, present, value, onWord) {
  if (!simAvail(key, present)) return;
  var v = $('simv-' + key);
  v.textContent = value.toUpperCase();
  v.className = 'simt-v ' + (value === onWord ? 'sim-on' : 'sim-off');
  $('sims-' + key).textContent = value === onWord ? 'active' : 'inactive';
}

export function paintSim(d) {
  d = d || {};
  var st = d.session ? d.state : null;
  var live = !!(st && st.controls);

  $('sim-ph').hidden = live;
  $('sim-live').hidden = !live;
  $('sim-pill').textContent = !d.link ? 'NO LINK' : (d.session ? 'LINK' : 'NO SIM');

  if (simPhProse === null) simPhProse = $('sim-ph-s').innerHTML;
  // A missing agent or a sim that is not running are ordinary absences and
  // keep the standing copy. A rejected token or an HTTP error is a fault
  // worth reading off the panel.
  var fault = d.reason && /token|returned/.test(d.reason);
  $('sim-ph-s').innerHTML = fault ? d.reason : simPhProse;
  if (!live) return;

  simLast = st;
  $('sim-live').classList.toggle('stale', Date.now() - simLastOk > 1500);
  $('sim-ac').textContent = st.aircraft || '(no aircraft)';
  $('sim-meta').textContent = 'SEQ ' + st.seq;

  var c = st.controls || {};

  if (simAvail('gear', !!c.gear)) {
    var g = c.gear;
    var v = $('simv-gear');
    v.textContent = g.state.toUpperCase();
    v.className = 'simt-v ' +
      (g.state === 'transit' ? 'sim-mid' : g.state === 'down' ? 'sim-on' : 'sim-off');
    $('simu-gear').textContent = g.pct.toFixed(1) + '%';
    $('sims-gear').textContent =
      'HANDLE ' + g.handle.toUpperCase() + ' · OBSERVED ' + g.state.toUpperCase();
    var bar = $('simb-gear');
    bar.style.width = g.pct + '%';
    bar.className = g.state === 'transit' ? 'sim-mid' : '';

    // The handle says one thing, the gear reports another, and nothing is
    // moving: the simulator declined the command — over the gear speed
    // limit, on the ground, whatever. Observed at 205-228 kt in a King Air,
    // where drawing the commanded position would have read GEAR DOWN.
    var stuck = g.state !== 'transit' && g.handle !== g.state;
    $('simw-gear').classList.toggle('on', stuck);
    if (stuck) {
      $('simw-gear').textContent =
        'COMMANDED ' + g.handle.toUpperCase() + ' · NOT MOVING · SIM DECLINED';
    }
  }

  if (simAvail('flaps', !!c.flaps)) {
    var f = c.flaps;
    var fv = $('simv-flaps');
    fv.textContent = f.index + ' / ' + f.detents;
    fv.className = 'simt-v ' + (f.index > 0 ? 'sim-on' : 'sim-off');
    $('simu-flaps').textContent = f.angle_deg.toFixed(1) + '°';
    $('sims-flaps').textContent = f.index === 0 ? 'UP / CLEAN' : 'DETENT ' + f.index;
    var pips = $('simp-flaps');
    if (pips.children.length !== f.detents) {
      pips.innerHTML = '';
      for (var i = 0; i < f.detents; i++) pips.appendChild(document.createElement('b'));
    }
    for (var j = 0; j < pips.children.length; j++) {
      pips.children[j].className = j < f.index ? 'f' : '';
    }
  }

  simSimple('park', !!c.parking_brake, c.parking_brake ? c.parking_brake.state : '', 'set');
  simSimple('lights', !!c.landing_lights, c.landing_lights ? c.landing_lights.state : '', 'on');
  simSimple('ap', !!c.ap_master, c.ap_master ? c.ap_master.state : '', 'engaged');

  if (simAvail('aphdg', !!c.ap_hdg)) {
    $('simv-aphdg').textContent = ('00' + c.ap_hdg.deg).slice(-3) + '°';
  }
  if (simAvail('apalt', !!c.ap_alt)) {
    $('simv-apalt').textContent = c.ap_alt.ft;
  }
  if (simAvail('apvs', !!c.ap_vs)) {
    $('simv-apvs').textContent = (c.ap_vs.fpm > 0 ? '+' : '') + c.ap_vs.fpm;
  }
  if (simAvail('apspd', !!c.ap_spd)) {
    $('simv-apspd').textContent = c.ap_spd.kt;
  }

  var r = st.readouts || {};
  missionUpdate(d);
  paintMissionStrip();
  cxPaint(c);
  $('simv-ias').textContent = r.ias_kt;
  $('simv-alt').textContent = r.alt_ft;
  $('simv-hdg').textContent = ('00' + r.hdg_mag).slice(-3);
  // Second-batch readouts: an agent that predates them sends nothing, and
  // a dash reads as "not reported" where 'undefined' would read as broken.
  $('simv-vs').textContent = r.vs_fpm === undefined ? '—'
    : (r.vs_fpm > 0 ? '+' : '') + r.vs_fpm;
  // Twins report both engines; singles report rpm_2 = 0 and show one figure.
  var rpmEl = $('simv-rpm');
  rpmEl.textContent = r.rpm_1 === undefined ? '—'
    : r.rpm_2 > 0 ? r.rpm_1 + ' / ' + r.rpm_2 : String(r.rpm_1);
  rpmEl.className = 'simt-v big' + (r.rpm_2 > 0 ? ' twin' : '');
  $('simu-rpm').textContent = r.rpm_2 > 0 ? 'ENG 1 / 2' : '';
  $('simv-fuel').textContent = r.fuel_gal === undefined ? '—' : r.fuel_gal;

  // Throttle: one row per engine, however many the airframe has. The agent
  // sends an array sized by NUMBER OF ENGINES rather than a fixed pair, so a
  // single, a twin and a 747 all render here with no special case — and a
  // shut-down engine still shows its lever at zero instead of disappearing.
  var thr = r.throttles;
  var bars = $('thr-bars');
  if (!thr || !thr.length) {
    bars.innerHTML = '';
    $('simu-thr').textContent = '';
  } else {
    $('simu-thr').textContent = thr.length > 1 ? thr.length + ' ENG · %' : '%';
    bars.className = 'thr' + (thr.length > 2 ? ' n4' : '');
    if (bars.children.length !== thr.length) {
      bars.innerHTML = '';
      thr.forEach(function (_, i) {
        var row = document.createElement('div');
        row.className = 'r';
        row.innerHTML = '<b></b><i></i><s></s>';
        row.querySelector('b').textContent = thr.length > 1 ? String(i + 1) : '';
        bars.appendChild(row);
      });
    }
    thr.forEach(function (v, i) {
      var row = bars.children[i];
      var pct = Math.max(0, Math.min(100, v));
      row.querySelector('i').style.setProperty('--p', pct + '%');
      row.querySelector('i').classList.toggle('max', v >= 99);
      row.querySelector('s').textContent = v + '%';
    });
  }

  simPaintGauges(r);

  simRenderPending('gear', 'gear', c);
  simRenderPending('flaps', 'flaps', c);
  simRenderPending('park', 'parking_brake', c);
  simRenderPending('lights', 'landing_lights', c);
  simRenderPending('ap', 'ap_master', c);
  simRenderPending('aphdg', 'ap_hdg', c);
  simRenderPending('apalt', 'ap_alt', c);
  simRenderPending('apvs', 'ap_vs', c);
  simRenderPending('apspd', 'ap_spd', c);
  ['com1', 'com2', 'nav1', 'nav2', 'xpdr', 'baro'].forEach(function (k) {
    simRenderPending(k, k, c);
  });
}

/* The comms drawer. Rows exist only for radios the airframe declares —
   a Cub never shows a NAV2, and never shows a greyed one either. */
function cxPaint(c) {
  ['com1', 'com2', 'nav1', 'nav2'].forEach(function (k) {
    var row = $('cx-' + k), has = !!c[k];
    row.hidden = !has;
    if (!has) return;
    $('cx-' + k + '-act').textContent = c[k].act.toFixed(3);
    $('cx-' + k + '-sby').textContent = c[k].sby.toFixed(3);
  });
  var x = c.xpdr;
  $('cx-xpdr').hidden = !x;
  if (x) {
    $('cx-xpdr-mode').textContent = x.mode.toUpperCase();
    $('cx-xpdr-code').textContent = x.code;
  }
  var b = c.baro;
  $('cx-baro').hidden = !b;
  if (b) {
    $('cx-baro-hpa').textContent = b.hpa + ' hPa';
    $('cx-baro-v').textContent = b.inhg.toFixed(2);
  }
}

/* ── wiring ─────────────────────────────────────────────────────────────── */

export function wireSim() {
  simBuildGauges();
  // Instant, except the gear.
  //
  // Everything on this surface is reversible and observed, so a mis-tap shows
  // as state moving and is undone by tapping the other way — which is why the
  // rest fire on touch. Firing on PRESS also means sliding a finger off no
  // longer cancels: the command has already gone. For a flap notch or a
  // heading bug that is the right trade. For the gear it is not — it is the
  // one control here with a real airframe consequence, it is not something you
  // press repeatedly, and a hold costs nothing when you use it twice a flight.
  [].forEach.call(document.querySelectorAll('.simbtn'), function (b) {
    var fire = function () {
      simSend(b.dataset.c, b.dataset.a, b.dataset.v || null);
    };
    if (b.dataset.c === 'gear') wireHold(b, fire); else wireTap(b, fire);
  });

  // AP bug steppers. Absolute set computed from the observed value at tap
  // time: a dropped command leaves the bug where it was, never double-steps.
  [].forEach.call(document.querySelectorAll('.simstep'), function (b) {
    wireTap(b, function () {
      var ctl = b.dataset.c;
      var ap = AP_VARS[ctl];
      var c = (simLast && simLast.controls) || {};
      if (!c[ctl]) return;
      var v = c[ctl][ap.field] + Number(b.dataset.d);
      v = ap.wrap ? ((v % 360) + 360) % 360
                  : Math.max(ap.min, Math.min(ap.max, v));
      simSend(ctl, 'set', String(v));
    });
  });

  // Doug's one numeric entry system. Options: title, unit, min, max,
  // decimals (0 = integers), allowNeg, hint, validate(v, str) -> bool,
  // onSet(v). Every number typed on this panel goes through here — AP bugs
  // today, frequencies and squawk below, train throttles someday.
  var numOpts = null, numStr = '', numNeg = false;

  function numValue() { return (numNeg ? -1 : 1) * parseFloat(numStr || '0'); }

  function numOk() {
    if (numStr === '' || numStr === '.') return false;
    var v = numValue();
    if (v < numOpts.min || v > numOpts.max) return false;
    return !numOpts.validate || numOpts.validate(v, numStr);
  }

  function numPaint() {
    var bad = numStr !== '' && !numOk();
    $('np-val').textContent = (numNeg ? '\u2212' : '') + (numStr || '\u00a0');
    var hint = $('np-hint');
    var range = numOpts.hint || (numOpts.min + ' \u2013 ' + numOpts.max);
    hint.textContent = bad ? 'OUT OF RANGE \u00b7 ' + range : range;
    hint.className = 'np-hint' + (bad ? ' bad' : '');
  }

  function showNumericInput(o) {
    numOpts = o; numStr = ''; numNeg = false;
    $('numpad-title').textContent = o.title;
    $('np-unit').textContent = o.unit || '';
    $('num-pad').hidden = false;
    numPaint();
  }

  (function () {
    var keys = $('np-keys');
    ['7', '8', '9', '4', '5', '6', '1', '2', '3', '\u00b1', '0', '.'].forEach(function (k) {
      var b = document.createElement('button');
      b.textContent = k;
      b.addEventListener('click', function () {
        if (!numOpts) return;
        if (k === '\u00b1') { if (numOpts.allowNeg) numNeg = !numNeg; }
        else if (k === '.') {
          if (numOpts.decimals > 0 && numStr.indexOf('.') === -1) numStr += '.';
        } else if (numStr.length < 7) {
          // No more digits after the point than the field means.
          var dot = numStr.indexOf('.');
          if (dot === -1 || numStr.length - dot <= (numOpts.decimals || 0)) numStr += k;
        }
        numPaint();
      });
      keys.appendChild(b);
    });
    var back = document.createElement('button');
    back.textContent = '\u232b';
    back.addEventListener('click', function () { numStr = numStr.slice(0, -1); numPaint(); });
    keys.appendChild(back);
    var set = document.createElement('button');
    set.textContent = 'SET';
    set.className = 'go';
    set.style.gridColumn = '1 / -2';
    set.addEventListener('click', function () {
      if (!numOk()) return;
      $('num-pad').hidden = true;
      numOpts.onSet(numValue(), numStr);
    });
    keys.appendChild(set);
    $('np-close').addEventListener('click', function () { $('num-pad').hidden = true; });
  })();

  // AP bugs on the dialog. Tap the value, type the target — "fly heading
  // 240" is three keys, not fifteen taps of +10.
  var AP_PAD = {
    ap_hdg: { title: 'AP HDG', unit: '\u00b0', min: 0, max: 359, key: 'aphdg' },
    ap_alt: { title: 'AP ALT', unit: 'FT', min: 0, max: 60000, key: 'apalt' },
    ap_vs: { title: 'AP V/S', unit: 'FPM', min: -8000, max: 8000, allowNeg: true,
             hint: '\u00b18000 \u00b7 \u00b1 flips climb/descend', key: 'apvs' },
    ap_spd: { title: 'AP SPD', unit: 'KT', min: 0, max: 900, key: 'apspd' }
  };
  Object.keys(AP_PAD).forEach(function (ctl) {
    var o = AP_PAD[ctl];
    var el = $('simv-' + o.key);
    el.classList.add('tap');
    el.addEventListener('click', function () {
      var c = (simLast && simLast.controls) || {};
      if (!c[ctl]) return;                  // no autopilot, no pad
      showNumericInput({ title: o.title, unit: o.unit, min: o.min, max: o.max,
        decimals: 0, allowNeg: !!o.allowNeg, hint: o.hint,
        onSet: function (v) { simSend(ctl, 'set', String(Math.round(v))); } });
    });
  });

  // Comms drawer. Standby values route through the same dialog; SWAP is a
  // tap — it mirrors the real avionics flow and is reversible by tapping
  // again, so hold-to-confirm would only slow the radio call down.
  $('comms-btn').addEventListener('click', function () { $('comms').hidden = false; });
  $('comms-close').addEventListener('click', function () { $('comms').hidden = true; });

  [['com1', 'COM1', 118, 136.99], ['com2', 'COM2', 118, 136.99],
   ['nav1', 'NAV1', 108, 117.95], ['nav2', 'NAV2', 108, 117.95]]
    .forEach(function (def) {
      var ctl = def[0];
      $('cx-' + ctl + '-sby').addEventListener('click', function () {
        var c = (simLast && simLast.controls) || {};
        if (!c[ctl]) return;
        showNumericInput({ title: def[1] + ' STANDBY', unit: 'MHZ',
          min: def[2], max: def[3], decimals: 3,
          onSet: function (v) { simSend(ctl, 'set_sby', v.toFixed(3)); } });
      });
      $('cx-' + ctl + '-swap').addEventListener('click', function () {
        simSend(ctl, 'swap', null);
      });
    });

  $('cx-xpdr-code').addEventListener('click', function () {
    var c = (simLast && simLast.controls) || {};
    if (!c.xpdr) return;
    showNumericInput({ title: 'SQUAWK', unit: '', min: 0, max: 7777, decimals: 0,
      hint: '4 digits \u00b7 0-7 each',
      validate: function (v, s) { return !/[89]/.test(s); },
      onSet: function (v) { simSend('xpdr', 'set', ('0000' + Math.round(v)).slice(-4)); } });
  });
  // IDENT has no observable state to confirm against, so it bypasses the
  // pending machinery: fire and trust the sim, like a real IDENT button.
  $('cx-xpdr-ident').addEventListener('click', function () {
    post('/api/sim/command', { cmd_id: 'ident-' + (++simSeq), control: 'xpdr', action: 'ident' });
  });

  $('cx-baro-v').addEventListener('click', function () {
    var c = (simLast && simLast.controls) || {};
    if (!c.baro) return;
    showNumericInput({ title: 'ALTIMETER', unit: 'INHG', min: 26, max: 32, decimals: 2,
      onSet: function (v) { simSend('baro', 'set', v.toFixed(2)); } });
  });
  $('cx-baro-std').addEventListener('click', function () {
    simSend('baro', 'set', '29.92');
  });
}
