/* screens.js — the SCREENS surface: one card per monitor, one button per
   input the panel declares.

   Talks only to deck-api, which routes to whichever agent the booted OS is
   running — the sim agent under Windows, media-agent under Linux. The panel
   never learns which, and must not: the two publish the same shape precisely
   so this file does not have to care. */

import { $, wireHold, post, getJSON } from './ui.js';

/* ── SCREENS (DDC via the Windows agent) ───────────────────────────────
   One card per physical monitor. Each pill shows the input that monitor
   REPORTS on read-back — never the commanded one. A switch a panel
   ignored shows up as the pill not moving, which is the truth. */
var DSP_INPUTS = [
  ['vga', 'VGA'], ['hdmi1', 'HDMI 1'], ['hdmi2', 'HDMI 2'],
  ['dp1', 'DP 1'], ['dp2', 'DP 2']
];
var dspTimer = null, dspSig = null;
var dspDefaults = null;   // deck-api's stated fallback for silent panels

/**
 * The inputs to offer for one monitor.
 *
 * A panel that declares its own inputs wins outright. One that refuses — the
 * HP 32f refuses the capabilities request flatly — falls back to deck-api's
 * operator-stated list, which is a fact about this desk rather than a guess
 * from a model table.
 *
 * Whatever that list says, the input the monitor is CURRENTLY on is always
 * offered. The fallback was written for two identical HPs and names no
 * DisplayPort, so when a third panel arrived on DP the card showed VGA,
 * HDMI 1 and HDMI 2 — one tap from moving that monitor to HDMI with no
 * button anywhere on the panel to bring it back. A one-way door to the far
 * side of the desk. An input a monitor is demonstrably sitting on is an
 * input that monitor has, whatever any fallback list forgot to mention.
 */
var dspSeen = {};      // monitor -> every input it has been observed on

function dspAllowed(m) {
  // Remembered, not just current. Offering only the input a monitor is on
  // RIGHT NOW is useless the moment you switch it: this Samsung sat on DP,
  // was switched to HDMI 2, and its declared list - which omits DisplayPort
  // entirely, as plenty of panels do - then became the whole truth. DP
  // vanished from the card while the monitor was still physically wired to
  // it, and the only way back was the OSD on the monitor itself.
  //
  // An input this panel has SEEN a monitor on is an input that monitor has,
  // and no capabilities string gets to overrule the evidence.
  var key = m.index + ':' + (m.desc || '');
  var seen = dspSeen[key] || (dspSeen[key] = {});
  // 'other' is the agents' word for a code neither of them recognises. There
  // is no button for it, so it cannot be offered and cannot be returned to.
  if (m.input && m.input !== 'other') seen[m.input] = true;

  var list = m.inputs || dspDefaults;
  if (!list) return null;                       // null means offer everything
  list = list.slice();
  Object.keys(seen).forEach(function (i) {
    if (list.indexOf(i) === -1) list.push(i);
  });
  return list;
}

export function dspPoll(on) {
  if (on && !dspTimer) { dspTick(); dspTimer = setInterval(dspTick, 3000); }
  else if (!on && dspTimer) { clearInterval(dspTimer); dspTimer = null; }
}

export function dspTick() {
  fetch('/api/monitor', { cache: 'no-store' })
    .then(function (r) { return r.json(); })
    // Fetch only — a paint exception must not be reported as a dead link.
    .catch(function () {
      paintDsp({ available: false, reason: 'deck-api unreachable' });
      return null;
    })
    .then(function (d) { if (d) paintDsp(d); });
}

export function paintDsp(d) {
  d = d || {};
  var mons = d.monitors || [];
  var live = d.available && mons.length > 0;
  $('dsp-ph').hidden = live;
  $('dsp-live').hidden = !live;
  $('dsp-pill').textContent = d.available ? 'NO MONITORS' : 'NO LINK';
  $('nav-displays').textContent = live ? mons.length + ' via ddc'
    : (d.available ? 'none' : 'no link');
  if (!live) return;

  dspDefaults = (d.default_inputs && d.default_inputs.length) ? d.default_inputs : null;

  var box = $('dsp-live');
  // position and size join the signature: the cards are ordered by desktop
  // geometry, so a monitor moving or a display server appearing has to
  // rebuild them, not just repaint the pills.
  var sig = mons.map(function (m) {
    return m.index + ':' + m.desc + ':' + (m.position || '') + ':' +
      (m.w || '') + 'x' + (m.h || '') +
      ':' + (dspAllowed(m) || []).join(',');
  }).join('|');
  if (sig !== dspSig) {
    dspSig = sig;
    box.innerHTML = '';
    mons.forEach(function (m) {
      var card = document.createElement('div');
      card.className = 'dspcard';
      card.dataset.idx = m.index;
      card.innerHTML =
        '<div class="h-top"><span class="h-name"></span>' +
        '<span class="h-pill dsp-obs">—</span></div>' +
        '<div class="dsp-desc"></div><div class="dsp-btns"></div>' +
        // The trap, stated on the card. Sending a monitor to an input with
        // nothing plugged into it does not just blank it - it takes the DDC
        // link with it, and the only way back is that monitor's own OSD.
        '<div class="dsp-warn">an input with nothing on it takes DDC with it' +
        ' \u00b7 recover at the monitor</div>';
      // Name from the panel's own EDID model where it gives one — Windows
      // says "Generic PnP Monitor" for plenty of screens, so fall back to
      // the geometry-derived position rather than inventing a model.
      //
      // It used to fall back to a hardcoded 'HP32F', which was true of every
      // monitor on this desk right up until it wasn't: a Samsung arrived and
      // the panel labelled it HP32F. A card that names the wrong screen is
      // worse than one that declines to name it, because the name is what you
      // steer by when you are about to switch an input.
      var model = (m.desc && !/generic/i.test(m.desc)) ? m.desc : '';
      card.querySelector('.h-name').textContent =
        (model ? model + ' ' : 'MON ') + (m.position || (m.index + 1));
      // Every geometry field is optional. DRM knows the resolution without a
      // display server, but only the display server knows where a panel sits
      // on the desktop, and a Wayland compositor may refuse to say — so the
      // agents send what they have. Build the line from the parts that
      // arrived instead of assuming all of them did, which is what rendered
      // a literal "at xundefined" for the Linux agent.
      // The desktop x offset is deliberately NOT shown. It exists to sort the
      // cards and to derive LEFT/RIGHT, and once the card is titled with that
      // position it says nothing a person standing at the desk can use — "at
      // x1920" is a fact about the framebuffer, not about the monitor.
      var bits = [];
      if (m.w && m.h) bits.push(m.w + '×' + m.h);
      if (m.primary) bits.push('primary');
      card.querySelector('.dsp-desc').textContent = bits.length ? bits.join(' · ') : '—';
      var btns = card.querySelector('.dsp-btns');
      var allowed = dspAllowed(m);
      var offer = DSP_INPUTS.filter(function (inp) {
        return !allowed || allowed.indexOf(inp[0]) !== -1;
      });
      btns.style.gridTemplateColumns = 'repeat(' + offer.length + ', 1fr)';
      offer.forEach(function (inp) {
        var b = document.createElement('button');
        b.className = 'simbtn';
        b.innerHTML = '<span></span><i class="hold"></i>';
        b.querySelector('span').textContent = inp[1];
        wireHold(b, function () {
          post('/api/monitor', { input: inp[0], index: m.index })
            .then(function () { setTimeout(dspTick, 1500); });
        });
        btns.appendChild(b);
      });
      box.appendChild(card);
    });
  }
  mons.forEach(function (m) {
    var card = box.querySelector('.dspcard[data-idx="' + m.index + '"]');
    if (!card) return;
    var pill = card.querySelector('.dsp-obs');
    pill.textContent = !m.ddc ? 'NO DDC'
      : (m.input && m.input !== 'other') ? m.input.toUpperCase()
      : 'INPUT ' + m.input_raw;
    pill.className = 'h-pill dsp-obs ' + (m.ddc ? 'ok' : 'off');
  });
}
