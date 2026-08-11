/* warnings.js — persistent observed cockpit warning cue. */

import { $ } from './ui.js';
import { cockpitWarning } from './simmath.js';

var warningTimer = null;

export function paintCockpitWarning(state) {
  var warning = cockpitWarning(state);
  var cue = $('cockpit-cue');
  cue.hidden = !warning;
  if (!warning) return null;
  cue.textContent = 'COCKPIT · ' + warning.label +
    (warning.detail ? ' · ' + warning.detail : '');
  return warning;
}

function warningTick() {
  fetch('/api/sim', { cache: 'no-store' })
    .then(function (response) { return response.json(); })
    .then(function (data) {
      paintCockpitWarning(data && data.session ? data.state : null);
    })
    .catch(function () { paintCockpitWarning(null); });
}

export function warningPoll(on) {
  if (on && !warningTimer) {
    warningTick();
    warningTimer = setInterval(warningTick, 500);
  } else if (!on && warningTimer) {
    clearInterval(warningTimer);
    warningTimer = null;
    paintCockpitWarning(null);
  }
}