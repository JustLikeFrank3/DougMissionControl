/* simmath.js — what a stepper tap means, with no DOM and no network.

   Every control on the SIM surface sends an ABSOLUTE target computed from the
   last observed value, never a relative nudge. That is the whole reason this
   file exists as pure functions: a dropped command then leaves the control
   exactly where it was instead of silently swallowing one step of a sequence,
   and the panel knows what it asked for, so PENDING can confirm against the
   target rather than against "anything that isn't what it was". */

/**
 * The detent a flap tap is asking for.
 *
 * Clamps rather than wraps: the top of the flap range is a hard stop on the
 * aeroplane, and a + tap at full flap that came back round to CLEAN would be
 * the worst possible surprise on short final.
 */
export function nextDetent(index, delta, detents) {
  var top = Math.max(0, (detents || 1) - 1);
  return Math.max(0, Math.min(top, Math.round(index) + delta));
}

/**
 * The value an autopilot bug tap is asking for.
 *
 * Heading wraps — 359 + 1 is 000, and a bug that stopped dead at 359 would be
 * unusable on any northerly course. Everything else clamps to its limits.
 */
export function stepBug(value, delta, spec) {
  var v = value + delta;
  if (spec.wrap) return ((v % 360) + 360) % 360;
  return Math.max(spec.min, Math.min(spec.max, v));
}

/** What remains for the pilot to do for an observed ATC handoff. */
export function atcHandoff(nextMHz, com1) {
  if (typeof nextMHz !== 'number' || !com1 ||
      Math.abs(nextMHz - com1.act) < 0.005) return null;
  return {
    frequency: nextMHz,
    action: Math.abs(nextMHz - com1.sby) < 0.005 ? 'swap' : 'stage',
  };
}

/** Highest-priority observed aircraft-neutral cockpit hazard. */
export function cockpitWarning(state) {
  var warnings = state && state.warnings;
  var readouts = state && state.readouts;
  if (!warnings) return null;
  if (Array.isArray(warnings.engine_fire) && warnings.engine_fire.length) return {
    kind: 'engine_fire', label: 'ENGINE FIRE',
    detail: 'ENG ' + warnings.engine_fire.join(' / '),
  };
  if (warnings.stall) return { kind: 'stall', label: 'STALL' };
  if (warnings.gear_damage) return { kind: 'gear_damage', label: 'GEAR DAMAGE' };
  if (warnings.gear_warning) {
    var gearLabels = {
      gear_up: 'GEAR UP', amphibious_gear_up: 'AMPHIB GEAR UP',
      amphibious_gear_down: 'AMPHIB GEAR DOWN',
      on_ground_handle_up: 'GEAR HANDLE UP',
    };
    return { kind: 'gear_warning', label: gearLabels[warnings.gear_warning] || 'GEAR WARNING' };
  }
  if (warnings.gear_speed_exceeded) return { kind: 'gear_overspeed', label: 'GEAR OVERSPEED' };
  if (warnings.overspeed) return {
    kind: 'overspeed',
    label: 'OVERSPEED',
    detail: readouts && typeof readouts.ias_kt === 'number'
      ? readouts.ias_kt + ' KT' : null,
  };
  return null;
}
