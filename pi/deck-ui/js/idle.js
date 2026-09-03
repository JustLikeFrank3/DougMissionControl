/* idle.js - the resting state: a clock, the boot tiles, and three dots.

   The panel spends most of its life with nothing happening. Its previous
   default was the densest surface it has - a full wallboard - which meant
   maximum pixels at minimum relevance, on an OLED-adjacent panel, all night.
   This is the opposite bet: when nothing is happening, show almost nothing,
   and make the one glanceable fact (is anything on?) readable from across
   the room.

   Everything here rides the SSE frame the page already holds. No polling of
   its own, no timers beyond the clock's once-a-second tick. */

import { $, wireHold, post } from './ui.js';
import { fmtClock } from './format.js';

var idleTimer = null;

export function wireIdle(fireBoot) {
  // Same hold-to-arm as the DECK tiles, driving the same endpoint through the
  // same callback deck.js already uses - one implementation of "start a boot".
  [].forEach.call(document.querySelectorAll('.idletile'), function (t) {
    wireHold(t, function () { fireBoot(t.dataset.intent); });
  });
}

export function idlePoll(on) {
  if (on && !idleTimer) { idleTickClock(); idleTimer = setInterval(idleTickClock, 1000); }
  else if (!on && idleTimer) { clearInterval(idleTimer); idleTimer = null; }
}

function idleTickClock() {
  $('idle-clock').textContent = fmtClock();   // no arg = now
  $('idle-date').textContent = new Date().toLocaleDateString(undefined,
    { weekday: 'long', day: 'numeric', month: 'long' });
}

/** Painted from the frame by deck.js's render loop, same as every surface. */
export function paintIdle(s) {
  var ws = (s && s.workstation) || {};
  var mini = (s && s.mini) || {};
  var mobile = (s && s.mobile) || {};
  var boot = (s && s.boot) || {};

  // The tiles lock during a boot exactly as DECK's do - one action at a time
  // is a property of the system, not of whichever surface asked.
  [].forEach.call(document.querySelectorAll('.idletile'), function (t) {
    t.disabled = !!boot.in_flight;
  });

  // Four cards, two by two. Green only when up, muted when off - "off" is
  // this screen's normal, not a warning.
  var rows = [
    ['workstation', ws.os === 'off' ? 'off' : (ws.os || 'unknown'),
     ws.os !== 'off' && !!ws.os],
    ['doug prime', 'up', true],                       // the Pi is drawing this
    ['minidoug', mini.up ? 'up' : 'off', !!mini.up],
    // A laptop that is out of the house reads the same as one that is
    // asleep on the desk: off, and that is all this panel can honestly say.
    ['dougmobile', mobile.up ? 'up' : 'off', !!mobile.up],
  ];
  var el = $('idle-fleet');
  el.innerHTML = '';
  rows.forEach(function (r) {
    var d = document.createElement('div');
    d.className = 'idle-host' + (r[2] ? ' up' : '');
    d.innerHTML = '<i></i><b></b><span></span>';
    d.querySelector('b').textContent = r[0];
    d.querySelector('span').textContent = r[1];
    el.appendChild(d);
  });
}
