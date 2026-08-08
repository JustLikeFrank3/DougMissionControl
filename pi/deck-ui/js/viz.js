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
var vizTimer = null, vizBars = 0;

export function vizPoll(on) {
  if (on && !vizTimer) { vizTick(); vizTimer = setInterval(vizTick, VIZ_MS); }
  else if (!on && vizTimer) { clearInterval(vizTimer); vizTimer = null; }
}

export function vizTick() {
  // getJSON never throws and resolves null on failure, which paints as "no
  // capture" - the same as the agent being gone, which it is.
  getJSON('/api/audio').then(paintViz);
}

export function paintViz(d) {
  var el = $('viz');
  if (!el) return;
  d = d || {};
  var bands = d.bands || [];
  var live = !!d.active && bands.length > 0;

  el.hidden = !live;
  if (!live) return;

  // Built once, and rebuilt only if the agent changes its band count.
  if (vizBars !== bands.length) {
    vizBars = bands.length;
    el.innerHTML = '';
    for (var i = 0; i < bands.length; i++) {
      el.appendChild(document.createElement('i'));
    }
  }

  for (var b = 0; b < bands.length; b++) {
    var v = bands[b];
    // A floor of a couple of percent so silence reads as a quiet line rather
    // than an empty box that looks like the widget has broken.
    var h = Math.max(0.02, Math.min(1, v));
    el.children[b].style.setProperty('--h', (h * 100) + '%');
    el.children[b].classList.toggle('hot', v > 0.85);
  }
}
