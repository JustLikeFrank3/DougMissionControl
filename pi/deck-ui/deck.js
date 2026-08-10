/* Flight Deck v0.1 — kiosk client.
   Talks only to deck-api on the same origin: one SSE stream in, small POSTs
   out. Deliberately dependency-free and small — on a Pi 4B the browser is
   the single most expensive process on the box, so this page stays cheap. */

import { fmtDur, fmtAgo, fmtClock, fmtVal } from './js/format.js';
import { createSeries } from './js/series.js';
import { $, rows, wireHold, post } from './js/ui.js';
import { buildTrack, renderTrack,
         setObservableMax, getObservableMax } from './js/track.js';
import { drawAllSparks } from './js/spark.js';
import { dspPoll } from './js/screens.js';
import { npPoll } from './js/media.js';
import { vizPoll } from './js/viz.js';
import { wireIdle, idlePoll, paintIdle } from './js/idle.js';
import { navPoll, setNavPlan, setNavPlanDisabled } from './js/nav.js';
import { wireNav } from './js/nav.js';
import { simPoll, simSend, wireSim } from './js/sim.js';

(function () {
  'use strict';

  var DECK_W = 2560, DECK_H = 720;


  var state = null;

  // A transient line for the DECK foot. render() repaints the foot on every
  // frame, so a one-off textContent write would survive less than a second -
  // the notice has to outlive the next frame, not fight it.
  var footMsg = '', footUntil = 0;
  function footNotice(msg) {
    footMsg = msg;
    footUntil = Date.now() + 6000;
    var f = $('foot');
    if (f) f.textContent = msg;
  }

  /* ── scale to fit ─────────────────────────────────────────────────────
     On the Edge the viewport is exactly 2560x720 and this is a no-op. */
  function fit() {
    var k = Math.min(window.innerWidth / DECK_W, window.innerHeight / DECK_H);
    var deck = $('deck');
    deck.style.transform = 'scale(' + k + ')';
    $('fit').style.height = window.innerHeight + 'px';
  }
  window.addEventListener('resize', fit);
  // A kiosk never resizes, so `resize` alone means fit() effectively runs once,
  // against whatever the viewport measured at that instant. If it settles even
  // a few pixels shorter afterwards — a late scrollbar, a compositor handing
  // over the real mode — nothing recomputes the scale and the bottom of the
  // 720 px layout is simply cut off. That is LIVE and "hold to arm" going
  // missing: they are the last 18 px of their columns, so they are what a few
  // pixels of overhang eats first.
  //
  // ResizeObserver catches the viewport actually changing; the load event
  // catches fonts and the stylesheet settling after first paint.
  window.addEventListener('load', fit);
  if (window.ResizeObserver) new ResizeObserver(fit).observe(document.documentElement);

  /* ── rolling window ───────────────────────────────────────────────────────
     Lives here and nowhere else. deck-api sends the latest reading and keeps
     no history; this buffer is the only history that exists, and it dies with
     the page. Nothing is written to the Pi's thumb drive. */

  var WINDOW_S = 3600;          // 60 min
  var MAX_PTS = 1200;           // hard cap regardless of sample rate
  var series = createSeries();

  // Which traces the page actually shows, read off the DOM once.
  var TRACKED = [].map.call(document.querySelectorAll('[data-key]'), function (el) {
    return el.dataset.key;
  }).filter(function (v, i, a) { return a.indexOf(v) === i; });

  /* ── surfaces ─────────────────────────────────────────────────────────── */

  var evalsSrc = null;        // only reassign when it CHANGES — see below
  var lastGrafanaOk = null;

  function renderSurface(s) {
    var active = (s.surface && s.surface.active) || 'evals';
    // HOME follows the configured default. idle has no strip button, so HOME
    // is the only touch route back to it.
    var home = $('home-btn');
    if (home && s.surface && s.surface.default) home.dataset.surface = s.surface.default;
    [].forEach.call(document.querySelectorAll('.surface'), function (el) {
      el.classList.toggle('on', el.dataset.name === active);
    });
    [].forEach.call(document.querySelectorAll('.navb'), function (b) {
      b.classList.toggle('on', b.dataset.surface === active);
    });
    simPoll(active === 'sim');
    // Now-playing feeds the DECK rail widget AND the AUDIO surface, so it
    // runs for either. The spectrum is the expensive one at 10 Hz, and it
    // stops the moment neither surface is showing it.
    npPoll(active === 'deck' || active === 'audio');
    vizPoll(active === 'deck' || active === 'audio');
    navPoll(active === 'nav');
    dspPoll(active === 'displays');
    idlePoll(active === 'idle');
  }

  function renderSimNav(s) {
    var sim = s.sim || {};
    var el = $('nav-sim');
    if (!el) return;
    var ac = sim.aircraft || '';
    if (ac.length > 17) ac = ac.slice(0, 16) + '…';
    el.textContent = !sim.link ? 'no link' : !sim.session ? 'no sim' : (ac || 'connected');
  }

  var navSig = null;

  function renderEvalsNav(e) {
    // Rebuild only when the board list actually changes — Grafana's list is
    // polled, and replacing these nodes every frame would fight your finger.
    var boards = e.dashboards || [];
    var sig = boards.map(function (d) { return d.uid; }).join(',');
    var nav = $('evals-nav');
    if (sig !== navSig) {
      navSig = sig;
      [].forEach.call(nav.querySelectorAll('[data-view]:not([data-view="auto"])'),
        function (n) { n.remove(); });
      boards.forEach(function (d) {
        var b = document.createElement('button');
        b.className = 'subb';
        b.dataset.view = d.uid;
        b.innerHTML = '<span class="t"></span><span class="s"></span>';
        b.querySelector('.t').textContent = d.label;
        b.querySelector('.s').textContent = 'board';
        b.title = d.title;
        b.addEventListener('click', function () { pickView(d.uid); });
        nav.appendChild(b);
      });
    }
    var view = e.view || 'auto';
    [].forEach.call(nav.querySelectorAll('[data-view]'), function (b) {
      b.classList.toggle('on', b.dataset.view === view);
    });
    $('auto-sub').textContent = e.mode ? e.mode + ' playlist' : 'playlist';
  }

  function pickView(view) {
    if (state && state.evals) {
      state.evals.view = view;          // optimistic, so a tap feels instant
      renderEvalsNav(state.evals);
    }
    post('/api/evals/view', { view: view });
  }

  function renderEvals(s) {
    var e = s.evals || {};
    var frame = $('evals-frame');

    renderEvalsNav(e);

    // Assigning .src reloads the frame, which restarts a playlist from its
    // first dashboard. Only touch it when the URL actually changes — a view
    // being picked, an OS flip while on AUTO, or a uid being re-minted.
    var want = e.view_url || e.url;
    if (want && want !== evalsSrc) {
      evalsSrc = want;
      frame.src = want;
    }

    if (e.grafana) lastGrafanaOk = e.checked;

    var down = !e.grafana;
    $('evals-overlay').hidden = !down;
    if (down) {
      $('ov-title').textContent = evalsSrc
        ? 'Grafana stopped answering'
        : 'Grafana is not answering';
      $('ov-sub').textContent = lastGrafanaOk
        ? 'last good check ' + fmtAgo(lastGrafanaOk) + ' ago'
        : 'no successful check yet';
    }

    var lbl = $('nav-evals');
    if (down) { lbl.textContent = 'unreachable'; lbl.parentNode.classList.add('alert'); }
    else {
      // Which board is on: the pinned one, or the OS playlist AUTO follows.
      var v = e.view || 'auto';
      if (v !== 'auto') {
        var hit = (e.dashboards || []).filter(function (d) { return d.uid === v; })[0];
        lbl.textContent = hit ? hit.label.toLowerCase() : 'metrics';
      } else {
        lbl.textContent = e.mode ? e.mode + ' playlist' : 'metrics';
      }
      lbl.parentNode.classList.remove('alert');
    }
  }

  function renderStrip(s) {
    var ws = s.workstation || {};
    var boot = s.boot || {};
    var look = OS_LOOK[ws.os] || OS_LOOK.unknown;

    $('st-os').innerHTML = '<span class="' + (ws.os === 'off' ? '' : 'live') + '">●</span> ' +
      (boot.in_flight ? 'BOOTING' : look.os);

    var t = series.latest('ws.gpu_temp_c');
    $('st-gpu').innerHTML = t ? 'GPU <b>' + Math.round(t.v) + '°</b>' : '';

    $('nav-deck').textContent = boot.in_flight
      ? 'BOOT IN FLIGHT'
      : (ws.os === 'off' ? 'workstation off' : ws.os + ' up');
    document.querySelector('[data-surface="deck"]')
      .classList.toggle('alert', !!boot.in_flight);
  }

  /* ── render ───────────────────────────────────────────────────────────── */
  var OS_LOOK = {
    windows: { pill: 'ONLINE',  cls: 'ok',   os: 'WINDOWS' },
    linux:   { pill: 'ONLINE',  cls: 'ok',   os: 'LINUX' },
    booting: { pill: 'BOOTING', cls: 'busy', os: 'NO OS YET' },
    off:     { pill: 'OFFLINE', cls: 'off',  os: 'POWERED OFF' },
    unknown: { pill: 'UNKNOWN', cls: 'off',  os: '—' }
  };

  var uiStamp = null;

  function render(s) {
    state = s;
    setObservableMax(s.boot && s.boot.observable_max);

    // The kiosk browser survives a deck-api restart, so newly installed
    // HTML/CSS/JS would otherwise sit on disk doing nothing until someone
    // restarted lightdm. Notice and reload instead.
    var v = s.version && s.version.ui;
    if (v && v !== 'unknown') {
      if (uiStamp === null) uiStamp = v;
      else if (v !== uiStamp) { location.reload(); return; }
    }

    // Traces redraw only when a genuinely new sample lands, not on the 1 Hz
    // clock tick — the Pi 4B has better things to do with its CPU.
    if (series.push(s, TRACKED)) drawAllSparks(series);

    var src = (s.telemetry && s.telemetry.source) || null;
    $('chart-lbl').textContent = 'GPU TEMPERATURE · °C' +
      (src ? '  ·  ' + src : '  ·  no exporter answering');

    renderSurface(s);
    renderEvals(s);
    renderStrip(s);
    paintIdle(s);
    renderSimNav(s);
    setNavPlan(s.nav_plan || []);   // deck-api owns the plan; SSE delivers it
    // Here, not in the NAV paint: that path only runs once the sim is feeding
    // a position, so with no sim the button sat enabled and did nothing —
    // which is the silently-inert control this was meant to prevent.
    setNavPlanDisabled();

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
        'phase ' + boot.phase + ' / ' + getObservableMax() + '  ·  ' + boot.phase_name;
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

    // Controls: one action at a time. Scoped to the boot tiles — the
    // Squadrons launch button shares their look but not their lockout.
    [].forEach.call(document.querySelectorAll('#tiles .tile'), function (t) {
      t.disabled = !!boot.in_flight;
    });
    $('abort').hidden = !boot.in_flight;
    // SHUTDOWN only when there is something to shut down and no boot to race:
    // the same gate deck-api enforces, mirrored so the button is never an
    // invitation to a 409.
    var canOff = !boot.in_flight && (ws.os === 'windows' || ws.os === 'linux');
    $('shutdown').hidden = !canOff;
    if (canOff) $('shutdown-sub').textContent = ws.os.toUpperCase();
    $('foot').textContent = Date.now() < footUntil ? footMsg
      : boot.in_flight ? 'a WOL packet already sent cannot be recalled'
      : 'hold to arm';

    // Workstation — only what the existing probes can honestly say.
    $('ws-pill').textContent = look.pill;
    $('ws-pill').className = 'h-pill ' + look.cls;
    rows($('ws-rows'), [
      ['os', ws.os === 'booting' ? 'no exporter yet' : (ws.os || '—')],
      ['address', ws.ip || '—'],
      ['boot agent', ws.agent ? ':9107 answering' : 'no answer'],
      // Two clocks, two labels. `changed` is when the OS state flipped;
      // `last seen` is the last successful contact — the one that matters
      // during an outage, and NOT zero just because a probe ran and confirmed
      // the host is dark.
      ['changed', fmtAgo(ws.since)],
      ['last seen', ws.os === 'windows' || ws.os === 'linux'
        ? 'now' : fmtAgo(ws.last_alive)]
    ]);

    var pi = s.pi || {};
    rows($('pi-rows'), [
      ['cpu', pi.cpu_pct != null ? pi.cpu_pct + ' %' : '—'],
      ['soc temp', pi.temp_c != null ? pi.temp_c + ' °C' : '—'],
      ['memory', pi.mem_pct != null ? pi.mem_pct + ' %' : '—'],
      ['uptime', pi.uptime != null ? fmtAgo(Date.now() / 1000 - pi.uptime) : '—']
    ]);

    // The mini: liveness and open doors, nothing invented beyond them.
    var mini = s.mini || {};
    $('mini-pill').textContent = mini.up ? 'ONLINE' : 'OFFLINE';
    $('mini-pill').className = 'h-pill ' + (mini.up ? 'ok' : 'off');
    var doors = [mini.ssh ? 'ssh' : null, mini.screen ? 'screen share' : null]
      .filter(Boolean).join(' · ');
    rows($('mini-rows'), [
      ['address', mini.ip || '—'],
      ['services', mini.up ? (doors || 'none visible') : '—'],
      ['last seen', mini.up ? 'now' : fmtAgo(mini.last_alive)]
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
  // ── wiring ────────────────────────────────────────────────────────────
  // Guarded so that a crash in here can never stop connect() below from
  // running. The kiosk's only self-heal path is the ui-stamp check inside
  // render(), which needs the stream alive: a mid-deploy reload once fetched
  // a mismatched index.html/deck.js pair, threw on a missing element in this
  // section, and died before connect() — leaving a page that could never see
  // the corrected files. A control that fails to wire is a dead button; a
  // wiring crash that stops the stream is a dead panel.
  try {

  // Boot tiles only — .sqd-launch also carries .tile for its styling, and
  // must not fire /api/boot with an undefined intent.
  [].forEach.call(document.querySelectorAll('#tiles .tile'), function (tile) {
    wireHold(tile, function () {
      post('/api/boot', { intent: tile.dataset.intent });
    });
  });
  wireHold($('abort'), function () { post('/api/abort', {}); });
  // A refused shutdown SAYS SO. The first field deployment had the panel
  // updated and both boot agents not, so every hold was answered "the boot
  // agent did not answer" - and the panel swallowed it, which reads as a
  // broken button rather than a stale agent. The refusal goes to the foot
  // line for a few seconds; render() honours the notice window.
  wireHold($('shutdown'), function () {
    post('/api/shutdown', {}).then(function (r) {
      if (r && !r.ok && r.message) footNotice(r.message);
      else if (!r) footNotice('deck-api did not answer');
    });
  });
  // The idle tiles drive the same endpoint through the same shape of call -
  // one implementation of "start a boot", two places to press it.
  wireIdle(function (intent) { post('/api/boot', { intent: intent }); });


  // Same hold-to-confirm as the boot tiles. wireHold already refuses a
  // disabled element, so a control the aircraft has not got cannot be fired.
  // The AUTO sub-nav button is static HTML, so the dynamic-button wiring in
  // renderEvalsNav never touches it — and no revision ever wired it, which
  // made pinning a board a one-way door: you could leave the playlist but
  // not return to it.
  $('evals-nav').querySelector('[data-view="auto"]')
    .addEventListener('click', function () { pickView('auto'); });

  // NAV and SIM install their own listeners. Each owns the state its controls
  // read — plan, zoom, trail, pending commands — so wiring them from out here
  // would mean reaching into it, which is exactly what putting them in their
  // own modules was for.
  wireNav();
  wireSim();

  // Surface navigation. Applied locally first so a tap feels instant on a
  // touch panel; the SSE frame confirms it a moment later.
  [].forEach.call(document.querySelectorAll('[data-surface]'), function (btn) {
    btn.addEventListener('click', function () {
      var name = btn.dataset.surface;
      if (state && state.surface) {
        state.surface.active = name;
        renderSurface(state);
      }
      post('/api/surface', { surface: name });
    });
  });

  // Tapping the now-playing widget opens AUDIO. The rail already shows the
  // track and a spectrum too small to be worth looking at, so the obvious
  // gesture is to touch it and get the full-width one — and the strip button
  // is there for anyone who does not think to try. The transport buttons sit
  // inside this element and must NOT open the surface: a tap on ⏭ is a skip,
  // not navigation, which is why this checks what was actually hit.
  (function () {
    var np = $('np');
    if (!np) return;
    np.addEventListener('click', function (e) {
      if (e.target.closest('.np-b')) return;
      if (state && state.surface) {
        state.surface.active = 'audio';
        renderSurface(state);
      }
      post('/api/surface', { surface: 'audio' });
    });
  })();

  } catch (err) {
    // Wired what we could; connect() below still runs, so the ui-stamp check
    // inside render() can still reload this page into a consistent pair.
    try { console.error('deck wiring failed:', err); } catch (ignored) { }
  }

  /* ── live state ───────────────────────────────────────────────────────── */
  var es = null, retry = 1000;

  function connect() {
    if (es) es.close();
    es = new EventSource('/api/events');

    es.onopen = function () {
      retry = 1000;
      $('conn').textContent = 'live';
      $('conn').className = 'live';
      $('st-conn').textContent = 'live';
      $('st-conn').className = 'live';
    };

    es.onmessage = function (ev) {
      try { render(JSON.parse(ev.data)); } catch (err) { /* keep last good */ }
    };

    es.onerror = function () {
      $('conn').textContent = 'reconnecting…';
      $('conn').className = 'dead';
      $('st-conn').textContent = 'reconnecting…';
      $('st-conn').className = 'dead';
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
