/* media.js — now-playing in the DECK rail, from whichever OS is up.

   Reads observed playback only. A transport tap is a command; what the widget
   shows is what the player reports afterwards, which is the same rule the SIM
   controls follow and for the same reason. */

import { $, post, getJSON } from './ui.js';
import { fmtTime } from './format.js';

/* ── now-playing widget (DECK rail) ────────────────────────────────────
   Observed playback only: the widget shows what the player reports, and a
   transport tap is a command whose effect arrives in the next poll. Art is
   fetched only when art_id changes — the JSON stays small. */
var npTimer = null, npArtId = null;

export function npPoll(on) {
  if (on && !npTimer) { npTick(); npTimer = setInterval(npTick, 2000); }
  else if (!on && npTimer) { clearInterval(npTimer); npTimer = null; }
}

export function npTick() {
  fetch('/api/media', { cache: 'no-store' })
    .then(function (r) { return r.json(); })
    // Fetch only — a paint exception must not be reported as a dead link.
    .catch(function () { paintNp({ active: false }); return null; })
    .then(function (d) { if (d) paintNp(d); });
}

export function paintNp(m) {
  var np = $('np');
  np.classList.toggle('idle', !m.active);
  if (!m.active) {
    $('np-title').textContent = 'no source';
    $('np-artist').textContent = '';
    $('np-time').textContent = '';
    $('np-art').hidden = true; $('np-noart').hidden = false;
    npArtId = null;
  } else {
    $('np-title').textContent = m.title || '(untitled)';
    $('np-artist').textContent = m.artist || '';
    $('np-time').textContent = (m.playing ? '▶ ' : '❚❚ ')
      + fmtTime(m.position_s) + (m.duration_s ? ' / ' + fmtTime(m.duration_s) : '');
    if (m.art_id && m.art_id !== npArtId) {
      npArtId = m.art_id;
      var img = $('np-art');
      img.onload = function () { img.hidden = false; $('np-noart').hidden = true; };
      img.onerror = function () { img.hidden = true; $('np-noart').hidden = false; };
      img.src = '/api/media/art?v=' + m.art_id;
    }
    var can = m.can || {};
    $('np-prev').disabled = !can.prev;
    $('np-play').disabled = !can.play_pause;
    $('np-next').disabled = !can.next;
  }
}

[{ id: 'np-prev', a: 'prev' }, { id: 'np-play', a: 'play_pause' },
 { id: 'np-next', a: 'next' }].forEach(function (b) {
  $(b.id).addEventListener('click', function () {
    post('/api/media/command', { action: b.a }).then(function () {
      setTimeout(npTick, 350);   // let the player react, then re-observe
    });
  });
});

/* SQUADRONS surface: tried and removed. Open-loop SendInput keystrokes
   never reached the game (games read raw scancodes), which made it exactly
   the blind macro deck the sim brief warns against. The DECK boot tile
   remains the launch path. */
