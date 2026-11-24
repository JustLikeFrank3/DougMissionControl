/* fleet.js — what a machine reports, in the few words a screen has room for.

   Pure: no DOM and no clock of its own, so all of it runs in node. The
   readings come from /api/fleet (and, for Douglas, from /api/douglas); this
   only words them. The rule is the panel's rule: a reading that is not there
   is left out or shown as a dash, never as a zero, and a reading that has
   gone stale says how old it is. */

import { fmtAgo } from './format.js';

var STALE_S = 90;

/** How full a disk is, as a whole percentage, or null when it does not say. */
export function diskPct(d) {
  if (!d || !d.total_bytes || d.used_bytes == null) return null;
  return Math.round(100 * d.used_bytes / d.total_bytes);
}

/**
 * A machine's services in a phrase, and whether anything is wrong.
 * `units` is null when the machine could not be asked, which is not the
 * same as having none. A service that is switched off on purpose is not
 * down, and is not counted as one that should be running.
 */
export function servicesLine(units) {
  if (units == null) return { text: 'services not readable', bad: false, known: false };
  units = units.filter(function (u) { return u && u.unit; });
  if (!units.length) return { text: '', bad: false, known: true };
  var off = units.filter(function (u) {
    return u.state === 'inactive' && (u.enabled === 'disabled' || u.enabled === 'masked');
  });
  var down = units.filter(function (u) { return u.state !== 'active' && off.indexOf(u) < 0; });
  var up = units.filter(function (u) { return u.state === 'active'; });
  var again = units.filter(function (u) { return u.restarts > 0; });
  var text = up.length + ' of ' + (units.length - off.length) + ' services up';
  if (down.length) text += ' · DOWN: ' + down.map(function (u) { return u.unit; }).join(', ');
  if (again.length) {
    text += ' · restarted: ' + again.map(function (u) { return u.unit + ' ' + u.restarts + '×'; }).join(', ');
  }
  if (off.length) text += ' · off: ' + off.map(function (u) { return u.unit; }).join(', ');
  return { text: text, bad: down.length > 0, known: true };
}

/**
 * What one machine of /api/fleet reports, in a line: { text, bad }.
 * Empty text for a machine that reports nothing about itself.
 */
export function machineLine(m, now) {
  if (!m || !m.up) return { text: '', bad: false };
  var r = m.reports || {}, host = r.host || r, bits = [];
  function add(v, unit, lead) { if (v != null && isFinite(v)) bits.push((lead || '') + Math.round(v) + unit); }
  add(host.temp_c, ' °C');
  add(host.power_w, ' W');
  add(r.gpu_temp_c, ' °C', 'GPU ');
  add(r.gpu_util_pct, ' %', 'GPU ');
  if (host.load != null && isFinite(host.load)) bits.push('load ' + Number(host.load).toFixed(2));
  var disk = r.disk || (r.disks && r.disks[0]);
  add(diskPct(disk), ' %', 'disk ');
  var svc = 'services' in m ? servicesLine(m.services) : { text: '', bad: false };
  if (svc.text) bits.push(svc.text);
  if (m.as_of && now - m.as_of > STALE_S && bits.length) bits.push(fmtAgo(m.as_of, now) + ' old');
  return { text: bits.join(' · '), bad: svc.bad };
}
