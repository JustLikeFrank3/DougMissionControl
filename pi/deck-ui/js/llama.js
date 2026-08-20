/* llama.js — the LOCAL MODEL control under the boot tiles.

   Observed state only: the bar shows what /api/llama reports the model is
   doing right now (the boot agent's answer, or a probe), never what a tap
   asked for. A hold is a command; its effect arrives in the next poll, the
   same rule the transport and SIM controls follow. Hidden entirely while
   nothing can act on it — an absent control, not a disabled lie. */

import { $, wireHold, post } from './ui.js';

var timer = null, last = null;

var SUB = {
  running: 'RUNNING · HOLD TO STOP',
  loading: 'LOADING · HOLD TO STOP',
  stopped: 'OFF · HOLD TO START',
};

export function llamaPoll(on) {
  if (on && !timer) { tick(); timer = setInterval(tick, 4000); }
  else if (!on && timer) { clearInterval(timer); timer = null; }
}

function tick() {
  fetch('/api/llama', { cache: 'no-store' })
    .then(function (r) { return r.json(); })
    .catch(function () { return null; })
    .then(paint);
}

function paint(d) {
  last = d;
  var bar = $('llama');
  if (!bar) return;
  // toggle=false covers Linux (no agent route yet), unknown, and off — in
  // every one of those a hold could only fail, so there is no bar at all.
  var usable = !!(d && d.toggle && SUB[d.state]);
  bar.hidden = !usable;
  if (!usable) return;
  bar.dataset.state = d.state;
  // The hold sweep colours by what the hold would DO: amber ends the model
  // like SHUT DOWN ends a session, magenta starts one like the boot tiles.
  bar.dataset.act = d.state === 'stopped' ? 'start' : 'stop';
  $('llama-sub').textContent = SUB[d.state];
}

export function wireLlama(footNotice) {
  wireHold($('llama'), function () {
    if (!last || !last.toggle) return;
    var action = last.state === 'stopped' ? 'start' : 'stop';
    post('/api/llama', { action: action }).then(function (r) {
      if (r && !r.ok && r.message) footNotice(r.message);
      else if (!r) footNotice('deck-api did not answer');
    });
  });
}
