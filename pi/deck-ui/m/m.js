/* m.js — the deck on a phone.

   The same deck-api, the same SSE frame and the same endpoints as the panel,
   rendered as one column for a thumb. Nothing here is a second source of
   truth: every value is the panel's value, every command the panel's command.

   The one thing the panel never needed is the token. The kiosk is loopback
   and deck-api lets loopback in; a phone arrives over the tailnet and must
   carry DECK_TOKEN on every request. It is handed over once in the URL
   (/m?token=…, printed by pi/mobile-link.sh), kept in this browser's storage,
   and stripped from the address bar so it does not sit in history or a
   screenshot. */

import { wireHold } from '../js/ui.js';
import { fmtAgo, fmtClock, fmtDur, fmtTime } from '../js/format.js';

var $ = function (id) { return document.getElementById(id); };
var KEY = 'dmc-deck-token';

// ── token ────────────────────────────────────────────────────────────────

var token = (function () {
  var q = new URLSearchParams(location.search).get('token');
  if (q) {
    try { localStorage.setItem(KEY, q); } catch (e) { /* private mode: session only */ }
    history.replaceState(null, '', location.pathname);
    return q;
  }
  try { return localStorage.getItem(KEY) || ''; } catch (e) { return ''; }
})();

function api(path) {
  if (!token) return path;
  return path + (path.indexOf('?') < 0 ? '?' : '&') + 'token=' + encodeURIComponent(token);
}

// A phone's link to node1 is not a wire. Over the tailnet it can drop to a
// relay, or stall while the phone moves between access points, and fetch()
// on its own waits about a minute before it admits anything is wrong. So
// every request has a deadline, and one that misses it comes back as null,
// "no answer", exactly as a refused connection does.
function request(path, opts, ms) {
  var ctl = window.AbortController ? new AbortController() : null;
  var timer = ctl ? setTimeout(function () { ctl.abort(); }, ms) : 0;
  if (ctl) opts.signal = ctl.signal;
  function done(v) { clearTimeout(timer); return v; }
  return fetch(api(path), opts).then(function (r) {
    if (r.status === 403) { showGate(); return null; }
    return r.json();
  }).then(done, function () { return done(null); });
}

// Reads answer within a few seconds or not at all: deck-api's own slowest
// read gives up after three.
function getJSON(path) {
  return request(path, { cache: 'no-store' }, 8000);
}

// The default outlasts deck-api's slowest command (a shutdown waits up to
// twenty seconds for the workstation), so a deadline here never cuts short
// something that is still going to happen.
function post(path, body, ms) {
  return request(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  }, ms || 30000);
}

function showGate() {
  $('gate').hidden = false;
  $('main').hidden = true;
  setConn('no token', 'dead');
}

$('gate-form').addEventListener('submit', function (e) {
  e.preventDefault();
  var v = $('gate-token').value.trim();
  if (!v) return;
  try { localStorage.setItem(KEY, v); } catch (err) { /* session only */ }
  token = v;
  $('gate').hidden = true;
  $('main').hidden = false;
  connect();
});

// ── stream ───────────────────────────────────────────────────────────────

var es = null, state = null, retry = 0;

function setConn(text, cls) {
  var c = $('conn');
  c.textContent = text;
  c.className = 'conn' + (cls ? ' ' + cls : '');
}

function connect() {
  clearTimeout(retry);
  if (es) es.close();
  // A probe first: EventSource cannot see a 403, it just retries forever,
  // which would read as "reconnecting" to a phone that is simply not allowed.
  getJSON('/api/state').then(function (s) {
    if (!s) {
      // No answer is not a verdict: keep trying while the page is being looked at.
      if ($('gate').hidden) {
        setConn('node1 unreachable', 'dead');
        if (!document.hidden) retry = setTimeout(connect, 5000);
      }
      return;
    }
    render(s);
    es = new EventSource(api('/api/events'));
    es.onopen = function () { setConn('live', 'live'); };
    es.onmessage = function (e) {
      try { render(JSON.parse(e.data)); } catch (err) { /* keep the last good frame */ }
    };
    es.onerror = function () { setConn('reconnecting…', 'dead'); };
  });
}

// A phone sleeps the page; the stream dies under it. Reconnect on return
// rather than showing a frame from an hour ago as if it were live.
document.addEventListener('visibilitychange', function () {
  if (document.hidden) {
    clearTimeout(retry);
    if (es) { es.close(); es = null; }
    pollAll(false);
  } else { connect(); pollAll(true); }
});

// ── render (SSE frame) ───────────────────────────────────────────────────

var OS_LOOK = {
  windows: { pill: 'ONLINE', cls: 'ok', os: 'WINDOWS' },
  linux: { pill: 'ONLINE', cls: 'ok', os: 'LINUX' },
  booting: { pill: 'BOOTING', cls: 'busy', os: 'NO OS YET' },
  off: { pill: 'OFFLINE', cls: '', os: 'POWERED OFF' },
  unknown: { pill: 'UNKNOWN', cls: '', os: '—' },
};

function render(s) {
  state = s;
  var ws = s.workstation || {}, boot = s.boot || {};
  var look = OS_LOOK[ws.os] || OS_LOOK.unknown;

  if (boot.in_flight) {
    $('ws-pill').textContent = 'BOOTING → ' + String(boot.target || '').toUpperCase();
    $('ws-pill').className = 'pill busy';
    $('ws-os').textContent = fmtDur(boot.elapsed);
    $('ws-phase').hidden = false;
    $('ws-phase').textContent = 'phase ' + boot.phase + ' / ' + (boot.observable_max || '—') +
      ' · ' + (boot.phase_name || '') + (boot.intent ? ' · ' + boot.intent : '');
    $('ws-meta').textContent = '';
  } else {
    $('ws-pill').textContent = look.pill;
    $('ws-pill').className = 'pill ' + look.cls;
    $('ws-os').textContent = look.os;
    $('ws-phase').hidden = true;
    $('ws-meta').textContent = ws.since
      ? (ws.os === 'off' ? 'since ' : 'up ') + fmtAgo(ws.since) : '';
  }

  [].forEach.call(document.querySelectorAll('.tile'), function (t) {
    t.disabled = !!boot.in_flight;
  });
  $('abort').hidden = !boot.in_flight;
  var canOff = !boot.in_flight && (ws.os === 'windows' || ws.os === 'linux');
  $('shutdown').hidden = !canOff;
  if (canOff) $('shutdown-t').textContent = 'HOLD TO SHUT DOWN ' + ws.os.toUpperCase();

  paintFleet(s);
  paintDouglasHead(s.douglas || {});
  paintEvents(s.events || []);
  $('foot').textContent = 'node1' +
    (s.surface && s.surface.active ? ' · panel showing ' + s.surface.active.toUpperCase() : '');
}

function paintFleet(s) {
  var ws = s.workstation || {}, mini = s.mini || {}, mob = s.mobile || {};
  var dgl = s.douglas || {}, voice = s.voice || {};
  var dm = dgl.model || {};
  var rows = [
    ['DOUG DUALBOOT', ws.os === 'windows' || ws.os === 'linux' ? ws.os
      : ws.os === 'booting' ? 'booting' : 'off', ws.os === 'windows' || ws.os === 'linux'],
    ['DOUG PRIME', 'up · drawing the panel', true],
    ['MINIDOUG4', mini.up ? 'up' : 'off · seen ' + fmtAgo(mini.last_alive), !!mini.up],
    ['DOUGMOBILE', mob.up ? 'up' : 'away', !!mob.up],
    ['DOUGLAS', !dgl.up ? 'offline · seen ' + fmtAgo(dgl.last_alive)
      : dm.state === 'running' ? (dm.name || 'serving')
      : dm.state === 'loading' ? 'loading model' : 'model server down',
     !!dgl.up, !dgl.up],
    ['DOUGVOICE', voice.up ? (voice.satellite ? 'listening' : 'up · satellite down') : 'off',
     !!voice.up],
  ];
  var el = $('fleet');
  el.innerHTML = '';
  rows.forEach(function (r) {
    var d = document.createElement('div');
    d.className = 'host' + (r[2] ? ' up' : '') + (r[3] ? ' bad' : '');
    d.innerHTML = '<i></i><b></b><span></span>';
    d.querySelector('b').textContent = r[0];
    d.querySelector('span').textContent = r[1];
    el.appendChild(d);
  });
}

function paintEvents(evs) {
  var el = $('events');
  el.innerHTML = '';
  evs.slice(0, 8).forEach(function (e) {
    var d = document.createElement('div');
    d.className = 'ev ' + (e.level || '');
    d.innerHTML = '<time></time><b></b><span></span>';
    d.querySelector('time').textContent = fmtClock(e.ts || e.t);
    d.querySelector('b').textContent = e.source || '';
    d.querySelector('span').textContent = e.text || '';
    el.appendChild(d);
  });
  if (!evs.length) el.textContent = 'nothing yet';
}

// ── Douglas ──────────────────────────────────────────────────────────────

var VOICE_SUB = { idle: 'READY', listening: 'LISTENING…', thinking: 'THINKING…',
                  speaking: 'SPEAKING…' };
var pressed = false;
var lastDgl = null;              // the frame on screen; null when node1 gave none
var polled = false;              // has a poll come back yet, with a frame or without?
var misses = 0;                  // polls in a row that got no answer
var late = false;                // a poll has been out for seconds: the link is slow
var asking = false;              // 'photo' or 'text' while a question is on its way
var askedAt = 0, askTimer = 0;
var shot = null;                 // { b64, url }: the photo going with the next question

function paintDouglasHead(dgl) {
  var m = dgl.model || {};
  var pill = $('dgl-pill');
  pill.textContent = dgl.up ? 'ONLINE' : 'OFFLINE';
  pill.className = 'pill ' + (dgl.up ? 'ok' : 'bad');
  var sub = $('dgl-model');
  sub.textContent = !dgl.up ? 'unreachable at ' + (dgl.host || 'douglas')
    : m.state === 'running' ? (m.name || 'model serving')
    : m.state === 'loading' ? 'loading model…' : 'model server down';
  sub.className = 'sub' + (dgl.up && m.state === 'running' ? ' ok'
    : dgl.up && m.state === 'loading' ? ' busy' : '');
  // The chat UI is Douglas's own page, not ours: link straight to it. It is
  // only offered when its door is actually open.
  var chat = $('dgl-chat');
  chat.hidden = !(dgl.up && dgl.chat);
  if (!chat.hidden) chat.href = 'http://' + (dgl.host || 'douglas') + ':8080/';
}

function gb(b) { return b == null ? null : b / 1073741824; }
function num(v, dp) { return v == null || !isFinite(v) ? null : Number(v).toFixed(dp); }

// The ask row and the talk button: what can be done right now and, when the
// answer is nothing, why. A greyed button that does not say why reads as a
// page that has hung, so every reason SEND is unavailable is put into words
// on the line under it - which stays readable when the box has text in it
// and its placeholder cannot be seen.
function paintAsk() {
  var d = lastDgl || {};
  var agent = !!d.agent, voice = d.voice || {};
  var vstate = agent && VOICE_SUB[voice.state] ? voice.state : 'absent';
  if (polled) {
    var ptt = $('dgl-ptt');
    ptt.dataset.state = vstate;
    ptt.disabled = vstate === 'absent' && !pressed;
    $('dgl-ptt-s').textContent = !lastDgl ? 'NO LINK' : !agent ? 'NO AGENT'
      : vstate === 'absent' ? 'NO VOICE PIPELINE' : VOICE_SUB[vstate];
    // The camera is offered only when he can actually see.
    $('dgl-cam').hidden = !(agent && voice.eyes);
  }
  // Typing needs node1, the agent, and a pipeline that is not mid-turn.
  // Before the first poll is back nothing is known, and nothing is claimed.
  var why = !polled ? ''
    : !lastDgl ? 'node1 is not answering'
    : !agent ? 'Douglas is unreachable'
    : vstate === 'absent' ? 'no voice pipeline on Douglas'
    : vstate === 'thinking' ? 'Douglas is thinking…'
    : vstate === 'listening' ? 'Douglas is listening…' : '';
  var sending = asking === 'photo' ? 'Sending the photo…' : 'Sending…';
  $('dgl-ask-go').disabled = !!asking || !!why;
  $('dgl-ask-text').placeholder = asking ? sending
    : why || (shot ? 'Ask about the photo…' : 'Ask Douglas…');
  var note = $('dgl-note');
  if (note.dataset.sticky === '1') return;      // a failure is still being read
  var secs = Math.round((Date.now() - askedAt) / 1000);
  note.className = 'note';
  note.textContent = asking ? sending + (secs >= 3 ? ' ' + secs + ' s' : '')
    : !agent && d.why ? 'Douglas is unreachable: ' + d.why
    : why || (misses || late ? 'node1 is slow to answer' : '');
}

function paintDouglas(d) {
  lastDgl = d || null;
  paintAsk();
  d = d || {};
  var agent = !!d.agent;

  var host = d.host || {}, gpu = d.gpu || {}, llm = d.llm || {};
  var gUsed = (gpu.vram_used_bytes || 0) + (gpu.gtt_used_bytes || 0);
  var gTot = (gpu.vram_total_bytes || 0) + (gpu.gtt_total_bytes || 0);
  var meters = !agent ? [] : [
    ['GPU', num(gpu.busy_pct, 0), '%'],
    ['MEMORY', gTot ? num(gb(gUsed), 0) : null, gTot ? '/' + Math.round(gb(gTot)) + 'G' : 'G'],
    ['TOK/S', num(llm.gen_tps, 1), ''],
    ['CPU', num(host.cpu_pct, 0), '%'],
    ['TEMP', num(host.temp_c, 0), '°C'],
    ['POWER', num(host.power_w, 0), 'W'],
  ];
  var box = $('dgl-meters');
  box.innerHTML = '';
  meters.forEach(function (r) {
    var m = document.createElement('div');
    m.className = 'm';
    m.innerHTML = '<div class="k"></div><div class="v"></div>';
    m.querySelector('.k').textContent = r[0];
    var v = m.querySelector('.v');
    v.classList.toggle('stale', r[1] == null);
    v.textContent = r[1] == null ? '—' : r[1];
    var u = document.createElement('small');
    u.textContent = r[2];
    v.appendChild(u);
    box.appendChild(m);
  });

  // The last few turns only: the full conversation is Douglas's chat page.
  var log = $('dgl-log');
  log.innerHTML = '';
  (agent ? (d.transcript || []).slice(-6) : []).forEach(function (t) {
    var me = t.who === 'user';
    var div = document.createElement('div');
    div.className = 'turn ' + (me ? 'user' : 'douglas');
    div.innerHTML = '<span class="who"><b></b> <span></span></span><span class="txt"></span>';
    div.querySelector('b').textContent = me ? 'YOU' : 'DOUGLAS';
    div.querySelector('.who span').textContent = '· ' + (t.via || 'voice') +
      (t.ts ? ' · ' + fmtClock(t.ts) : '');
    div.querySelector('.txt').textContent = t.text || '';
    log.appendChild(div);
  });
}

// One missed poll is the link, not Douglas: the frame on screen stays through
// it, with a word that node1 is slow. Three in a row and that frame is too
// old to stand behind.
function gotDouglas(d) {
  polled = true;
  late = false;
  misses = d ? 0 : misses + 1;
  if (d || misses >= 3 || !lastDgl) paintDouglas(d);
  else paintAsk();
  repace();
}

// A failure the person has to see: red, and held for a few seconds so the
// next poll does not wipe it before it has been read.
var noteTimer = 0;
function dglNote(msg, ms) {
  var note = $('dgl-note');
  note.className = 'note bad';
  note.textContent = msg;
  note.dataset.sticky = '1';
  clearTimeout(noteTimer);
  noteTimer = setTimeout(function () { note.dataset.sticky = ''; paintAsk(); }, ms || 5000);
}

// ── a photo for Douglas ─────────────────────────────────────────────────
// The phone's camera or library is his eye. The photo is scaled down here,
// on the phone: a 12-megapixel original is megabytes he has no use for, and
// redrawing it also turns whatever the phone shot (HEIC included) into JPEG.

var SHOT_EDGE = 1280;            // longest side sent, in pixels

function shrink(file) {
  return new Promise(function (resolve, reject) {
    var url = URL.createObjectURL(file), img = new Image();
    img.onload = function () {
      var k = Math.min(1, SHOT_EDGE / Math.max(img.naturalWidth, img.naturalHeight));
      var c = document.createElement('canvas');
      c.width = Math.max(1, Math.round(img.naturalWidth * k));
      c.height = Math.max(1, Math.round(img.naturalHeight * k));
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(url);
      c.toBlob(function (b) { if (b) resolve(b); else reject(new Error('encode')); },
               'image/jpeg', 0.85);
    };
    img.onerror = function () { URL.revokeObjectURL(url); reject(new Error('decode')); };
    img.src = url;
  });
}

function clearShot() {
  if (shot) URL.revokeObjectURL(shot.url);
  shot = null;
  $('dgl-shot').hidden = true;
  $('dgl-photo').value = '';
  paintAsk();                                  // the placeholder follows
}

$('dgl-cam').addEventListener('click', function () { $('dgl-photo').click(); });
$('dgl-shot-x').addEventListener('click', clearShot);
$('dgl-photo').addEventListener('change', function () {
  var f = this.files && this.files[0];
  if (!f) return;
  shrink(f).then(function (blob) {
    var rd = new FileReader();
    rd.onload = function () {
      clearShot();
      shot = { b64: String(rd.result).split(',')[1], url: URL.createObjectURL(blob) };
      $('dgl-shot-img').src = shot.url;
      $('dgl-shot').hidden = false;
      paintAsk();
    };
    rd.onerror = function () { dglNote('could not read that photo'); };
    rd.readAsDataURL(blob);
  }).catch(function () { dglNote('could not open that photo'); });
});

$('dgl-ask').addEventListener('submit', function (e) {
  e.preventDefault();
  var box = $('dgl-ask-text');
  var text = box.value.trim();
  if ((!text && !shot) || asking) return;     // a photo alone asks "what is this?"
  var body = { text: text, via: 'phone' };
  if (shot) body.image = shot.b64;
  asking = shot ? 'photo' : 'text';
  askedAt = Date.now();
  clearTimeout(noteTimer);
  $('dgl-note').dataset.sticky = '';          // a new try: the last failure has been read
  // The line under the box counts the seconds while this is out. A photo over
  // a slow link takes a while, and a button that only goes grey looks hung.
  askTimer = setInterval(paintAsk, 1000);
  paintAsk();
  // A photo gets longer: it is a few hundred kilobytes to push through
  // whatever link the phone has, and deck-api then gives Douglas twenty
  // seconds to take it.
  post('/api/douglas/say', body, shot ? 45000 : 12000).then(function (r) {
    asking = false;
    clearInterval(askTimer);
    if (r && r.ok) {
      box.value = '';
      clearShot();
      box.blur();                    // drop the keyboard so the reply is visible
    } else {
      // Nothing is thrown away: the words and the photo stay for another try.
      dglNote(r ? (r.message || 'Douglas refused')
        : 'No answer from node1: the link from this phone is slow or down. Try again.', 8000);
    }
    paintAsk();
    tickDouglas(true);               // shows our line now; 'thinking' speeds the poll
  });
});

function pttSend(action) {
  post('/api/douglas/ptt', { action: action }, 8000).then(function (r) {
    if (!(r && r.ok)) dglNote(r ? (r.message || 'Douglas refused') : 'No answer from node1');
  });
}

(function wirePtt() {
  var ptt = $('dgl-ptt');
  ptt.addEventListener('pointerdown', function (e) {
    if (ptt.disabled || pressed) return;
    e.preventDefault();
    try { ptt.setPointerCapture(e.pointerId); } catch (err) { /* older browsers */ }
    pressed = true;
    ptt.classList.add('pressed');
    if (navigator.vibrate) navigator.vibrate(15);
    pttSend('start');
    repace();
  });
  function release(action) {
    return function () {
      if (!pressed) return;
      pressed = false;
      ptt.classList.remove('pressed');
      pttSend(action);
    };
  }
  ptt.addEventListener('pointerup', release('stop'));
  ptt.addEventListener('pointercancel', release('cancel'));
  ptt.addEventListener('contextmenu', function (e) { e.preventDefault(); });
})();

// ── now playing ──────────────────────────────────────────────────────────

var artId = null;

function paintNp(m) {
  m = m || {};
  var np = $('np');
  np.classList.toggle('idle', !m.active);
  $('np-title').textContent = m.active ? (m.title || '(untitled)') : 'nothing playing';
  $('np-artist').textContent = m.active ? (m.artist || '') : '';
  $('np-time').textContent = !m.active ? ''
    : (m.playing ? '▶ ' : '❚❚ ') + fmtTime(m.position_s) +
      (m.duration_s ? ' / ' + fmtTime(m.duration_s) : '');
  var img = $('np-art');
  if (m.active && m.art_id && m.art_id !== artId) {
    artId = m.art_id;
    img.onload = function () { img.hidden = false; };
    img.onerror = function () { img.hidden = true; };
    img.src = api('/api/media/art?v=' + encodeURIComponent(m.art_id));
  } else if (!m.active || !m.art_id) {
    artId = null;
    img.hidden = true;
  }
  var can = m.can || {};
  var btns = np.querySelectorAll('.np-btns button');
  btns[0].disabled = !can.prev;
  btns[1].disabled = !can.play_pause;
  btns[2].disabled = !can.next;
}

[].forEach.call(document.querySelectorAll('.np-btns button'), function (b) {
  b.addEventListener('click', function () {
    post('/api/media/command', { action: b.dataset.a }).then(function () {
      setTimeout(tickNp, 350);
    });
  });
});

// ── polls ────────────────────────────────────────────────────────────────

var timers = {};
function every(name, ms, fn) {
  if (timers[name] && timers[name].ms === ms) return;
  if (timers[name]) clearInterval(timers[name].id);
  fn();
  timers[name] = { ms: ms, id: setInterval(fn, ms) };
}

// One request per poll at a time. /api/media can take seconds when the
// workstation is slow to answer, and a slow link stretches everything; fired
// every three seconds regardless, those calls pile up on the handful of
// connections a phone opens to node1, and the one request that matters, a
// question for Douglas, queues behind them. `fresh` asks for an answer
// fetched after this moment, so a poll already under way is followed by one
// more.
var flying = {}, again = {}, epoch = 0;
function poll(name, path, paint, fresh) {
  if (flying[name]) {
    if (fresh) again[name] = true;
    return Date.now() - flying[name];           // how long that one has been out
  }
  var mine = epoch;
  flying[name] = Date.now();
  getJSON(path).then(function (d) {
    if (mine !== epoch) return;                 // the page slept meanwhile: stale
    flying[name] = 0;
    paint(d);
    if (again[name]) { again[name] = false; poll(name, path, paint); }
  });
  return 0;
}
function tickDouglas(fresh) {
  var out = poll('dgl', '/api/douglas', gotDouglas, fresh === true);
  // Douglas's status normally takes a tenth of a second. Still waiting on it
  // seconds later is a slow link, and worth saying before a deadline makes it
  // official.
  if (out >= 2500 && !late) { late = true; paintAsk(); }
}
function tickNp() { poll('np', '/api/media', paintNp); }

function busy() {
  var st = lastDgl && lastDgl.voice && lastDgl.voice.state;
  return pressed || st === 'listening' || st === 'thinking' || st === 'speaking';
}
function repace() { if (!document.hidden) every('dgl', busy() ? 700 : 3000, tickDouglas); }

function pollAll(on) {
  if (!on) {
    Object.keys(timers).forEach(function (k) { clearInterval(timers[k].id); });
    timers = {};
    // Whatever is in flight belongs to a page about to sleep. Its answer, if
    // one ever comes, must not be painted over a fresher one, nor hold up the
    // first poll when the page wakes.
    epoch++;
    flying = {};
    again = {};
    return;
  }
  every('dgl', 3000, tickDouglas);
  every('np', 3000, tickNp);
}

// ── boot controls ────────────────────────────────────────────────────────

function wsNote(msg) {
  var n = $('ws-note');
  n.className = 'note bad';
  n.textContent = msg;
  setTimeout(function () { if (n.textContent === msg) n.textContent = ''; }, 6000);
}

[].forEach.call(document.querySelectorAll('.tile'), function (t) {
  wireHold(t, function () {
    if (navigator.vibrate) navigator.vibrate(30);
    post('/api/boot', { intent: t.dataset.intent }).then(function (r) {
      if (!r) wsNote('deck-api did not answer');
      else if (!r.ok && r.message) wsNote(r.message);
    });
  });
});
wireHold($('abort'), function () { post('/api/abort', {}); });
wireHold($('shutdown'), function () {
  post('/api/shutdown', {}).then(function (r) {
    if (!r) wsNote('deck-api did not answer');
    else if (!r.ok && r.message) wsNote(r.message);
  });
});

// ── go ───────────────────────────────────────────────────────────────────

setInterval(function () { $('clock').textContent = fmtClock(); }, 1000);
$('clock').textContent = fmtClock();
connect();
pollAll(true);
