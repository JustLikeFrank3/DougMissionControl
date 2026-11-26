/* fleet.ts — what a machine reports, in the few words a screen has room for.
   A port of pi/deck-ui/js/fleet.js and of the fleet rows in m.js. The rule is
   the panel's: a reading that is not there is left out, never shown as a
   zero, and one that has gone stale says how old it is. */

import { fmtAgo } from './format';

const STALE_S = 90;

type Disk = { total_bytes?: number; used_bytes?: number | null } | null | undefined;
export type Unit = { unit?: string; state?: string; enabled?: string; restarts?: number };
export type Line = { text: string; bad: boolean };

/** How full a disk is, as a whole percentage, or null when it does not say. */
export function diskPct(d: Disk): number | null {
  if (!d || !d.total_bytes || d.used_bytes == null) return null;
  return Math.round((100 * d.used_bytes) / d.total_bytes);
}

/** A machine's services in a phrase, and whether anything is wrong. `units`
    is null when the machine could not be asked, which is not the same as
    having none. A service switched off on purpose is not down. */
export function servicesLine(units: Unit[] | null | undefined): Line & { known: boolean } {
  if (units == null) return { text: 'services not readable', bad: false, known: false };
  const all = units.filter((u) => u && u.unit);
  if (!all.length) return { text: '', bad: false, known: true };
  const off = all.filter((u) => u.state === 'inactive' && (u.enabled === 'disabled' || u.enabled === 'masked'));
  const down = all.filter((u) => u.state !== 'active' && off.indexOf(u) < 0);
  const up = all.filter((u) => u.state === 'active');
  const again = all.filter((u) => (u.restarts || 0) > 0);
  let text = up.length + ' of ' + (all.length - off.length) + ' services up';
  if (down.length) text += ' · DOWN: ' + down.map((u) => u.unit).join(', ');
  if (again.length) text += ' · restarted: ' + again.map((u) => u.unit + ' ' + u.restarts + '×').join(', ');
  if (off.length) text += ' · off: ' + off.map((u) => u.unit).join(', ');
  return { text, bad: down.length > 0, known: true };
}

/** What one machine of /api/fleet reports, in a line. Empty text for a
    machine that reports nothing about itself. */
export function machineLine(m: any, now: number): Line {
  if (!m || !m.up) return { text: '', bad: false };
  const r = m.reports || {};
  const host = r.host || r;
  const bits: string[] = [];
  const add = (v: unknown, unit: string, lead?: string) => {
    if (typeof v === 'number' && isFinite(v)) bits.push((lead || '') + Math.round(v) + unit);
  };
  add(host.temp_c, ' °C');
  add(host.power_w, ' W');
  add(r.gpu_temp_c, ' °C', 'GPU ');
  add(r.gpu_util_pct, ' %', 'GPU ');
  if (host.load != null && isFinite(host.load)) bits.push('load ' + Number(host.load).toFixed(2));
  const disk = r.disk || (r.disks && r.disks[0]);
  add(diskPct(disk), ' %', 'disk ');
  const svc = 'services' in m ? servicesLine(m.services) : { text: '', bad: false };
  if (svc.text) bits.push(svc.text);
  if (m.as_of && now - m.as_of > STALE_S && bits.length) bits.push(fmtAgo(m.as_of, now) + ' old');
  return { text: bits.join(' · '), bad: svc.bad };
}

export type FleetRow = { name: string; says: string; up: boolean; bad: boolean; more: Line };

/** The fleet card's rows, from the live frame and (where it has arrived)
    what each machine reports about itself. */
export function fleetRows(s: any, fleet: any, now: number): FleetRow[] {
  const ws = s.workstation || {}, mini = s.mini || {}, mob = s.mobile || {};
  const dgl = s.douglas || {}, voice = s.voice || {};
  const dm = dgl.model || {};
  const wsUp = ws.os === 'windows' || ws.os === 'linux';
  const rows: [string, string, boolean, boolean, string][] = [
    ['DOUG DUALBOOT', wsUp ? ws.os : ws.os === 'booting' ? 'booting' : 'off', wsUp, false, 'workstation'],
    ['DOUG PRIME', 'up · drawing the panel', true, false, 'node1'],
    ['MINIDOUG4', mini.up ? 'up' : 'off · seen ' + fmtAgo(mini.last_alive, now), !!mini.up, false, 'mini'],
    ['DOUGMOBILE', mob.up ? 'up' : 'away', !!mob.up, false, 'mobile'],
    ['DOUGLAS', !dgl.up ? 'offline · seen ' + fmtAgo(dgl.last_alive, now)
      : dm.state === 'running' ? (dm.name || 'serving')
      : dm.state === 'loading' ? 'loading model' : 'model server down', !!dgl.up, !dgl.up, 'douglas'],
    ['DOUGVOICE', voice.up ? (voice.satellite ? 'listening' : 'up · satellite down') : 'off', !!voice.up, false,
     'dougvoice'],
  ];
  const machines: any[] = fleet && Array.isArray(fleet.machines) ? fleet.machines : [];
  return rows.map((r) => {
    const m = machines.filter((x) => x && x.id === r[4])[0];
    return { name: r[0], says: r[1], up: r[2], bad: r[3], more: r[2] ? machineLine(m, now) : { text: '', bad: false } };
  });
}
