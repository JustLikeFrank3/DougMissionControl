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
var npTimer = null, npArtId = null, navNpArtId = null;

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
  paintNavNp(m);
  paintAud(m);
  var nav = $('nav-audio');
  if (nav) nav.textContent = m.active ? (m.title || 'playing') : 'no source';
}

function paintNavNp(m) {
  var np = $('navnp');
  if (!np) return;
  np.classList.toggle('idle', !m.active);
  $('navnp-title').textContent = m.active ? (m.title || '(untitled)') : 'no source';
  $('navnp-artist').textContent = m.active ? (m.artist || '') : '';
  $('navnp-time').textContent = !m.active ? ''
    : (m.playing ? '▶ ' : '❚❚ ') + fmtTime(m.position_s)
      + (m.duration_s ? ' / ' + fmtTime(m.duration_s) : '');

  if (m.active && m.art_id && m.art_id !== navNpArtId) {
    navNpArtId = m.art_id;
    var img = $('navnp-art');
    img.onload = function () { img.hidden = false; $('navnp-noart').hidden = true; };
    img.onerror = function () { img.hidden = true; $('navnp-noart').hidden = false; };
    img.src = '/api/media/art?v=' + m.art_id;
  } else if (!m.active) {
    navNpArtId = null;
    $('navnp-art').hidden = true; $('navnp-noart').hidden = false;
  }

  var can = m.can || {};
  $('navnp-prev').disabled = !can.prev;
  $('navnp-play').disabled = !can.play_pause;
  $('navnp-next').disabled = !can.next;
}

/* The AUDIO surface's half of the same frame. Same observed playback, drawn
   with the room a 2560 px panel actually has: art you can see from across the
   room, and a progress bar rather than a line of small text. */
var audArtId = null;

function paintAud(m) {
  if (!$('aud-title')) return;
  $('aud-title').textContent = m.active ? (m.title || '(untitled)') : 'no source';
  $('aud-artist').textContent = m.artist || '';
  $('aud-album').textContent = m.album || '';
  $('aud-time').textContent = !m.active ? ''
    : (m.playing ? '▶ ' : '❚❚ ') + fmtTime(m.position_s)
      + (m.duration_s ? ' / ' + fmtTime(m.duration_s) : '');

  // Elapsed only where a duration was reported. A bar that guesses its own
  // length is worse than no bar: it moves confidently and means nothing.
  var pct = (m.active && m.duration_s)
    ? Math.max(0, Math.min(100, 100 * (m.position_s || 0) / m.duration_s)) : 0;
  $('aud-bar').style.width = pct + '%';
  $('aud-barwrap').hidden = !(m.active && m.duration_s);

  if (m.active && m.art_id && m.art_id !== audArtId) {
    audArtId = m.art_id;
    var img = $('aud-art');
    img.onload = function () { img.hidden = false; $('aud-noart').hidden = true; };
    img.onerror = function () { img.hidden = true; $('aud-noart').hidden = false; };
    img.src = '/api/media/art?v=' + m.art_id;
  } else if (!m.active) {
    audArtId = null;
    $('aud-art').hidden = true; $('aud-noart').hidden = false;
  }

  var can = m.can || {};
  $('aud-prev').disabled = !can.prev;
  $('aud-play').disabled = !can.play_pause;
  $('aud-next').disabled = !can.next;
  $('aud-foot').textContent = m.app
    ? 'source ' + m.app + ' · ' + (m.source || '') : '';
}

// Both transports drive the same command, and a tap on either re-observes.
[{ id: 'np-prev', a: 'prev' }, { id: 'np-play', a: 'play_pause' },
 { id: 'np-next', a: 'next' },
 { id: 'navnp-prev', a: 'prev' }, { id: 'navnp-play', a: 'play_pause' },
 { id: 'navnp-next', a: 'next' },
 { id: 'aud-prev', a: 'prev' }, { id: 'aud-play', a: 'play_pause' },
 { id: 'aud-next', a: 'next' }].forEach(function (b) {
  var el = $(b.id);
  if (!el) return;
  el.addEventListener('click', function () {
    post('/api/media/command', { action: b.a }).then(function () {
      setTimeout(npTick, 350);   // let the player react, then re-observe
    });
  });
});

/* SQUADRONS surface: tried and removed. Open-loop SendInput keystrokes
   never reached the game (games read raw scancodes), which made it exactly
   the blind macro deck the sim brief warns against. The DECK boot tile
   remains the launch path. */
