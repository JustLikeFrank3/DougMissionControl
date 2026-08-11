/* ui.js — the primitives every surface is built from: element lookup, the
   key/value rows, hold-to-arm, and the one way this panel talks to deck-api.

   Kept apart from the surfaces because all of them need it and none of them
   should own it. Everything here is deliberately small: on a Pi 4B the browser
   is the most expensive process on the box. */

/** Hold-to-arm duration. Long enough that a brush past a tile cannot boot a
    machine, short enough that a deliberate press does not feel broken. */
export const HOLD_MS = 1500;

export const $ = (id) => document.getElementById(id);

/** Fill a container with label/value rows. */
export function rows(el, pairs) {
  el.innerHTML = '';
  pairs.forEach(function (p) {
    var r = document.createElement('div');
    r.className = 'row';
    r.innerHTML = '<span class="k"></span><span class="v"></span>';
    r.querySelector('.k').textContent = p[0];
    r.querySelector('.v').textContent = p[1];
    el.appendChild(r);
  });
}

/**
 * Hold-to-arm on a control, with the fill sweep as the progress indicator.
 *
 * Pointer events, not touch events: this panel's Chromium delivers pointer
 * events and not Touch Events, so a touchstart-based version of any gesture
 * here never fires at all. Everything interactive in Flight Deck is built on
 * this fact — see the pinch handler on the NAV map.
 *
 * A disabled element is refused outright, so a control the aircraft has not
 * got cannot be armed.
 */
export function wireHold(el, fire) {
  var raf = null, start = 0;

  function tick() {
    var pct = Math.min(1, (performance.now() - start) / HOLD_MS);
    el.querySelector('.hold').style.width = (pct * 100) + '%';
    if (pct >= 1) { stop(); fire(); return; }
    raf = requestAnimationFrame(tick);
  }

  function begin(e) {
    if (el.disabled) return;
    e.preventDefault();
    el.classList.add('arming');
    start = performance.now();
    raf = requestAnimationFrame(tick);
  }

  function stop() {
    if (raf) cancelAnimationFrame(raf);
    raf = null;
    el.classList.remove('arming');
    el.querySelector('.hold').style.width = '0';
  }

  el.addEventListener('pointerdown', begin);
  ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (t) {
    el.addEventListener(t, stop);
  });
}

/**
 * Fire on touch-down, immediately.
 *
 * Hold-to-arm exists so a stray touch cannot reboot a machine someone is
 * working on. Nothing in a simulator carries that weight: a flap detent, a
 * heading bug, a landing light are all reversible, incremental, and pressed
 * repeatedly — a 1.5 s hold between each step of the flaps is an obstacle
 * with nothing on the other side of it.
 *
 * pointerdown rather than click: this panel speaks pointer events, and firing
 * on press is what makes a control feel like a switch instead of a web page.
 * The `tapped` class gives the press somewhere to show, since these buttons
 * have no fill sweep to watch.
 */
export function wireTap(el, fire) {
  el.addEventListener('pointerdown', function (e) {
    if (el.disabled) return;
    e.preventDefault();
    el.classList.add('tapped');
    fire();
  });
  ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (t) {
    el.addEventListener(t, function () { el.classList.remove('tapped'); });
  });
}

/**
 * POST JSON to deck-api. Never throws and never rejects: a command that did
 * not reach the Pi resolves to null, and every caller renders that as the
 * command not having happened. The panel has no error dialog by design —
 * an unanswered command shows as observed state failing to move.
 */
export function post(path, body) {
  return fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  }).then(function (r) { return r.json(); }).catch(function () { return null; });
}

/** GET JSON from deck-api, with the same never-throws contract. */
export function getJSON(path) {
  return fetch(path, { cache: 'no-store' })
    .then(function (r) { return r.json(); })
    .catch(function () { return null; });
}

/* Display preference: show airspeed as KT or MACH. Presentation only — it
   never touches a command — so it lives here and survives a kiosk reload. */
var spdUnit = null;
export function speedUnit() {
  if (spdUnit === null) {
    try { spdUnit = localStorage.getItem('deck-spd-unit') || 'kt'; }
    catch (e) { spdUnit = 'kt'; }
  }
  return spdUnit;
}
export function speedUnitToggle() {
  spdUnit = speedUnit() === 'kt' ? 'mach' : 'kt';
  try { localStorage.setItem('deck-spd-unit', spdUnit); } catch (e) { /* tmpfs kiosk */ }
  return spdUnit;
}
/** "M.85" from 0.8449, or a dash when the agent predates mach. */
export function fmtMach(mach) {
  return typeof mach === 'number' ? 'M' + mach.toFixed(2).slice(1) : '—';
}
