/* track.js — the boot phase track: seven steps from a trigger arriving to the
   game launching, driven by the orchestrator's own journald lines.

   Phases 6-7 depend on a callback from the Windows greeting that v0.1 does not
   wire, so a run legitimately completes at OS UP. The track says "not in this
   run" rather than leaving them looking pending forever. */

import { $ } from './ui.js';
import { fmtDur } from './format.js';

/* How far this run can honestly get. deck-api sends it per boot, because the
   ceiling is a property of the deployment — phases past OS UP need a callback
   the Windows greeting does not yet make — not of the track. Owned here, since
   the track is the only thing that renders it. */
let observableMax = 5;
export const setObservableMax = (n) => { observableMax = n || 5; };
export const getObservableMax = () => observableMax;

export const PHASES = [
  { n: 'TRIGGER',  s: 'deck-api' },
  { n: 'PROBE',    s: ':9105 / :9106' },
  { n: 'KICK',     s: 'WOL · ssh · agent' },
  { n: 'PING',     s: 'icmp' },
  { n: 'OS UP',    s: 'probe answers' },
  { n: 'LOGON',    s: 'greeting calls back' },
  { n: 'LAUNCHED', s: 'app started' }
];

/* ── phase track ──────────────────────────────────────────────────────── */
export function buildTrack() {
  var track = $('track');
  track.innerHTML = '';
  PHASES.forEach(function (p, i) {
    var el = document.createElement('div');
    el.className = 'node pending';
    el.dataset.phase = String(i + 1);
    el.innerHTML = '<div class="dot"></div><div class="n"></div><div class="s"></div>';
    el.querySelector('.n').textContent = p.n;
    el.querySelector('.s').textContent = p.s;
    track.appendChild(el);
  });
}

export function renderTrack(boot, last) {
  var reached = boot.in_flight ? boot.phase
              : (last ? last.reached_phase : 0);
  var failed = !boot.in_flight && last && last.result !== 'ok';

  [].forEach.call($('track').children, function (el) {
    var p = Number(el.dataset.phase);
    el.className = 'node';
    if (p < reached) el.classList.add('done');
    else if (p === reached) el.classList.add(failed ? 'fail' : (boot.in_flight ? 'now' : 'done'));
    else el.classList.add('pending');
    // Phases beyond this run's reach — a linux boot has no greeting to
    // call back, a plain windows boot launches nothing. Dashed and named,
    // so the rail states its scope instead of looking short.
    if (p > observableMax && p > reached) {
      el.classList.remove('pending');
      el.classList.add('unwired');
      el.querySelector('.s').textContent = 'not in this run';
    } else {
      el.querySelector('.s').textContent = PHASES[p - 1].s;
    }
  });

  var lbl = $('track-lbl');
  if (boot.in_flight) {
    lbl.textContent = 'BOOTING → ' + String(boot.target || '').toUpperCase() +
      '  ·  ' + (boot.intent || '') + '  ·  ' + fmtDur(boot.elapsed);
  } else if (last) {
    lbl.textContent = 'LAST BOOT  ·  ' + String(last.target || '').toUpperCase() +
      '  ·  ' + (last.intent || '') + '  ·  ' + fmtDur(last.seconds) +
      '  ·  ' + String(last.result || '').toUpperCase();
  } else {
    lbl.textContent = 'NO BOOT IN FLIGHT';
  }
}
