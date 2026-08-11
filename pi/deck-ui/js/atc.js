/* atc.js — the persistent ATC handoff cue shared by NAV and SIM. */

import { $ } from './ui.js';
import { atcHandoff } from './simmath.js';

export function paintAtcCue(state) {
  var controls = state && state.controls;
  var readouts = state && state.readouts;
  var handoff = atcHandoff(readouts && readouts.atc_next_mhz,
                           controls && controls.com1);
  var cue = $('atc-cue');
  cue.hidden = !handoff;
  if (!handoff) return null;
  cue.textContent = handoff.action === 'swap'
    ? 'ATC SWAP → ' + handoff.frequency.toFixed(3)
    : 'ATC → ' + handoff.frequency.toFixed(3);
  return handoff;
}