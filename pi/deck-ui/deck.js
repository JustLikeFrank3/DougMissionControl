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
    { n: 'LOGON',    s: 'greeting calls back' },
    { n: 'LAUNCHED', s: 'app started' }
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
    // The stat tiles put data-key on the .stat div, not on their svg — so the
    // svg's own key is undefined there and this looked up series[undefined],
    // which is why every stat spark has read "no data — exporter not
    // answering" since the day it shipped, healthy exporter or not. Fall back
    // to the parent's key.
    var key = svg.dataset.key ||
      (svg.parentNode && svg.parentNode.dataset && svg.parentNode.dataset.key);
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

    // A stale number in primary type is a lie readable from across the room —
    // and this fires exactly when the workstation is down or rebooting, the one
    // moment someone actually looks. Stale (> ~3 scrape intervals) dims the
    // value AND stamps its age beside it in the same glance; a value older
    // than two minutes stops being shown at all.
    [].forEach.call(document.querySelectorAll('.stat'), function (el) {
      var p = latest(el.dataset.key);
      var v = el.querySelector('.v');
      var age = p ? Date.now() / 1000 - p.t : Infinity;
      var stale = age > 15;
      v.className = 'v' + (stale ? ' stale' : '');
      if (!p || age > 120) {
        v.innerHTML = '—';
      } else {
        v.innerHTML =
          (Math.abs(p.v) >= 100 ? Math.round(p.v) : Math.round(p.v * 10) / 10) +
          '<span class="u">' + (el.dataset.unit || '') + '</span>' +
          (stale ? '<span class="age">· ' + fmtAgo(p.t) + ' ago</span>' : '');
      }
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
    npPoll(active === 'deck');
    navPoll(active === 'nav');
    dspPoll(active === 'displays');
  }

  /* ── SCREENS (DDC via the Windows agent) ───────────────────────────────
     One card per physical monitor. Each pill shows the input that monitor
     REPORTS on read-back — never the commanded one. A switch a panel
     ignored shows up as the pill not moving, which is the truth. */
  var DSP_INPUTS = [
    ['vga', 'VGA'], ['hdmi1', 'HDMI 1'], ['hdmi2', 'HDMI 2'],
    ['dp1', 'DP 1'], ['dp2', 'DP 2']
  ];
  // Windows reports "Generic PnP Monitor" for these panels, so the model name
  // is stated here rather than guessed from EDID. Only used when the monitor
  // does not name itself.
  var DSP_MODEL = 'HP32F';
  var dspTimer = null, dspSig = null;
  var dspDefaults = null;   // deck-api's stated fallback for silent panels

  function dspPoll(on) {
    if (on && !dspTimer) { dspTick(); dspTimer = setInterval(dspTick, 3000); }
    else if (!on && dspTimer) { clearInterval(dspTimer); dspTimer = null; }
  }

  function dspTick() {
    fetch('/api/monitor', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(paintDsp)
      .catch(function () { paintDsp({ available: false, reason: 'deck-api unreachable' }); });
  }

  function paintDsp(d) {
    d = d || {};
    var mons = d.monitors || [];
    var live = d.available && mons.length > 0;
    $('dsp-ph').hidden = live;
    $('dsp-live').hidden = !live;
    $('dsp-pill').textContent = d.available ? 'NO MONITORS' : 'NO LINK';
    $('nav-displays').textContent = live ? mons.length + ' via ddc'
      : (d.available ? 'none' : 'no link');
    if (!live) return;

    dspDefaults = (d.default_inputs && d.default_inputs.length) ? d.default_inputs : null;

    var box = $('dsp-live');
    // position and size join the signature: the cards are ordered by desktop
    // geometry, so a monitor moving or a display server appearing has to
    // rebuild them, not just repaint the pills.
    var sig = mons.map(function (m) {
      return m.index + ':' + m.desc + ':' + (m.position || '') + ':' +
        (m.w || '') + 'x' + (m.h || '') +
        ':' + (m.inputs || dspDefaults || []).join(',');
    }).join('|');
    if (sig !== dspSig) {
      dspSig = sig;
      box.innerHTML = '';
      mons.forEach(function (m) {
        var card = document.createElement('div');
        card.className = 'dspcard';
        card.dataset.idx = m.index;
        card.innerHTML =
          '<div class="h-top"><span class="h-name"></span>' +
          '<span class="h-pill dsp-obs">—</span></div>' +
          '<div class="dsp-desc"></div><div class="dsp-btns"></div>';
        // Name from the panel's own EDID model where it gives one — Windows
        // says "Generic PnP Monitor" for plenty of screens, so fall back to
        // the geometry-derived position rather than inventing a model.
        var model = (m.desc && !/generic/i.test(m.desc)) ? m.desc : DSP_MODEL;
        card.querySelector('.h-name').textContent =
          (model ? model + ' ' : 'MON ') + (m.position || (m.index + 1));
        // Every geometry field is optional. DRM knows the resolution without a
        // display server, but only the display server knows where a panel sits
        // on the desktop, and a Wayland compositor may refuse to say — so the
        // agents send what they have. Build the line from the parts that
        // arrived instead of assuming all of them did, which is what rendered
        // a literal "at xundefined" for the Linux agent.
        // The desktop x offset is deliberately NOT shown. It exists to sort the
        // cards and to derive LEFT/RIGHT, and once the card is titled with that
        // position it says nothing a person standing at the desk can use — "at
        // x1920" is a fact about the framebuffer, not about the monitor.
        var bits = [];
        if (m.w && m.h) bits.push(m.w + '×' + m.h);
        if (m.primary) bits.push('primary');
        card.querySelector('.dsp-desc').textContent = bits.length ? bits.join(' · ') : '—';
        var btns = card.querySelector('.dsp-btns');
        // Inputs the panel DECLARED win; a panel that refuses to say (the HP
        // 32f refuses outright) falls back to deck-api's configured default,
        // which is an operator-stated fact rather than a guess from a model.
        var allowed = m.inputs || dspDefaults;
        var offer = DSP_INPUTS.filter(function (inp) {
          return !allowed || allowed.indexOf(inp[0]) !== -1;
        });
        btns.style.gridTemplateColumns = 'repeat(' + offer.length + ', 1fr)';
        offer.forEach(function (inp) {
          var b = document.createElement('button');
          b.className = 'simbtn';
          b.innerHTML = '<span></span><i class="hold"></i>';
          b.querySelector('span').textContent = inp[1];
          wireHold(b, function () {
            post('/api/monitor', { input: inp[0], index: m.index })
              .then(function () { setTimeout(dspTick, 1500); });
          });
          btns.appendChild(b);
        });
        box.appendChild(card);
      });
    }
    mons.forEach(function (m) {
      var card = box.querySelector('.dspcard[data-idx="' + m.index + '"]');
      if (!card) return;
      var pill = card.querySelector('.dsp-obs');
      pill.textContent = !m.ddc ? 'NO DDC'
        : (m.input && m.input !== 'other') ? m.input.toUpperCase()
        : 'INPUT ' + m.input_raw;
      pill.className = 'h-pill dsp-obs ' + (m.ddc ? 'ok' : 'off');
    });
  }

  /* ── NAV ───────────────────────────────────────────────────────────────
     Moving map from the sim agent's observed position, via the same
     /api/sim pass-through the SIM surface uses. The trail is where the
     aircraft HAS been; the steer arrow is (true bearing to waypoint) minus
     (true track) — fly the arrow to the top and the waypoint arrives.
     Nothing here is commanded state, and there are no map tiles: a vector
     trail on dark is cheaper for the Pi and more EFIS anyway. */
  // The flight plan is the operator's, not ours: an ordered list of
  // {name, lat, lon} held by deck-api (so a kiosk reload keeps it), edited
  // from the panel via search. The selected entry is the steer target.
  var navPlan = [];
  var navSelIdx = 0;
  var navPlanSig = null;
  var navTrail = [];               // {t, lat, lon} — browser memory only
  // Implied ground speed above which the aircraft did not fly there, it was
  // put there. Comfortably above anything the sim's aircraft do, and far
  // below the rate a relocate across a continent implies.
  var NAV_TELEPORT_KT = 1500;
  var navTimer = null;
  var navZoom = null;              // null = auto-fit; integer = manual slippy z
  // Manual zoom range. The auto-fit stops at 15; manual goes one closer, which
  // is the level that shows taxiways.
  var NAV_ZMIN = 8, NAV_ZMAX = 16;
  var navLastZ = 12;               // whatever the auto-fit last chose

  function navPoll(on) {
    if (on && !navTimer) { navTick(); navTimer = setInterval(navTick, 500); }
    else if (!on && navTimer) { clearInterval(navTimer); navTimer = null; }
  }

  function navTick() {
    fetch('/api/sim', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(paintNav)
      .catch(function () { paintNav({ link: false }); });
  }

  var NM_PER_DEG = 60;
  function navXY(lat, lon, lat0, lon0) {
    // Local flat projection in nm around the reference — exact enough for a
    // wall map at Florida distances, and vastly cheaper than real geodesy.
    var k = Math.cos(lat0 * Math.PI / 180);
    return { x: (lon - lon0) * NM_PER_DEG * k, y: (lat - lat0) * NM_PER_DEG };
  }

  function navDistBrg(lat1, lon1, lat2, lon2) {
    var p = navXY(lat2, lon2, lat1, lon1);
    var dist = Math.sqrt(p.x * p.x + p.y * p.y);
    var brg = (Math.atan2(p.x, p.y) * 180 / Math.PI + 360) % 360;
    return { dist: dist, brg: brg };
  }

  function fmtEte(nm, gs) {
    if (!gs || gs < 5) return 'ETE — · ' + gs + ' kt';
    var min = nm / gs * 60;
    return 'ETE ' + (min >= 90 ? (min / 60).toFixed(1) + ' h' : Math.round(min) + ' min');
  }

  function paintNav(d) {
    d = d || {};
    var st = d.session ? d.state : null;
    var r = st && st.readouts;
    var live = !!(r && typeof r.lat === 'number');

    $('navp-ph').hidden = live;
    $('navp-live').hidden = !live;
    $('navp-pill').textContent = !d.link ? 'NO LINK'
      : !d.session ? 'NO SIM'
      : 'AGENT PREDATES NAV';       // session up but no lat in readouts
    if (!live) return;

    // Trail: record when we have actually moved (~37 m), prune at 45 min.
    var last = navTrail[navTrail.length - 1];
    if (last) {
      // A relocate — new departure airport, repositioned flight — is
      // instantaneous, and the trail is one polyline, so without this the map
      // draws a straight blue line from the old airport to the new one and
      // keeps it for 45 minutes. It also poisons the auto-fit, which bounds
      // the view on the trail: one relocate and the map zooms out to hold two
      // states at once.
      //
      // Detect it as implied ground speed rather than raw distance. Dividing
      // by elapsed time is what keeps a backgrounded tab or a missed poll —
      // both of which produce a legitimately large gap — from being mistaken
      // for a jump.
      var moved = navDistBrg(last.lat, last.lon, r.lat, r.lon).dist;
      var hrs = Math.max(Date.now() - last.t, 1000) / 3600000;
      if (moved / hrs > NAV_TELEPORT_KT) navTrail.length = 0;
      last = navTrail[navTrail.length - 1];
    }
    if (!last || navDistBrg(last.lat, last.lon, r.lat, r.lon).dist > 0.02) {
      navTrail.push({ t: Date.now(), lat: r.lat, lon: r.lon });
    }
    var cut = Date.now() - 45 * 60000;
    while (navTrail.length && navTrail[0].t < cut) navTrail.shift();

    if (navSelIdx >= navPlan.length) navSelIdx = Math.max(0, navPlan.length - 1);
    var wpt = navPlan[navSelIdx] || null;

    if (wpt) {
      var nav = navDistBrg(r.lat, r.lon, wpt.lat, wpt.lon);
      // Route total: aircraft -> wpt1 -> ... -> N, since the start point is
      // wherever the aircraft actually is.
      var total = 0, prev = { lat: r.lat, lon: r.lon };
      navPlan.forEach(function (w) {
        total += navDistBrg(prev.lat, prev.lon, w.lat, w.lon).dist;
        prev = w;
      });
      $('navp-brg').textContent = ('00' + Math.round(nav.brg)).slice(-3) + '°T · ' +
        (nav.dist >= 10 ? Math.round(nav.dist) : nav.dist.toFixed(1)) + ' nm';
      $('navp-ete').textContent = fmtEte(nav.dist, r.gs_kt) + ' · ' + wpt.name +
        (navPlan.length > 1 ? ' · route ' + Math.round(total) + ' nm' : '');
      $('navp-arrow-g').style.transform =
        'rotate(' + ((nav.brg - (r.trk_true || 0) + 360) % 360) + 'deg)';
      $('nav-nav').textContent = wpt.name.slice(0, 12) + ' ' +
        (nav.dist >= 10 ? Math.round(nav.dist) : nav.dist.toFixed(1)) + ' nm';
    } else {
      $('navp-brg').textContent = '—';
      $('navp-ete').textContent = 'no waypoints';
      $('navp-arrow-g').style.transform = 'rotate(0deg)';
      $('nav-nav').textContent = 'moving map';
    }
    $('navp-trk').textContent = ('00' + (r.trk_true || 0)).slice(-3) + '°T · ' + r.gs_kt + ' kt';
    $('navp-alt').textContent = r.alt_ft + ' ft · ' + r.ias_kt + ' kt ias';

    drawNavMap(r, wpt);
    renderNavPlan(r);
  }

  /* The plan list. Rebuilt when the plan itself changes; the per-row live
     distances update every paint without rebuilding the DOM. */
  function renderNavPlan(r) {
    var box = $('navp-plan');
    var sig = navPlan.map(function (w) { return w.name + w.lat + w.lon; }).join('|');
    if (sig !== navPlanSig) {
      navPlanSig = sig;
      box.innerHTML = '';
      if (!navPlan.length) {
        var e = document.createElement('div');
        e.className = 'empty';
        e.textContent = 'No waypoints. ADD searches the real map — type a place, tap a result.';
        box.appendChild(e);
      }
      navPlan.forEach(function (w, i) {
        var row = document.createElement('div');
        row.className = 'navrow';
        row.innerHTML = '<span class="n"></span><span class="t"></span>' +
                        '<span class="d"></span><button class="x">✕</button>';
        row.querySelector('.n').textContent = (i + 1);
        row.querySelector('.t').textContent = w.name;
        row.addEventListener('click', function () { navSelIdx = i; navTick(); });
        row.querySelector('.x').addEventListener('click', function (ev) {
          ev.stopPropagation();
          var next = navPlan.slice(0, i).concat(navPlan.slice(i + 1));
          post('/api/nav/plan', { waypoints: next });
        });
        box.appendChild(row);
      });
    }
    [].forEach.call(box.querySelectorAll('.navrow'), function (row, i) {
      row.classList.toggle('on', i === navSelIdx);
      if (r && navPlan[i]) {
        var d = navDistBrg(r.lat, r.lon, navPlan[i].lat, navPlan[i].lon).dist;
        row.querySelector('.d').textContent =
          (d >= 10 ? Math.round(d) : d.toFixed(1)) + ' nm';
      }
    });
  }

  /* Search overlay + on-screen keyboard. Geocoding runs through deck-api's
     Nominatim proxy; a tapped result is appended to the plan. */
  var navQ = '';

  var navKbdMode = 'wpt';          // 'wpt' = geocode search, 'save' = plan name
  var navKbdGoBtn = null;

  function navSearchOpen(on, mode) {
    navKbdMode = mode || 'wpt';
    $('navp-search').hidden = !on;
    if (navKbdGoBtn) navKbdGoBtn.textContent = navKbdMode === 'save' ? 'SAVE' : 'SEARCH';
    if (on) {
      navQ = ''; navQPaint();
      $('navp-results').innerHTML = navKbdMode === 'save'
        ? '<div class="nsx-msg">Name the current ' + navPlan.length +
          '-waypoint plan, then SAVE. It survives reboots.</div>'
        : '';
    }
  }

  function navQPaint() {
    $('navp-q').textContent = navQ.length ? navQ : ' ';
  }

  function navSearchGo() {
    if (navQ.trim().length < 2) return;
    if (navKbdMode === 'save') {
      post('/api/nav/plans', { action: 'save', name: navQ.trim() }).then(function (rsp) {
        if (rsp && rsp.ok) { navSearchOpen(false); navPlansOpen(true); }
        else {
          $('navp-results').innerHTML = '<div class="nsx-msg">SAVE FAILED · ' +
            ((rsp && rsp.reason) || 'no reply') + '</div>';
        }
      });
      return;
    }
    var res = $('navp-results');
    res.innerHTML = '<div class="nsx-msg">searching…</div>';
    fetch('/api/nav/geocode?q=' + encodeURIComponent(navQ.trim()), { cache: 'no-store' })
      .then(function (x) { return x.json(); })
      .then(function (d) {
        var hits = (d && d.results) || [];
        res.innerHTML = '';
        if (!hits.length) {
          res.innerHTML = '<div class="nsx-msg">nothing found — OSM knows places, not brands; try adding a city</div>';
          return;
        }
        hits.forEach(function (h) {
          var b = document.createElement('button');
          b.className = 'nsx-r';
          b.innerHTML = '<span class="t"></span><span class="s"></span>';
          b.querySelector('.t').textContent = h.name;
          b.querySelector('.s').textContent = h.detail || (h.lat.toFixed(4) + ', ' + h.lon.toFixed(4));
          b.addEventListener('click', function () {
            var next = navPlan.concat([{ name: h.name.toUpperCase(), lat: h.lat, lon: h.lon }]);
            post('/api/nav/plan', { waypoints: next }).then(function () {
              navSelIdx = next.length - 1;   // new waypoint becomes the target
              navSearchOpen(false);
            });
          });
          res.appendChild(b);
        });
      })
      .catch(function () {
        res.innerHTML = '<div class="nsx-msg">search failed — no internet from the Pi?</div>';
      });
  }

  function navKbdBuild() {
    var rows = ['1234567890', 'QWERTYUIOP', 'ASDFGHJKL', 'ZXCVBNM'];
    var kbd = $('navp-kbd');
    rows.forEach(function (line) {
      var kr = document.createElement('div');
      kr.className = 'krow';
      line.split('').forEach(function (ch) {
        var k = document.createElement('button');
        k.textContent = ch;
        k.addEventListener('click', function () { navQ += ch; navQPaint(); });
        kr.appendChild(k);
      });
      kbd.appendChild(kr);
    });
    var last = document.createElement('div');
    last.className = 'krow';
    [['SPACE', 'space', function () { navQ += ' '; }],
     ['⌫', 'wide', function () { navQ = navQ.slice(0, -1); }],
     ['CLEAR', 'wide', function () { navQ = ''; }],
     ['SEARCH', 'wide go', navSearchGo]].forEach(function (def) {
      var k = document.createElement('button');
      k.textContent = def[0];
      k.className = def[1];
      k.addEventListener('click', function () { def[2](); navQPaint(); });
      if (def[0] === 'SEARCH') navKbdGoBtn = k;   // relabels to SAVE in name mode
      last.appendChild(k);
    });
    kbd.appendChild(last);
  }

  /* Saved plans overlay: tap to load, ✕ to delete. The plan itself arrives
     back through the SSE state, same as any other plan edit. */
  function navPlansOpen(on) {
    $('navp-plans').hidden = !on;
    if (!on) return;
    var list = $('navp-plans-list');
    list.innerHTML = '<div class="nsx-msg">loading…</div>';
    fetch('/api/nav/plans', { cache: 'no-store' })
      .then(function (x) { return x.json(); })
      .then(function (d) {
        var plans = (d && d.plans) || [];
        list.innerHTML = plans.length ? ''
          : '<div class="nsx-msg">No saved plans yet. Build one, then SAVE it.</div>';
        plans.forEach(function (p) {
          var row = document.createElement('div');
          row.className = 'navrow';
          row.innerHTML = '<span class="t"></span><span class="d"></span>' +
                          '<button class="x">✕</button>';
          row.querySelector('.t').textContent = p.name;
          row.querySelector('.d').textContent = p.count + ' wpts';
          row.addEventListener('click', function () {
            post('/api/nav/plans', { action: 'load', name: p.name }).then(function () {
              navSelIdx = 0;
              navPlansOpen(false);
            });
          });
          row.querySelector('.x').addEventListener('click', function (ev) {
            ev.stopPropagation();
            post('/api/nav/plans', { action: 'delete', name: p.name })
              .then(function () { navPlansOpen(true); });   // refresh the list
          });
          list.appendChild(row);
        });
      })
      .catch(function () {
        list.innerHTML = '<div class="nsx-msg">could not load plans</div>';
      });
  }

  /* OpenStreetMap raster tiles under the overlays, dimmed so the EFIS
     symbology stays legible on top of real roads, lakes and theme parks.
     One kiosk is comfortably inside OSM's tile usage policy; attribution is
     drawn on the map as required. No internet degrades gracefully: missing
     tiles simply do not draw and the overlays stand alone on dark.
     Projection is Web Mercator throughout so tiles and trail agree. */
  var TILE = 256;
  var navTiles = {};             // url -> Image
  var navTileOrder = [];         // insertion order, for crude eviction

  function navWorld(lat, lon, z) {
    var n = TILE * Math.pow(2, z);
    var rad = lat * Math.PI / 180;
    return {
      x: (lon + 180) / 360 * n,
      y: (1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2 * n
    };
  }

  function navTile(url) {
    var img = navTiles[url];
    if (img) return img;
    img = new Image();
    img.src = url;
    navTiles[url] = img;
    navTileOrder.push(url);
    if (navTileOrder.length > 300) delete navTiles[navTileOrder.shift()];
    return img;
  }

  function drawNavMap(r, wpt) {
    var cv = $('navp-canvas');
    if (cv.width !== cv.clientWidth || cv.height !== cv.clientHeight) {
      cv.width = cv.clientWidth; cv.height = cv.clientHeight;
    }
    var g = cv.getContext('2d');
    var W = cv.width, H = cv.height;
    g.clearRect(0, 0, W, H);

    var latC, lonC, z;
    if (navZoom !== null) {
      // Manual zoom follows the aircraft; the waypoint is allowed offscreen —
      // that is what the operator asked for by taking the wheel.
      latC = r.lat; lonC = r.lon; z = navZoom;
    } else {
      // Auto: fit aircraft + the whole route + trail, north up, min 6 nm.
      var lats = [r.lat], lons = [r.lon];
      navPlan.forEach(function (w) { lats.push(w.lat); lons.push(w.lon); });
      navTrail.forEach(function (p) { lats.push(p.lat); lons.push(p.lon); });
      latC = (Math.min.apply(0, lats) + Math.max.apply(0, lats)) / 2;
      lonC = (Math.min.apply(0, lons) + Math.max.apply(0, lons)) / 2;
      var spanNm = Math.max(
        (Math.max.apply(0, lats) - Math.min.apply(0, lats)) * NM_PER_DEG,
        (Math.max.apply(0, lons) - Math.min.apply(0, lons)) * NM_PER_DEG *
          Math.cos(latC * Math.PI / 180),
        6) * 1.3;
      // Integer slippy zoom whose scale best fits that span — the map steps
      // between zoom levels as the flight closes in, like any slippy map.
      var mPerPx = spanNm * 1852 / Math.min(W, H);
      z = Math.floor(Math.log(156543.03 * Math.cos(latC * Math.PI / 180) / mPerPx) / Math.LN2);
      z = Math.max(8, Math.min(15, z));
      navLastZ = z;
    }

    var c = navWorld(latC, lonC, z);
    var ox = c.x - W / 2, oy = c.y - H / 2;
    function px(lat, lon) {
      var w = navWorld(lat, lon, z);
      return { x: w.x - ox, y: w.y - oy };
    }

    // Tiles, then a dark wash so the symbology owns the foreground.
    var maxT = Math.pow(2, z);
    for (var tx = Math.floor(ox / TILE); tx <= Math.floor((ox + W) / TILE); tx++) {
      for (var ty = Math.floor(oy / TILE); ty <= Math.floor((oy + H) / TILE); ty++) {
        if (ty < 0 || ty >= maxT) continue;
        var wx = ((tx % maxT) + maxT) % maxT;
        var img = navTile('https://tile.openstreetmap.org/' + z + '/' + wx + '/' + ty + '.png');
        if (img.complete && img.naturalWidth > 0) {
          g.drawImage(img, tx * TILE - ox, ty * TILE - oy);
        }
      }
    }
    g.fillStyle = 'rgba(7, 10, 15, 0.45)';
    g.fillRect(0, 0, W, H);

    var me = px(r.lat, r.lon);
    var scalePxNm = 1852 / (156543.03 * Math.cos(latC * Math.PI / 180) / Math.pow(2, z));

    // Range rings around the aircraft at a tidy step.
    var steps = [1, 2, 5, 10, 20, 50, 100];
    var ring = steps[0];
    for (var i = 0; i < steps.length; i++) {
      if (steps[i] * scalePxNm < Math.min(W, H) / 2.2) ring = steps[i];
    }
    g.strokeStyle = 'rgba(140, 165, 195, 0.55)'; g.fillStyle = '#aebfd4';
    g.font = '12px monospace';
    for (var n = 1; n <= 2; n++) {
      g.beginPath(); g.arc(me.x, me.y, ring * n * scalePxNm, 0, 7); g.stroke();
      g.fillText(ring * n + ' nm', me.x + 6, me.y - ring * n * scalePxNm + 14);
    }

    // Symbology is CASED — a dark outline under every line and glyph, the
    // way charts stay readable over any terrain. Bare magenta on Florida
    // green disappeared; magenta over a dark casing does not.
    function casedLine(path, color, width, dash) {
      [['#070a0f', width + 3.5], [color, width]].forEach(function (pass) {
        g.strokeStyle = pass[0]; g.lineWidth = pass[1];
        if (dash) g.setLineDash(pass[0] === color ? dash : []);
        g.beginPath(); path(); g.stroke();
      });
      g.setLineDash([]); g.lineWidth = 1;
    }
    function casedText(txt, x, y, color, font) {
      g.font = font || 'bold 13px monospace';
      g.lineWidth = 3.5; g.strokeStyle = '#070a0f'; g.strokeText(txt, x, y);
      g.fillStyle = color; g.fillText(txt, x, y);
      g.lineWidth = 1;
    }

    // Trail — where we have actually been.
    if (navTrail.length > 1) {
      casedLine(function () {
        navTrail.forEach(function (p, idx) {
          var q = px(p.lat, p.lon);
          if (idx === 0) g.moveTo(q.x, q.y); else g.lineTo(q.x, q.y);
        });
      }, '#56cfe1', 2.5);
    }

    // The route: the START POINT is the aircraft, always — legs run from the
    // observed position through the waypoints in plan order, Google-Maps
    // style. The selected target additionally gets a dashed direct-to line,
    // which matters when it is not the next waypoint in sequence.
    if (navPlan.length > 0) {
      casedLine(function () {
        g.moveTo(me.x, me.y);
        navPlan.forEach(function (w) {
          var q = px(w.lat, w.lon);
          g.lineTo(q.x, q.y);
        });
      }, '#8fa2b8', 2);
    }
    if (wpt) {
      casedLine(function () {
        var q = px(wpt.lat, wpt.lon);
        g.moveTo(me.x, me.y); g.lineTo(q.x, q.y);
      }, '#ff7ad9', 2.5, [8, 8]);
    }
    navPlan.forEach(function (w, i) {
      var q = px(w.lat, w.lon);
      var on = i === navSelIdx;
      var color = on ? '#ff7ad9' : '#aebfd4';
      g.fillStyle = '#070a0f';
      g.beginPath();
      g.moveTo(q.x, q.y - 9); g.lineTo(q.x + 9, q.y); g.lineTo(q.x, q.y + 9);
      g.lineTo(q.x - 9, q.y); g.closePath(); g.fill();
      g.strokeStyle = color; g.lineWidth = 2.5; g.stroke(); g.lineWidth = 1;
      casedText((i + 1) + ' ' + w.name.slice(0, 14), q.x + 13, q.y + 4, color);
    });

    // The aircraft, rotated to its observed true track.
    g.save();
    g.translate(me.x, me.y);
    g.rotate((r.trk_true || 0) * Math.PI / 180);
    g.fillStyle = '#ffffff'; g.strokeStyle = '#0b0f15';
    g.beginPath();
    g.moveTo(0, -12); g.lineTo(9, 12); g.lineTo(0, 6); g.lineTo(-9, 12);
    g.closePath(); g.fill(); g.stroke();
    g.restore();

    // Required by the tile usage policy, and honest besides.
    g.fillStyle = 'rgba(226, 233, 242, 0.55)'; g.font = '11px monospace';
    g.fillText('© OpenStreetMap contributors', W - 205, H - 8);
  }

  /* ── now-playing widget (DECK rail) ────────────────────────────────────
     Observed playback only: the widget shows what the player reports, and a
     transport tap is a command whose effect arrives in the next poll. Art is
     fetched only when art_id changes — the JSON stays small. */
  var npTimer = null, npArtId = null;

  function npPoll(on) {
    if (on && !npTimer) { npTick(); npTimer = setInterval(npTick, 2000); }
    else if (!on && npTimer) { clearInterval(npTimer); npTimer = null; }
  }

  function npTick() {
    fetch('/api/media', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(paintNp)
      .catch(function () { paintNp({ active: false }); });
  }

  function fmtTime(s) {
    if (!s && s !== 0) return '';
    return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2);
  }

  function paintNp(m) {
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

  /* Commands. The panel NEVER draws the commanded position. A command puts
     PENDING over the last observed state; only the state stream moves what is
     displayed. If nothing moves before the timeout the panel says NO RESPONSE
     and keeps showing the observed position — which is how a command the
     simulator ignored reads as a failed command instead of a lie. */
  var SIM_TIMEOUT = { gear: 9000, flaps: 5000, parking_brake: 3000,
                      landing_lights: 3000, ap_master: 3000 };
  var simPending = {};          // control -> { id, want, from, sent, dead, reason }
  var simSeq = 0;
  var simLast = null;

  // The observed position of a control, in the same vocabulary the buttons use.
  function simObserved(control, c) {
    c = c || {};
    if (control === 'gear') return c.gear ? c.gear.state : null;
    if (control === 'flaps') return c.flaps ? String(c.flaps.index) : null;
    if (control === 'parking_brake') return c.parking_brake ? c.parking_brake.state : null;
    if (control === 'landing_lights') return c.landing_lights ? c.landing_lights.state : null;
    if (control === 'ap_master') return c.ap_master ? c.ap_master.state : null;
    return null;
  }

  function simSend(control, action, value) {
    var c = (simLast && simLast.controls) || {};
    var id = 'c-' + (++simSeq);
    // `from` is what it looked like when we asked — the only way to tell that
    // an incr/decr moved, since we cannot know the target detent up front.
    simPending[control] = { id: id, want: value || null,
                            from: simObserved(control, c),
                            sent: Date.now(), dead: false, reason: null };
    post('/api/sim/command',
         { cmd_id: id, control: control, action: action, value: value })
      .then(function (r) {
        var p = simPending[control];
        if (!p || p.id !== id) return;
        if (!r || !r.accepted) {
          p.dead = true;
          p.reason = (r && r.reason) || 'no reply';
        } else if (r.noop) {
          // Already in the requested position: the agent transmitted nothing,
          // so nothing is going to move and PENDING would hang forever.
          delete simPending[control];
        }
      });
  }

  function simRenderPending(key, control, c) {
    var el = $('simpd-' + key);
    var p = simPending[control];
    if (!p) { el.className = 'simpend'; el.textContent = ''; return; }

    var now = simObserved(control, c);
    var moved = p.want ? (now === p.want) : (now !== null && now !== p.from);
    if (moved) { delete simPending[control]; el.className = 'simpend'; el.textContent = ''; return; }

    var age = Date.now() - p.sent;
    if (p.dead) {
      el.className = 'simpend dead';
      el.textContent = 'REJECTED · ' + String(p.reason).toUpperCase();
      if (age > 6000) delete simPending[control];
      return;
    }
    var limit = SIM_TIMEOUT[control] || 4000;
    if (age > limit) {
      el.className = 'simpend dead';
      el.textContent = 'NO RESPONSE · SHOWING OBSERVED';
      if (age > limit + 6000) delete simPending[control];
      return;
    }
    el.className = 'simpend wait';
    el.textContent = 'PENDING' + (p.want ? ' · ' + p.want.toUpperCase() : '');
  }

  // Absent from `controls` means the aircraft has not got it — skids instead of
  // wheels, no flaps. Grey it and name it; never render a dead control.
  function simAvail(key, present) {
    $('simt-' + key).classList.toggle('na', !present);
    [].forEach.call($('simt-' + key).querySelectorAll('.simbtn'), function (b) {
      b.disabled = !present;
    });
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

    simLast = st;
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

    simRenderPending('gear', 'gear', c);
    simRenderPending('flaps', 'flaps', c);
    simRenderPending('park', 'parking_brake', c);
    simRenderPending('lights', 'landing_lights', c);
    simRenderPending('ap', 'ap_master', c);
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
    navPlan = s.nav_plan || [];   // deck-api owns the plan; SSE delivers it

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

    // Controls: one action at a time. Scoped to the boot tiles — the
    // Squadrons launch button shares their look but not their lockout.
    [].forEach.call(document.querySelectorAll('#tiles .tile'), function (t) {
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


  // Same hold-to-confirm as the boot tiles. wireHold already refuses a
  // disabled element, so a control the aircraft has not got cannot be fired.
  [].forEach.call(document.querySelectorAll('.simbtn'), function (b) {
    wireHold(b, function () {
      simSend(b.dataset.c, b.dataset.a, b.dataset.v || null);
    });
  });

  // The AUTO sub-nav button is static HTML, so the dynamic-button wiring in
  // renderEvalsNav never touches it — and no revision ever wired it, which
  // made pinning a board a one-way door: you could leave the playlist but
  // not return to it.
  $('evals-nav').querySelector('[data-view="auto"]')
    .addEventListener('click', function () { pickView('auto'); });

  // NAV zoom. Steps start from wherever the auto-fit currently sits, so the
  // first tap nudges rather than jumps; FIT hands framing back to auto.
  function navClampZ(z) { return Math.max(NAV_ZMIN, Math.min(NAV_ZMAX, z)); }
  function navSetZoom(zOrNull) {
    navZoom = zOrNull === null ? null : navClampZ(zOrNull);
    $('navp-fit').classList.toggle('on', navZoom === null);
    if (navTimer) navTick();
  }
  $('navp-zin').addEventListener('click', function () {
    navSetZoom((navZoom === null ? navLastZ : navZoom) + 1);
  });
  $('navp-zout').addEventListener('click', function () {
    navSetZoom((navZoom === null ? navLastZ : navZoom) - 1);
  });
  $('navp-fit').addEventListener('click', function () { navSetZoom(null); });

  // Pinch to zoom. The map only ever draws integer slippy zooms, so the
  // gesture is measured continuously and committed each time it crosses a
  // level — the same stepping the auto-fit does as a flight closes in.
  //
  // The base zoom is captured once, at touchdown, and every move is measured
  // against THAT. Reading the current zoom each move would ratchet: each
  // committed step would become the new baseline and a slow steady pinch
  // would run away to the clamp.
  (function () {
    var cv = $('navp-canvas');
    var span0 = 0, z0 = 0, zLast = 0;

    function span(t) {
      var dx = t[0].clientX - t[1].clientX, dy = t[0].clientY - t[1].clientY;
      return Math.sqrt(dx * dx + dy * dy);
    }

    cv.addEventListener('touchstart', function (e) {
      if (e.touches.length !== 2) return;
      e.preventDefault();
      span0 = span(e.touches);
      // Starting from the auto-fit's current level means the first pinch
      // nudges from what is on screen, exactly like the +/- buttons.
      z0 = zLast = (navZoom === null ? navLastZ : navZoom);
    }, { passive: false });

    cv.addEventListener('touchmove', function (e) {
      if (e.touches.length !== 2 || !span0) return;
      e.preventDefault();
      var now = span(e.touches);
      if (now < 8) return;              // fingers together: ratio goes wild
      // Clamp before comparing, not after. Tracking the requested level past
      // the limit would mean a pinch that overshot had to be given back the
      // overshoot before the map moved again.
      var z = navClampZ(Math.round(z0 + Math.log(now / span0) / Math.LN2));
      if (z !== zLast) {
        zLast = z;
        navSetZoom(z);                  // drops FIT back off
      }
    }, { passive: false });

    function end(e) { if (e.touches.length < 2) span0 = 0; }
    cv.addEventListener('touchend', end);
    cv.addEventListener('touchcancel', end);
  })();

  // Flight-plan search overlay + its on-screen keyboard.
  navKbdBuild();
  $('navp-add').addEventListener('click', function () { navSearchOpen(true, 'wpt'); });
  $('navp-close').addEventListener('click', function () { navSearchOpen(false); });
  $('navp-plansbtn').addEventListener('click', function () { navPlansOpen(true); });
  $('navp-plans-close').addEventListener('click', function () { navPlansOpen(false); });
  $('navp-saveas').addEventListener('click', function () {
    navPlansOpen(false);
    navSearchOpen(true, 'save');
  });

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
