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

function getJSON(path) {
  return fetch(api(path), { cache: 'no-store' }).then(function (r) {
    if (r.status === 403) { showGate(); return null; }
    return r.json();
  }).catch(function () { return null; });
}

function post(path, body) {
  return fetch(api(path), {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  }).then(function (r) {
    if (r.status === 403) { showGate(); return null; }
    return r.json();
  }).catch(function () { return null; });
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

var es = null, state = null;

function setConn(text, cls) {
  var c = $('conn');
  c.textContent = text;
  c.className = 'conn' + (cls ? ' ' + cls : '');
}

function connect() {
  if (es) es.close();
  // A probe first: EventSource cannot see a 403, it just retries forever,
  // which would read as "reconnecting" to a phone that is simply not allowed.
  getJSON('/api/state').then(function (s) {
    if (!s) { if ($('gate').hidden) setConn('node1 unreachable', 'dead'); return; }
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
  if (document.hidden) { if (es) { es.close(); es = null; } pollAll(false); }
  else { connect(); pollAll(true); }
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
var pressed = false, lastDgl = null, asking = false;
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

function paintDouglas(d) {
  lastDgl = d;
  d = d || {};
  var agent = !!d.agent, voice = d.voice || {};
  var vstate = agent && VOICE_SUB[voice.state] ? voice.state : 'absent';
  var ptt = $('dgl-ptt');
  ptt.dataset.state = vstate;
  ptt.disabled = vstate === 'absent' && !pressed;
  $('dgl-ptt-s').textContent = !agent ? 'NO AGENT'
    : vstate === 'absent' ? 'NO VOICE PIPELINE' : VOICE_SUB[vstate];
  // Typing needs the agent and a pipeline that is not mid-turn.
  var canAsk = agent && vstate !== 'absent' && vstate !== 'thinking' && vstate !== 'listening';
  $('dgl-ask-go').disabled = !canAsk || asking;
  $('dgl-ask-text').placeholder = !agent ? 'Douglas is unreachable'
    : vstate === 'absent' ? 'no voice pipeline on Douglas'
    : vstate === 'thinking' ? (shot ? 'Douglas is looking…' : 'Douglas is thinking…')
    : shot ? 'Ask about the photo…' : 'Ask Douglas…';
  // The camera is offered only when he can actually see.
  $('dgl-cam').hidden = !(agent && voice.eyes);
  var note = $('dgl-note');
  if (note.dataset.sticky !== '1') {
    note.className = 'note';
    note.textContent = agent ? '' : (d.why || '');
  }

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

function dglNote(msg) {
  var note = $('dgl-note');
  note.className = 'note bad';
  note.textContent = msg;
  note.dataset.sticky = '1';
  setTimeout(function () { note.dataset.sticky = ''; }, 5000);
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
  if (lastDgl) paintDouglas(lastDgl);          // the placeholder follows
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
      if (lastDgl) paintDouglas(lastDgl);
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
  asking = true;
  $('dgl-ask-go').disabled = true;
  var body = { text: text, via: 'phone' };
  if (shot) body.image = shot.b64;
  post('/api/douglas/say', body).then(function (r) {
    asking = false;
    if (r && r.ok) {
      box.value = '';
      clearShot();
      box.blur();                    // drop the keyboard so the reply is visible
      tickDouglas();                 // shows our line now; 'thinking' speeds the poll
    } else {
      dglNote(r ? (r.message || 'Douglas refused') : 'deck-api did not answer');
    }
    $('dgl-ask-go').disabled = false;
  });
});

function pttSend(action) {
  post('/api/douglas/ptt', { action: action }).then(function (r) {
    if (r && r.ok) return;
    var note = $('dgl-note');
    note.className = 'note bad';
    note.textContent = r ? (r.message || 'Douglas refused') : 'deck-api did not answer';
    note.dataset.sticky = '1';
    setTimeout(function () { note.dataset.sticky = ''; }, 5000);
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
function tickDouglas() { getJSON('/api/douglas').then(function (d) { paintDouglas(d); repace(); }); }
function tickNp() { getJSON('/api/media').then(paintNp); }

function busy() {
  var st = lastDgl && lastDgl.voice && lastDgl.voice.state;
  return pressed || st === 'listening' || st === 'thinking' || st === 'speaking';
}
function repace() { if (!document.hidden) every('dgl', busy() ? 700 : 3000, tickDouglas); }

function pollAll(on) {
  if (!on) {
    Object.keys(timers).forEach(function (k) { clearInterval(timers[k].id); });
    timers = {};
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
