/* workstation.ts — the workstation card's words, from the live frame. */

import { fmtAgo, fmtDur } from './format';

export type Tone = '' | 'ok' | 'busy' | 'bad';
const OS_LOOK: Record<string, { pill: string; cls: Tone; os: string }> = {
  windows: { pill: 'ONLINE', cls: 'ok', os: 'WINDOWS' },
  linux: { pill: 'ONLINE', cls: 'ok', os: 'LINUX' },
  booting: { pill: 'BOOTING', cls: 'busy', os: 'NO OS YET' },
  off: { pill: 'OFFLINE', cls: '', os: 'POWERED OFF' },
  unknown: { pill: 'UNKNOWN', cls: '', os: '—' },
};

export const INTENTS: [string, string, string][] = [
  ['sim', 'FLIGHT SIM', 'MSFS 2024'],
  ['squadrons', 'SQUADRONS', 'STAR WARS'],
  ['plain', 'WINDOWS', 'greeting only'],
  ['code', 'LINUX', 'VS Code'],
];

export function wsView(s: any, now?: number) {
  const ws = (s && s.workstation) || {}, boot = (s && s.boot) || {};
  const look = OS_LOOK[ws.os] || OS_LOOK.unknown;
  const inFlight = !!boot.in_flight;
  const canOff = !inFlight && (ws.os === 'windows' || ws.os === 'linux');
  return {
    pill: inFlight ? 'BOOTING → ' + String(boot.target || '').toUpperCase() : look.pill,
    cls: (inFlight ? 'busy' : look.cls) as Tone,
    big: inFlight ? fmtDur(boot.elapsed) : look.os,
    phase: inFlight
      ? 'phase ' + boot.phase + ' / ' + (boot.observable_max || '—') + ' · ' + (boot.phase_name || '') +
        (boot.intent ? ' · ' + boot.intent : '')
      : '',
    meta: inFlight || !ws.since ? '' : (ws.os === 'off' ? 'since ' : 'up ') + fmtAgo(ws.since, now),
    inFlight,
    canOff,
    shutdown: canOff ? 'HOLD TO SHUT DOWN ' + String(ws.os).toUpperCase() : '',
  };
}
