/* viz.js - the output spectrum, drawn from what the speakers are doing.

   The bands are measured on the workstation by the sim agent's WASAPI loopback
   capture, because the panel runs on the Pi and has no audio of its own. What
   is drawn here is therefore observed sound and nothing else - the same rule
   SIM follows for the gear and SCREENS for monitor inputs. There is no
   fallback pattern, no idle animation, and nothing that moves when the music
   is not playing. A visualiser that is not listening is just a screensaver
   wearing a meter's clothes.

   DOM bars rather than a canvas: on a Pi 4B, Chromium is the most expensive
   process on the box, and forty-eight transforms the compositor can handle on
   its own cost far less than a full-width canvas repaint at 20 Hz. */

import { $, getJSON } from './ui.js';

// Faster than the media poll (2 s) and slower than SIM (250 ms). The agent
// republishes at 20 Hz; asking much faster only costs requests, and asking
// slower makes the bass look like it is stepping rather than moving.
var VIZ_MS = 100;
var vizTimer = null;
var vizBars = {};      // element id -> how many bars it currently holds
var vizLast = null;

export function vizPoll(on) {
  if (on && !vizTimer) { vizTick(); vizTimer = setInterval(vizTick, VIZ_MS); }
  else if (!on && vizTimer) { clearInterval(vizTimer); vizTimer = null; }
}

export function vizTick() {
  // getJSON never throws and resolves null on failure, which paints as "no
  // capture" - the same as the agent being gone, which it is.
  getJSON('/api/audio').then(paintViz);
}

/** The last frame, so the AUDIO surface can paint the moment it opens rather
    than waiting out a poll interval with an empty box. */
export function vizLatest() { return vizLast; }

/**
 * Draw one spectrum into one container. Both the DECK rail widget and the
 * AUDIO surface come through here: the bars are the same bars, and two
 * implementations of "how loud is 400 Hz" would eventually disagree.
 */
export function drawBands(el, bands) {
  if (!el) return;
  var id = el.id;
  if (vizBars[id] !== bands.length) {
    vizBars[id] = bands.length;
    el.innerHTML = '';
    for (var i = 0; i < bands.length; i++) el.appendChild(document.createElement('i'));
  }
  for (var b = 0; b < bands.length; b++) {
    var v = bands[b];
    // A floor of a couple of percent so silence reads as a quiet line rather
    // than an empty box that looks like the widget has broken.
    el.children[b].style.setProperty('--h', (Math.max(0.02, Math.min(1, v)) * 100) + '%');
    el.children[b].classList.toggle('hot', v > 0.85);
  }
}

export function paintViz(d) {
  d = d || {};
  vizLast = d;
  var bands = d.bands || [];
  var live = !!d.active && bands.length > 0;

  // The DECK rail widget: hidden outright when there is nothing to draw, so
  // the rail gets its 46 px back rather than holding an empty box.
  var rail = $('viz');
  if (rail) {
    rail.hidden = !live;
    if (live) drawBands(rail, bands);
  }

  // The AUDIO surface, which says WHY when there is nothing rather than just
  // going blank — this is the surface someone opened on purpose.
  var live_ = $('aud-live'), ph = $('aud-ph');
  if (!live_ || !ph) return;
  ph.hidden = live;
  live_.hidden = !live;
  if (live) drawBands($('aud-viz'), bands);
  else {
    $('aud-pill').textContent = d.reason ? 'NO CAPTURE' : 'SILENT';
    var s = $('aud-foot'); if (s) s.textContent = '';
  }
}
