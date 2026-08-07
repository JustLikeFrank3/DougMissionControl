/* gauges.js — the two EFIS instruments on SIM: attitude and compass rose.

   Built once as SVG and then only transformed, because rebuilding a pitch
   ladder every frame on a Pi 4B is the kind of thing that shows up as the
   whole panel stuttering. */

import { $ } from './ui.js';

/* ── EFIS gauges: attitude + compass rose ────────────────────────────
   Drawn from readouts the agent may not send yet: the ATT gauge covers
   itself with NOT REPORTED rather than freezing level, which on an
   instrument would be a lie. */
var SVG_NS = 'http://www.w3.org/2000/svg';
var AI_PX_PER_DEG = 2.2;      // pitch ladder scale
var simHdgCum = 0, simHdgPrev = null;   // wrap-safe rose rotation

export function simBuildGauges() {
  var ladder = $('simg-ladder');
  ladder.setAttribute('class', 'ai-ladder');
  [-20, -10, 10, 20].forEach(function (deg) {
    var y = 100 - deg * AI_PX_PER_DEG;
    var w = Math.abs(deg) === 10 ? 16 : 26;
    var ln = document.createElementNS(SVG_NS, 'line');
    ln.setAttribute('x1', 100 - w); ln.setAttribute('x2', 100 + w);
    ln.setAttribute('y1', y); ln.setAttribute('y2', y);
    ladder.appendChild(ln);
    var tx = document.createElementNS(SVG_NS, 'text');
    tx.setAttribute('x', 100 + w + 9); tx.setAttribute('y', y + 3);
    tx.textContent = Math.abs(deg);
    ladder.appendChild(tx);
  });

  var rose = $('simg-rose');
  var CARD = { 0: 'N', 90: 'E', 180: 'S', 270: 'W' };
  for (var d = 0; d < 360; d += 10) {
    var major = d % 30 === 0;
    var tick = document.createElementNS(SVG_NS, 'line');
    tick.setAttribute('class', 'rose-tick' + (CARD[d] !== undefined ? ' card' : ''));
    tick.setAttribute('x1', 100); tick.setAttribute('x2', 100);
    tick.setAttribute('y1', 14); tick.setAttribute('y2', major ? 26 : 21);
    tick.setAttribute('transform', 'rotate(' + d + ' 100 100)');
    rose.appendChild(tick);
    if (major) {
      var lbl = document.createElementNS(SVG_NS, 'text');
      var card = CARD[d] !== undefined;
      lbl.setAttribute('class', 'rose-lbl' + (card ? '' : ' minor'));
      lbl.setAttribute('x', 100); lbl.setAttribute('y', card ? 42 : 39);
      lbl.setAttribute('transform', 'rotate(' + d + ' 100 100)');
      lbl.textContent = card ? CARD[d] : String(d / 10);
      rose.appendChild(lbl);
    }
  }
}

export function simPaintGauges(r) {
  var ai = $('simg-horizon').closest('.gauge');
  var att = r.pitch_deg !== undefined && r.bank_deg !== undefined;
  ai.classList.toggle('na', !att);
  if (att) {
    // Rotate about the wings, then slide along the rotated vertical — the
    // order that keeps the horizon parallel to itself through a banked climb.
    $('simg-horizon').style.transform =
      'rotate(' + (-r.bank_deg) + 'deg) translateY(' + (r.pitch_deg * AI_PX_PER_DEG) + 'px)';
  }
  if (typeof r.hdg_mag === 'number') {
    // Accumulate shortest-path deltas so 359→001 nudges 2° instead of
    // unwinding the card 358° through the transition.
    if (simHdgPrev !== null) {
      var dlt = ((r.hdg_mag - simHdgPrev + 540) % 360) - 180;
      simHdgCum += dlt;
    } else { simHdgCum = r.hdg_mag; }
    simHdgPrev = r.hdg_mag;
    $('simg-rose').style.transform = 'rotate(' + (-simHdgCum) + 'deg)';
    $('simg-hdg-txt').textContent = ('00' + r.hdg_mag).slice(-3);
  }
}

// The strip's SIM sub-label, from the ordinary state poll. Losing the agent
// is normal operation — it lives and dies with Windows — so this reads as an
// absence, not an alert.
