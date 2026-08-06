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

  /* ── rolling window ───────────────────────────────────────────────────────
     Lives here and nowhere else. deck-api sends the latest reading and keeps
     no history; this buffer is the only history that exists, and it dies with
     the page. Nothing is written to the Pi's thumb drive. */

  var WINDOW_S = 3600;          // 60 min
  var MAX_PTS = 1200;           // hard cap regardless of sample rate
  var series = Object.create(null);
  var lastSampleTs = 0;

  // Which traces the page actually shows, read off the DOM once.
  var TRACKED = [].map.call(document.querySelectorAll('[data-key]'), function (el) {
    return el.dataset.key;
  }).filter(function (v, i, a) { return a.indexOf(v) === i; });

  function record(key, t, v) {
    var a = series[key] || (series[key] = []);
    a.push({ t: t, v: (typeof v === 'number' && isFinite(v)) ? v : null });
    var cut = t - WINDOW_S;
    var drop = 0;
    while (drop < a.length && a[drop].t < cut) drop++;
    if (drop) a.splice(0, drop);
    if (a.length > MAX_PTS) a.splice(0, a.length - MAX_PTS);
  }

  function pushSamples(s) {
    var tel = s.telemetry || {};
    if (!tel.ts || tel.ts === lastSampleTs) return false;
    lastSampleTs = tel.ts;
    TRACKED.forEach(function (key) {
      var parts = key.split('.');
      var src = tel[parts[0]] || {};
      // A key that is absent this cycle is a genuine gap — the machine was
      // off or mid-reboot — not a zero and not a carried-forward value.
      record(key, tel.ts, parts[1] in src ? src[parts[1]] : null);
    });
    return true;
  }

  function latest(key) {
    var a = series[key];
    if (!a) return null;
    for (var i = a.length - 1; i >= 0; i--) if (a[i].v !== null) return a[i];
    return null;
  }

  /* ── sparkline ────────────────────────────────────────────────────────── */

  function drawSpark(svg) {
    var key = svg.dataset.key;
    var data = series[key] || [];
    var vb = svg.viewBox.baseVal;
    var W = vb.width, H = vb.height;
    var wide = svg.classList.contains('wide');
    var padL = wide ? 34 : 0, padT = wide ? 16 : 4, padB = wide ? 6 : 4;
    var padR = wide ? 62 : 4;

    var pts = data.filter(function (p) { return p.v !== null; });
    if (!pts.length) {
      svg.innerHTML = '<text x="' + (W / 2) + '" y="' + (H / 2 + 4) +
        '" text-anchor="middle">no data — exporter not answering</text>';
      return;
    }

    // x always spans the full window, so a gap reads as a gap rather than
    // the remaining data stretching to fill the box.
    var now = Date.now() / 1000;
    var t0 = now - WINDOW_S;

    var lo = svg.dataset.lo !== undefined ? Number(svg.dataset.lo) : null;
    var hi = svg.dataset.hi !== undefined ? Number(svg.dataset.hi) : null;
    if (lo === null || hi === null) {
      var vs = pts.map(function (p) { return p.v; });
      lo = Math.min.apply(null, vs);
      hi = Math.max.apply(null, vs);
      var span = Number(svg.dataset.minSpan || 1);
      if (hi - lo < span) {                    // don't dramatise flat data
        var mid = (hi + lo) / 2;
        lo = mid - span / 2; hi = mid + span / 2;
      }
      var pad = (hi - lo) * 0.12;
      lo -= pad; hi += pad;
    }

    var X = function (t) { return padL + (W - padL - padR) * Math.max(0, Math.min(1, (t - t0) / WINDOW_S)); };
    var Y = function (v) { return padT + (H - padT - padB) * (1 - (v - lo) / (hi - lo || 1)); };

    // Split into contiguous runs so gaps break the line.
    var runs = [], cur = [];
    data.forEach(function (p) {
      if (p.v === null) { if (cur.length) { runs.push(cur); cur = []; } }
      else cur.push(p);
    });
    if (cur.length) runs.push(cur);

    var out = [];
    if (wide) {
      [0.5, 0].forEach(function (f) {
        var y = padT + (H - padT - padB) * f;
        out.push('<path class="sk-g" d="M' + padL + ' ' + y.toFixed(1) +
                 'H' + (W - padR) + '"/>');
      });
      out.push('<text x="0" y="' + (padT + 10) + '">' + Math.round(hi) + '</text>');
      out.push('<text x="0" y="' + (H - padB) + '">' + Math.round(lo) + '</text>');
    }

    runs.forEach(function (run) {
      var d = run.map(function (p, i) {
        return (i ? 'L' : 'M') + X(p.t).toFixed(1) + ' ' + Y(p.v).toFixed(1);
      }).join('');
      if (run.length > 1) {
        var base = (H - padB).toFixed(1);
        out.push('<path class="sk-a" d="' + d +
                 'L' + X(run[run.length - 1].t).toFixed(1) + ' ' + base +
                 'L' + X(run[0].t).toFixed(1) + ' ' + base + 'Z"/>');
      }
      out.push('<path class="sk-l" d="' + d + '"/>');
    });

    var last = pts[pts.length - 1];
    var fresh = (now - last.t) < 30;
    if (fresh) {
      out.push('<circle class="sk-d" cx="' + X(last.t).toFixed(1) +
               '" cy="' + Y(last.v).toFixed(1) + '" r="' + (wide ? 4 : 3) + '"/>');
    }
    if (wide) {
      out.push('<text class="now" x="' + (W - padR + 8) + '" y="' +
               Y(last.v).toFixed(1) + '" dy="6">' +
               fmtVal(last.v, svg.dataset.unit) + '</text>');
    }

    svg.innerHTML = out.join('');
  }

  function fmtVal(v, unit) {
    if (v === null || v === undefined) return '—';
    var s = Math.abs(v) >= 100 ? Math.round(v) : Math.round(v * 10) / 10;
    return s + (unit ? ' ' + unit : '');
  }

  function drawAllSparks() {
    [].forEach.call(document.querySelectorAll('svg.spark'), drawSpark);

    [].forEach.call(document.querySelectorAll('.stat'), function (el) {
      var p = latest(el.dataset.key);
      var v = el.querySelector('.v');
      var stale = !p || (Date.now() / 1000 - p.t) > 30;
      v.className = 'v' + (stale ? ' stale' : '');
      v.innerHTML = p
        ? (Math.abs(p.v) >= 100 ? Math.round(p.v) : Math.round(p.v * 10) / 10) +
          '<span class="u">' + (el.dataset.unit || '') + '</span>'
        : '—';
    });

    var mins = Math.round(WINDOW_S / 60);
    $('win').textContent = mins + ' min · in memory';
  }

  /* ── surfaces ─────────────────────────────────────────────────────────── */

  var evalsSrc = null;        // only reassign when it CHANGES — see below
  var lastGrafanaOk = null;

  function renderSurface(s) {
    var active = (s.surface && s.surface.active) || 'evals';
    [].forEach.call(document.querySelectorAll('.surface'), function (el) {
      el.classList.toggle('on', el.dataset.name === active);
    });
    [].forEach.call(document.querySelectorAll('.navb'), function (b) {
      b.classList.toggle('on', b.dataset.surface === active);
    });
    simPoll(active === 'sim');
  }

  /* ── SIM ───────────────────────────────────────────────────────────────
     Polled directly rather than carried on the SSE state. Gear travel takes
     about five seconds end to end and deck-api's state poll runs at 2-5 s, so
     the interesting part — the gear in motion — would fall between samples.
     Only runs while the surface is on screen: there is no reason to hold a
     4 Hz conversation with the workstation to feed a hidden panel.

     Everything below renders OBSERVED state. The agent reports what the
     simulator is doing, never what it was told to do. */
  var SIM_MS = 250;
  var simTimer = null, simLastOk = 0;
  var simPhProse = null;

  function simPoll(on) {
    if (on && !simTimer) {
      simTick();
      simTimer = setInterval(simTick, SIM_MS);
    } else if (!on && simTimer) {
      clearInterval(simTimer);
      simTimer = null;
    }
  }

  function simTick() {
    fetch('/api/sim', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) { simLastOk = Date.now(); paintSim(d); })
      .catch(function () { paintSim({ link: false, reason: 'deck-api unreachable' }); });
  }

  // Absent from `controls` means the aircraft has not got it — skids instead of
  // wheels, no flaps. Grey it and name it; never render a dead control.
  function simAvail(key, present) {
    $('simt-' + key).classList.toggle('na', !present);
    if (!present) {
      $('simv-' + key).textContent = 'NOT FITTED';
      var s = $('sims-' + key); if (s) s.textContent = '';
      var u = $('simu-' + key); if (u) u.textContent = '';
    }
    return present;
  }

  function simSimple(key, present, value, onWord) {
    if (!simAvail(key, present)) return;
    var v = $('simv-' + key);
    v.textContent = value.toUpperCase();
    v.className = 'simt-v ' + (value === onWord ? 'sim-on' : 'sim-off');
    $('sims-' + key).textContent = value === onWord ? 'active' : 'inactive';
  }

  function paintSim(d) {
    d = d || {};
    var st = d.session ? d.state : null;
    var live = !!(st && st.controls);

    $('sim-ph').hidden = live;
    $('sim-live').hidden = !live;
    $('sim-pill').textContent = !d.link ? 'NO LINK' : (d.session ? 'LINK' : 'NO SIM');

    if (simPhProse === null) simPhProse = $('sim-ph-s').innerHTML;
    // A missing agent or a sim that is not running are ordinary absences and
    // keep the standing copy. A rejected token or an HTTP error is a fault
    // worth reading off the panel.
    var fault = d.reason && /token|returned/.test(d.reason);
    $('sim-ph-s').innerHTML = fault ? d.reason : simPhProse;
    if (!live) return;

    $('sim-live').classList.toggle('stale', Date.now() - simLastOk > 1500);
    $('sim-ac').textContent = st.aircraft || '(no aircraft)';
    $('sim-meta').textContent = 'SEQ ' + st.seq;

    var c = st.controls || {};

    if (simAvail('gear', !!c.gear)) {
      var g = c.gear;
      var v = $('simv-gear');
      v.textContent = g.state.toUpperCase();
      v.className = 'simt-v ' +
        (g.state === 'transit' ? 'sim-mid' : g.state === 'down' ? 'sim-on' : 'sim-off');
      $('simu-gear').textContent = g.pct.toFixed(1) + '%';
      $('sims-gear').textContent =
        'HANDLE ' + g.handle.toUpperCase() + ' · OBSERVED ' + g.state.toUpperCase();
      var bar = $('simb-gear');
      bar.style.width = g.pct + '%';
      bar.className = g.state === 'transit' ? 'sim-mid' : '';

      // The handle says one thing, the gear reports another, and nothing is
      // moving: the simulator declined the command — over the gear speed
      // limit, on the ground, whatever. Observed at 205-228 kt in a King Air,
      // where drawing the commanded position would have read GEAR DOWN.
      var stuck = g.state !== 'transit' && g.handle !== g.state;
      $('simw-gear').classList.toggle('on', stuck);
      if (stuck) {
        $('simw-gear').textContent =
          'COMMANDED ' + g.handle.toUpperCase() + ' · NOT MOVING · SIM DECLINED';
      }
    }

    if (simAvail('flaps', !!c.flaps)) {
      var f = c.flaps;
      var fv = $('simv-flaps');
      fv.textContent = f.index + ' / ' + f.detents;
      fv.className = 'simt-v ' + (f.index > 0 ? 'sim-on' : 'sim-off');
      $('simu-flaps').textContent = f.angle_deg.toFixed(1) + '°';
      $('sims-flaps').textContent = f.index === 0 ? 'UP / CLEAN' : 'DETENT ' + f.index;
      var pips = $('simp-flaps');
      if (pips.children.length !== f.detents) {
        pips.innerHTML = '';
        for (var i = 0; i < f.detents; i++) pips.appendChild(document.createElement('b'));
      }
      for (var j = 0; j < pips.children.length; j++) {
        pips.children[j].className = j < f.index ? 'f' : '';
      }
    }

    simSimple('park', !!c.parking_brake, c.parking_brake ? c.parking_brake.state : '', 'set');
    simSimple('lights', !!c.landing_lights, c.landing_lights ? c.landing_lights.state : '', 'on');
    simSimple('ap', !!c.ap_master, c.ap_master ? c.ap_master.state : '', 'engaged');

    var r = st.readouts || {};
    $('simv-ias').textContent = r.ias_kt;
    $('simv-alt').textContent = r.alt_ft;
    $('simv-hdg').textContent = ('00' + r.hdg_mag).slice(-3);
  }

  // The strip's SIM sub-label, from the ordinary state poll. Losing the agent
  // is normal operation — it lives and dies with Windows — so this reads as an
  // absence, not an alert.
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

    var t = latest('ws.gpu_temp_c');
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
    observableMax = (s.boot && s.boot.observable_max) || 5;

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
    if (pushSamples(s)) drawAllSparks();

    var src = (s.telemetry && s.telemetry.source) || null;
    $('chart-lbl').textContent = 'GPU TEMPERATURE · °C' +
      (src ? '  ·  ' + src : '  ·  no exporter answering');

    renderSurface(s);
    renderEvals(s);
    renderStrip(s);
    renderSimNav(s);

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
      ['cpu', pi.cpu_pct != null ? pi.cpu_pct + ' %' : '—'],
      ['soc temp', pi.temp_c != null ? pi.temp_c + ' °C' : '—'],
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
