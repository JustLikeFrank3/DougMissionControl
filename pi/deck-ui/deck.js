/* Flight Deck v0.1 — kiosk client.
   Talks only to deck-api on the same origin: one SSE stream in, small POSTs
   out. Deliberately dependency-free and small — on a Pi 4B the browser is
   the single most expensive process on the box, so this page stays cheap. */

(function () {
  'use strict';

  var HOLD_MS = 1500;
  var DECK_W = 2560, DECK_H = 720;

  var PHASES = [
    { n: 'TRIGGER',  s: 'deck-api' },
    { n: 'PROBE',    s: ':9105 / :9106' },
    { n: 'KICK',     s: 'WOL · ssh · agent' },
    { n: 'PING',     s: 'icmp' },
    { n: 'OS UP',    s: 'probe answers' },
    { n: 'LOGON',    s: 'needs hook' },
    { n: 'LAUNCHED', s: 'needs hook' }
  ];

  var $ = function (id) { return document.getElementById(id); };
  var state = null;
  var observableMax = 5;

  /* ── scale to fit ─────────────────────────────────────────────────────
     On the Edge the viewport is exactly 2560x720 and this is a no-op. */
  function fit() {
    var k = Math.min(window.innerWidth / DECK_W, window.innerHeight / DECK_H);
    var deck = $('deck');
    deck.style.transform = 'scale(' + k + ')';
    $('fit').style.height = window.innerHeight + 'px';
  }
  window.addEventListener('resize', fit);

  /* ── phase track ──────────────────────────────────────────────────────── */
  function buildTrack() {
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

  function renderTrack(boot, last) {
    var reached = boot.in_flight ? boot.phase
                : (last ? last.reached_phase : 0);
    var failed = !boot.in_flight && last && last.result !== 'ok';

    [].forEach.call($('track').children, function (el) {
      var p = Number(el.dataset.phase);
      el.className = 'node';
      if (p < reached) el.classList.add('done');
      else if (p === reached) el.classList.add(failed ? 'fail' : (boot.in_flight ? 'now' : 'done'));
      else el.classList.add('pending');
      // 6 and 7 are unreachable until the Windows greeting calls back.
      if (p > observableMax && p > reached) el.classList.add('pending');
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

  /* ── formatting ───────────────────────────────────────────────────────── */
  function fmtDur(s) {
    s = Math.max(0, Math.round(s || 0));
    var m = Math.floor(s / 60);
    return m ? m + ':' + String(s % 60).padStart(2, '0') : s + ' s';
  }

  function fmtAgo(ts) {
    if (!ts) return '—';
    var s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 90) return s + ' s';
    var m = Math.floor(s / 60);
    if (m < 90) return m + ' m';
    var h = Math.floor(m / 60);
    return h < 48 ? h + ' h ' + (m % 60) + ' m' : Math.floor(h / 24) + ' d';
  }

  function fmtClock(ts) {
    var d = new Date((ts || Date.now() / 1000) * 1000);
    return String(d.getHours()).padStart(2, '0') + ':' +
           String(d.getMinutes()).padStart(2, '0');
  }

  function rows(el, pairs) {
    el.innerHTML = '';
    pairs.forEach(function (p) {
      var r = document.createElement('div');
      r.className = 'row';
      r.innerHTML = '<span class="k"></span><span class="v"></span>';
      r.querySelector('.k').textContent = p[0];
      r.querySelector('.v').textContent = p[1];
      el.appendChild(r);
    });
  }

  /* ── render ───────────────────────────────────────────────────────────── */
  var OS_LOOK = {
    windows: { pill: 'ONLINE',  cls: 'ok',   os: 'WINDOWS' },
    linux:   { pill: 'ONLINE',  cls: 'ok',   os: 'LINUX' },
    booting: { pill: 'BOOTING', cls: 'busy', os: 'NO OS YET' },
    off:     { pill: 'OFFLINE', cls: 'off',  os: 'POWERED OFF' },
    unknown: { pill: 'UNKNOWN', cls: 'off',  os: '—' }
  };

  function render(s) {
    state = s;
    observableMax = (s.boot && s.boot.observable_max) || 5;

    var ws = s.workstation || {};
    var boot = s.boot || {};
    var look = OS_LOOK[ws.os] || OS_LOOK.unknown;

    $('clock').textContent = fmtClock(s.server_time);

    // While a boot is in flight the badge is the timer — that is the number
    // you actually want when the machine is unusable.
    if (boot.in_flight) {
      $('state-pill').textContent = 'BOOTING → ' + String(boot.target || '').toUpperCase();
      $('state-pill').className = 'pill busy';
      $('state-os').textContent = fmtDur(boot.elapsed);
      $('state-meta').textContent =
        'phase ' + boot.phase + ' / ' + observableMax + '  ·  ' + boot.phase_name;
    } else {
      $('state-pill').textContent = look.pill;
      $('state-pill').className = 'pill ' + look.cls;
      $('state-os').textContent = look.os;
      var bits = [];
      if (ws.since) bits.push((ws.os === 'off' ? 'since ' : 'up ') + fmtAgo(ws.since));
      bits.push(ws.ip || '');
      if (ws.agent) bits.push('agent :9107 ok');
      $('state-meta').textContent = bits.filter(Boolean).join('  ·  ');
    }

    renderTrack(boot, s.last_boot);

    // Controls: one action at a time.
    [].forEach.call(document.querySelectorAll('.tile'), function (t) {
      t.disabled = !!boot.in_flight;
    });
    $('abort').hidden = !boot.in_flight;
    $('foot').textContent = boot.in_flight
      ? 'a WOL packet already sent cannot be recalled'
      : 'hold to arm · voice triggers land here too';

    // Workstation — only what the existing probes can honestly say.
    $('ws-pill').textContent = look.pill;
    $('ws-pill').className = 'h-pill ' + look.cls;
    rows($('ws-rows'), [
      ['os', ws.os === 'booting' ? 'no exporter yet' : (ws.os || '—')],
      ['address', ws.ip || '—'],
      ['boot agent', ws.agent ? ':9107 answering' : 'no answer'],
      [ws.os === 'off' ? 'last seen' : 'changed', fmtAgo(ws.since)]
    ]);

    var pi = s.pi || {};
    rows($('pi-rows'), [
      ['soc temp', pi.temp_c != null ? pi.temp_c + ' °C' : '—'],
      ['load', pi.load != null ? pi.load.toFixed(2) : '—'],
      ['memory', pi.mem_pct != null ? pi.mem_pct + ' %' : '—'],
      ['uptime', pi.uptime != null ? fmtAgo(Date.now() / 1000 - pi.uptime) : '—']
    ]);

    var log = $('log');
    log.innerHTML = '';
    (s.events || []).slice(0, 9).forEach(function (e) {
      var d = document.createElement('div');
      d.className = e.level || 'info';
      d.innerHTML = '<span class="t"></span> <span class="src"></span> <span class="m"></span>';
      d.querySelector('.t').textContent = fmtClock(e.ts);
      d.querySelector('.src').textContent = e.source;
      d.querySelector('.m').textContent = e.text;
      log.appendChild(d);
    });
  }

  /* ── hold to arm ──────────────────────────────────────────────────────── */
  function wireHold(el, fire) {
    var raf = null, start = 0;

    function tick() {
      var pct = Math.min(1, (performance.now() - start) / HOLD_MS);
      el.querySelector('.hold').style.width = (pct * 100) + '%';
      if (pct >= 1) { stop(); fire(); return; }
      raf = requestAnimationFrame(tick);
    }

    function begin(e) {
      if (el.disabled) return;
      e.preventDefault();
      el.classList.add('arming');
      start = performance.now();
      raf = requestAnimationFrame(tick);
    }

    function stop() {
      if (raf) cancelAnimationFrame(raf);
      raf = null;
      el.classList.remove('arming');
      el.querySelector('.hold').style.width = '0';
    }

    el.addEventListener('pointerdown', begin);
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (t) {
      el.addEventListener(t, stop);
    });
  }

  function post(path, body) {
    return fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json(); }).catch(function () { return null; });
  }

  [].forEach.call(document.querySelectorAll('.tile'), function (tile) {
    wireHold(tile, function () {
      post('/api/boot', { intent: tile.dataset.intent });
    });
  });
  wireHold($('abort'), function () { post('/api/abort', {}); });

  /* ── live state ───────────────────────────────────────────────────────── */
  var es = null, retry = 1000;

  function connect() {
    if (es) es.close();
    es = new EventSource('/api/events');

    es.onopen = function () {
      retry = 1000;
      $('conn').textContent = 'live';
      $('conn').className = 'live';
    };

    es.onmessage = function (ev) {
      try { render(JSON.parse(ev.data)); } catch (err) { /* keep last good */ }
    };

    es.onerror = function () {
      $('conn').textContent = 'reconnecting…';
      $('conn').className = 'dead';
      es.close();
      setTimeout(connect, retry);
      retry = Math.min(retry * 2, 15000);   // deck-api restarting, not a crisis
    };
  }

  // The elapsed timer has to advance between pushes, so tick locally.
  setInterval(function () {
    if (!state) return;
    if (state.boot && state.boot.in_flight && state.boot.started) {
      state.boot.elapsed = Date.now() / 1000 - state.boot.started;
      render(state);
    } else {
      $('clock').textContent = fmtClock();
    }
  }, 1000);

  window.addEventListener('contextmenu', function (e) { e.preventDefault(); });

  buildTrack();
  fit();
  connect();
})();
