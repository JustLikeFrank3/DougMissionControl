/* speedprofile.js — aircraft speed schedules, pure.

   A profile is Doug knowing what aeroplane it is looking at: the speed the
   schedule calls for at this altitude in this phase, and nothing else. The
   targets are advisory — same posture as the descent card, which can suggest
   a descent but never command one.

   KIAS below the transition altitude, Mach above it, because indicated
   airspeed and the speed of sound drift apart as the air thins: the same
   Mach reads fewer and fewer knots as an aircraft climbs, so past the
   crossover the schedule is flown as Mach or the target itself would decay. */

const PROFILES = [
  {
    name: '747',
    match: /747/i,
    transitionFt: 28000,
    cruiseMach: 0.85,
    // [ceiling_ft, target_kt] bands, lowest first. Below 10k is the
    // regulatory 250, not an aircraft preference.
    climbKt: [[10000, 250], [28000, 290]],
    descentKt: [[10000, 250], [28000, 280]],
  },
  {
    name: 'CJ4',
    match: /cj4|citationjet|525/i,
    transitionFt: 27000,
    cruiseMach: 0.74,
    climbKt: [[10000, 250], [27000, 270]],
    descentKt: [[10000, 250], [27000, 270]],
  },
];

/** The profile for an observed aircraft title, or null — an unknown
    airframe gets no advice rather than a guess from the wrong handbook. */
export function speedProfile(title) {
  if (!title) return null;
  for (const p of PROFILES) if (p.match.test(title)) return p;
  return null;
}

/**
 * The schedule's target at this phase and altitude.
 * Returns { mode: 'KIAS', kt } or { mode: 'MACH', mach }, or null when the
 * schedule has nothing to say (on the ground, unknown phase, no altitude).
 */
export function speedTarget(profile, phase, altFt) {
  if (!profile || typeof altFt !== 'number') return null;
  const bands = phase === 'TAKEOFF' || phase === 'CLIMB' || phase === 'CRUISE'
    ? profile.climbKt
    : phase === 'DESCENT' || phase === 'APPROACH' ? profile.descentKt
    : null;
  if (!bands) return null;
  if (altFt >= profile.transitionFt) return { mode: 'MACH', mach: profile.cruiseMach };
  for (const [ceiling, kt] of bands) if (altFt < ceiling) return { mode: 'KIAS', kt };
  return { mode: 'MACH', mach: profile.cruiseMach };
}

/**
 * Observed speed against the target: { off, unit } where off is signed
 * (negative = slow) and unit is 'KT' or 'MACH'. Null when the reading the
 * mode needs is absent — an old agent sends no mach, and a deviation
 * invented from IAS at FL350 would be exactly the lie this panel avoids.
 */
export function speedDeviation(target, iasKt, mach) {
  if (!target) return null;
  if (target.mode === 'KIAS') {
    if (typeof iasKt !== 'number') return null;
    return { off: iasKt - target.kt, unit: 'KT' };
  }
  if (typeof mach !== 'number') return null;
  return { off: Math.round((mach - target.mach) * 1000) / 1000, unit: 'MACH' };
}
