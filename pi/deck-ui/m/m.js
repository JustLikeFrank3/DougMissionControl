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
import { memoryView, didView } from '../js/memory.js';
import { machineLine } from '../js/fleet.js';
import { noticesView } from '../js/notices.js';
import { approvalView, switchesView } from '../js/owner.js';

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

// Added to the Home Screen, the page runs as an app of its own. On an iPhone
// that app shares no storage with Safari, so it starts without the token
// Safari has had all along.
function inApp() {
  return navigator.standalone === true ||
    !!(window.matchMedia && window.matchMedia('(display-mode: standalone)').matches);
}

var gateTried = false;           // a token was just saved: a refusal now is about that token

function showGate() {
  $('gate').hidden = false;
  $('main').hidden = true;
  $('gate-app').hidden = !inApp();
  if (gateTried) {
    $('gate-note').textContent = 'node1 did not accept that. Paste the token again, or the whole link.';
  }
  setConn('no token', 'dead');
}

$('gate-form').addEventListener('submit', function (e) {
  e.preventDefault();
  var v = $('gate-token').value.trim();
  // The whole link from pi/mobile-link.sh will do as well as the token in it.
  var inLink = /[?&]token=([^&#\s]+)/.exec(v);
  if (inLink) {
    try { v = decodeURIComponent(inLink[1]); } catch (err) { v = inLink[1]; }
  }
  if (!v) return;
  $('gate-token').value = '';
  $('gate-note').textContent = '';
  gateTried = true;
  try { localStorage.setItem(KEY, v); } catch (err) { /* session only */ }
  token = v;
  $('gate').hidden = true;
  $('main').hidden = false;
  connect();
});

// ── handing the token on ─────────────────────────────────────────────────
// The other browser on this phone, the Home Screen app, has to be given the
// token once, and nobody should have to type thirty-two characters. This
// puts it on the clipboard. The page is plain HTTP on a tailnet name, where
// the modern clipboard is not offered, so the old way is the usual one; and
// if that is refused too, the token is shown, selected, to be copied by hand.

var handoffTimer = 0;
function handoffDone(ok) {
  var box = $('handoff-text'), note = $('handoff-note');
  clearTimeout(handoffTimer);
  if (ok) {
    box.hidden = true;
    box.value = '';
    note.textContent = 'Copied. Open the Home Screen app, paste it into the box and tap Save.';
  } else {
    note.textContent = 'This browser would not copy it. Hold the text above, choose Copy, ' +
                       'then paste it in the Home Screen app.';
    handoffTimer = setTimeout(function () {       // not left on screen for long
      box.hidden = true;
      box.value = '';
      note.textContent = 'for the Home Screen app, which asks for it once';
    }, 60000);
  }
}

$('handoff-go').addEventListener('click', function () {
  var box = $('handoff-text');
  if (!token) return;
  box.hidden = false;
  box.value = token;
  box.focus();
  box.select();
  box.setSelectionRange(0, token.length);
  var ok = false;
  try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
  if (ok || !(navigator.clipboard && navigator.clipboard.writeText)) return handoffDone(ok);
  navigator.clipboard.writeText(token).then(function () { handoffDone(true); },
                                            function () { handoffDone(false); });
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
    gateTried = false;
    // Offered where there is a token to hand on and somewhere else to take it.
    $('handoff').hidden = !token || inApp();
    // The same page on HTTPS, where the phone lends its microphone. Offered
    // from the plain page only, with the token in the link: it is this
    // phone's own, going to the same node1.
    var https = $('dgl-https');
    https.hidden = canRecord || !s.https_url || location.protocol === 'https:';
    if (!https.hidden) {
      https.href = s.https_url + '/m' + (token ? '?token=' + encodeURIComponent(token) : '');
    }
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

// What each machine reports about itself, from /api/fleet: the same reading
// Douglas is given. Kept beside the frame, which says whether a machine is
// up; a reading that has stopped arriving is dropped, not left on screen.
var fleetView = null;
function gotFleet(f) {
  fleetView = f && f.machines ? f : null;
  if (state) paintFleet(state);
}
function tickFleet() { poll('fleet', '/api/fleet', gotFleet); }

function paintFleet(s) {
  var ws = s.workstation || {}, mini = s.mini || {}, mob = s.mobile || {};
  var dgl = s.douglas || {}, voice = s.voice || {};
  var dm = dgl.model || {};
  var rows = [
    ['DOUG DUALBOOT', ws.os === 'windows' || ws.os === 'linux' ? ws.os
      : ws.os === 'booting' ? 'booting' : 'off', ws.os === 'windows' || ws.os === 'linux', false,
     'workstation'],
    ['DOUG PRIME', 'up · drawing the panel', true, false, 'node1'],
    ['MINIDOUG4', mini.up ? 'up' : 'off · seen ' + fmtAgo(mini.last_alive), !!mini.up, false, 'mini'],
    ['DOUGMOBILE', mob.up ? 'up' : 'away', !!mob.up, false, 'mobile'],
    ['DOUGLAS', !dgl.up ? 'offline · seen ' + fmtAgo(dgl.last_alive)
      : dm.state === 'running' ? (dm.name || 'serving')
      : dm.state === 'loading' ? 'loading model' : 'model server down',
     !!dgl.up, !dgl.up, 'douglas'],
    ['DOUGVOICE', voice.up ? (voice.satellite ? 'listening' : 'up · satellite down') : 'off',
     !!voice.up, false, 'dougvoice'],
  ];
  var el = $('fleet'), now = Date.now() / 1000;
  el.innerHTML = '';
  rows.forEach(function (r) {
    var d = document.createElement('div');
    d.className = 'host' + (r[2] ? ' up' : '') + (r[3] ? ' bad' : '');
    d.innerHTML = '<i></i><b></b><span></span>';
    d.querySelector('b').textContent = r[0];
    d.querySelector('span').textContent = r[1];
    var m = fleetView && fleetView.machines.filter(function (x) { return x.id === r[4]; })[0];
    var line = r[2] ? machineLine(m, now) : { text: '' };
    if (line.text) {
      var more = document.createElement('em');
      more.textContent = line.text;
      if (line.bad) more.className = 'bad';
      d.appendChild(more);
    }
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
var ask = null;                  // the question on its way: { body, kind, since, flying, last }
var askTimer = 0;
var eyes = false;                // has Douglas said he can see? kept through a lost link
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

function clock(ms) {
  var s = Math.max(0, Math.floor(ms / 1000));
  return Math.floor(s / 60) + ':' + ('0' + s % 60).slice(-2);
}

// Can a question go to Douglas this very moment? Before the first poll is
// back nothing is known, so it is worth a try.
function ready() {
  var d = lastDgl || {}, st = (d.voice || {}).state;
  return !polled || (!!d.agent && !misses && !late && (st === 'idle' || st === 'speaking'));
}

// The ask row and the talk button: what can be done right now and, when
// something is in the way, what. SEND is never greyed out: a question that
// cannot go yet waits for the moment it can, and the line under the box says
// what it is waiting for. That line stays readable when the box has text in
// it and its placeholder cannot be seen.
function paintAsk() {
  var d = lastDgl || {};
  var agent = !!d.agent, voice = d.voice || {};
  var vstate = agent && VOICE_SUB[voice.state] ? voice.state : 'absent';
  var ptt = $('dgl-ptt');
  if (canRecord) {
    // This phone's own microphone: the button is about what the phone is
    // doing, and is never out of reach. What Douglas is doing is on the
    // line under the box.
    var mine = rec ? (rec.live ? 'listening' : 'idle') : speaking ? 'speaking'
      : (ask && ask.kind === 'voice') || awaiting ? 'thinking' : 'idle';
    ptt.dataset.state = mine;
    ptt.disabled = false;
    $('dgl-ptt-s').textContent = rec ? (rec.live ? 'LISTENING…' : 'STARTING…')
      : speaking ? 'SPEAKING…' : mine === 'thinking' ? 'THINKING…' : 'READY';
    $('dgl-ptt-where').textContent = 'this phone\'s microphone and speaker';
  } else if (polled) {
    ptt.dataset.state = vstate;
    ptt.disabled = vstate === 'absent' && !pressed;
    $('dgl-ptt-s').textContent = !lastDgl ? 'NO LINK' : !agent ? 'NO AGENT'
      : vstate === 'absent' ? 'NO VOICE PIPELINE' : VOICE_SUB[vstate];
  }
  if (polled) {
    // The camera is offered once he has said he can see, and stays through a
    // lost link: a photo can be taken now and go when node1 is back.
    if (agent) {
      eyes = !!voice.eyes;
      shotEdge = Math.min(4096, Math.max(SHOT_EDGE, +voice.shot || 0));
    }
    $('dgl-cam').hidden = !eyes;
  }
  // What stands between a question and Douglas, if anything. Before the
  // first poll is back nothing is known, and nothing is claimed.
  var link = polled && (!lastDgl || misses || late);
  var why = !polled ? ''
    : !lastDgl ? 'node1 is not answering'
    : !agent ? 'Douglas is unreachable'
    : vstate === 'absent' ? 'no voice pipeline on Douglas'
    : vstate === 'thinking' ? 'Douglas is thinking…'
    : vstate === 'listening' ? 'Douglas is listening…' : '';
  var box = $('dgl-ask-text'), go = $('dgl-ask-go');
  var sending = !ask ? '' : ask.kind === 'photo' ? 'Sending the photo…'
    : ask.kind === 'voice' ? 'Sending what you said…' : 'Sending…';
  // While a question is on its way the button stops it, and what is going
  // cannot be edited from under it.
  go.textContent = ask ? 'STOP' : 'SEND';
  box.disabled = !!ask;
  $('dgl-cam').disabled = !!ask;
  $('dgl-shot-x').hidden = !!ask;
  box.placeholder = ask ? (ask.flying ? sending : 'Waiting to send…')
    : why || (shot ? 'Ask about the photo…' : 'Ask Douglas…');
  var note = $('dgl-note');
  if (note.dataset.sticky === '1') return;      // a failure is still being read
  note.className = 'note';
  if (rec) {
    note.textContent = rec.live
      ? 'Listening through this phone. Let go when you have finished. ' +
        clock(Date.now() - rec.started)
      : 'Starting the microphone…';
  } else if (ask && ask.flying) {
    var secs = Math.round((Date.now() - ask.last) / 1000);
    note.textContent = sending + (secs >= 3 ? ' ' + secs + ' s' : '');
  } else if (ask) {
    var waited = clock(Date.now() - ask.since);
    note.textContent = vstate === 'thinking' || vstate === 'listening'
      ? 'Douglas is busy. Your question goes next. ' + waited
      : link ? 'No answer from node1 yet. Your question will go by itself when the link ' +
               'is back: keep this page open. ' + waited
      : why ? why + '. Your question will go by itself when that clears: keep this page ' +
              'open. ' + waited
      : sending;
  } else {
    // What DougVoice made of the last press, while it is still news. A
    // press that gets no answer is otherwise indistinguishable from a
    // Douglas who has stopped working.
    var heard = voice.heard, deaf = '';
    if (speaking) {
      deaf = 'Douglas is speaking. Hold the button to cut in.';
    } else if (awaiting && !why) {
      deaf = 'Fetching his reply…';
    } else if (!canRecord && heard && heard.words === 0 && d.ts - heard.ts < 20) {
      deaf = 'DougVoice heard nothing in ' + heard.seconds + ' s' +
        (heard.level_db < -45 ? ': the room around it was quiet' : '') +
        '. It listens through its own microphone, not this phone\'s.';
    }
    note.textContent = !lastDgl && polled
        ? 'node1 is not answering. SEND still works: your question waits until the link is back.'
      : !agent && d.why ? 'Douglas is unreachable: ' + d.why
      : why || deaf || (misses || late ? 'node1 is slow to answer' : '');
  }
}

function paintDouglas(d) {
  lastDgl = d || null;
  paintAsk();
  d = d || {};
  var agent = !!d.agent;
  // What is waiting for a yes, and what he may use: both from his own state.
  var tools = agent && d.voice ? d.voice.tools : null;
  approval.paint(agent && d.voice ? d.voice.pending : null);
  switches.paint(tools);
  var off = tools ? Object.keys(tools).filter(function (k) { return tools[k] === 'off'; }).length : -1;
  $('sw-count').textContent = off < 0 ? '—' : off ? off + ' OFF' : 'ALL ON';
  $('sw-count').className = 'pill' + (off > 0 ? ' busy' : off === 0 ? ' ok' : '');

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
  var log = $('dgl-log'), shown = {};
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
    if (!me && t.image) {
      shown[t.image] = true;
      div.appendChild(picture(t.image));
    }
    log.appendChild(div);
  });
  Object.keys(pics).forEach(function (id) { if (!shown[id]) delete pics[id]; });
}

// A picture Douglas drew comes as an id on his line. The log is rebuilt on
// every poll, so the element is kept and moved into the new log, not made
// again: made again it would be fetched again, a few hundred kilobytes each
// time, over a link that is sometimes a relay. One that did not load is tried
// again after a quarter of a minute. A tap fills the screen with it; another
// puts it back.
var pics = {};
function picture(id) {
  var p = pics[id];
  if (p && (!p.failed || Date.now() - p.failed < 15000)) return p.el;
  var img = new Image();
  p = pics[id] = { el: img, failed: 0 };
  img.className = 'pic';
  img.alt = 'a picture Douglas drew';
  img.onerror = function () { p.failed = Date.now(); img.alt = 'his picture did not load'; };
  img.onclick = function () { img.classList.toggle('big'); };
  img.src = api('/api/douglas/picture?id=' + encodeURIComponent(id));
  return img;
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
  trySend();                                   // a question may have been waiting for this
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

//
// How far down: to 1280 pixels on its long side, unless Douglas has said he
// can make use of more (voice.shot). He can where he is able to cut a part
// out of a photo and look at it enlarged, and what he cuts it from is what
// arrives: a name on a muffler that takes up a tenth of the frame is gone at
// 1280, and no closer look brings it back.

var SHOT_EDGE = 1280;            // longest side sent, in pixels, unless he asks for more
var shotEdge = SHOT_EDGE;        // what he has asked for, kept through a lost link

function shrink(file) {
  return new Promise(function (resolve, reject) {
    var url = URL.createObjectURL(file), img = new Image();
    img.onload = function () {
      var k = Math.min(1, shotEdge / Math.max(img.naturalWidth, img.naturalHeight));
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

// ── a question on its way ───────────────────────────────────────────────
// The link from a phone to node1 comes and goes: a minute or two at a time
// when the phone changes access point, which is exactly when someone has
// walked off to photograph something. So "no answer" is not the end of a
// question. It waits, and goes by itself on the first poll that says node1
// is back and Douglas is free. It carries an id, so a copy that did arrive,
// with only its reply lost on the way back, is not asked twice.

var ASK_GIVE_UP_MS = 5 * 60 * 1000;

function newId() {
  var a = new Uint8Array(12), s = '';
  if (window.crypto && crypto.getRandomValues) crypto.getRandomValues(a);
  else for (var i = 0; i < a.length; i++) a[i] = Math.floor(Math.random() * 256);
  for (var j = 0; j < a.length; j++) s += ('0' + a[j].toString(16)).slice(-2);
  return 'q-' + s;
}

function endAsk() {
  clearInterval(askTimer);
  ask = null;
}

function trySend() {
  if (!ask || ask.flying) return;
  var now = Date.now();
  if (now - ask.since > ASK_GIVE_UP_MS) {
    endAsk();
    dglNote('That could not be sent in five minutes. Your words and photo are still here: ' +
            'press SEND to try again.', 12000);
    paintAsk();
    return;
  }
  if (!ready() || now - ask.last < 2500) return;
  var mine = ask;
  mine.flying = true;
  mine.last = now;
  // A photo gets longer: it is a few hundred kilobytes (a few megabytes,
  // where Douglas has asked for a bigger one) to push through
  // whatever link the phone has, and deck-api then gives Douglas twenty
  // seconds to take it.
  post(mine.kind === 'voice' ? '/api/douglas/hear' : '/api/douglas/say', mine.body,
       mine.kind === 'text' ? 12000 : 45000).then(function (r) {
    if (ask !== mine) return;                  // stopped while it was out
    mine.flying = false;
    if (r && r.ok) {
      endAsk();
      if (mine.kind === 'voice') {
        awaitSpeech(mine.body.id);             // his reply comes back as sound
      } else {
        $('dgl-ask-text').value = '';
        clearShot();
        $('dgl-ask-text').blur();              // drop the keyboard so the reply is visible
      }
    } else if (r && !r.retry) {
      // Douglas or the deck said no, and asking again will not change that.
      // Nothing is thrown away: the words and the photo stay in place.
      endAsk();
      dglNote(r.message || 'Douglas refused', 8000);
    }
    // Anything else is the link, or Douglas gone for a moment: the question
    // stays where it is and goes again when a poll says it can.
    paintAsk();
    tickDouglas(true);                         // shows our line now; 'thinking' speeds the poll
  });
  paintAsk();
}

$('dgl-ask').addEventListener('submit', function (e) {
  e.preventDefault();
  if (ask) {                                   // the button reads STOP
    var out = ask.flying;
    endAsk();
    if (out) dglNote('Stopped. If it had already reached Douglas, his answer will still appear.', 6000);
    paintAsk();
    return;
  }
  var text = $('dgl-ask-text').value.trim();
  if (!text && !shot) return;                  // a photo alone asks "what is this?"
  var body = { text: text, via: 'phone', id: newId() };
  if (shot) body.image = shot.b64;
  queueAsk(body, shot ? 'photo' : 'text');
});

function queueAsk(body, kind) {
  clearTimeout(noteTimer);
  $('dgl-note').dataset.sticky = '';           // a new try: the last failure has been read
  ask = { body: body, kind: kind, since: Date.now(), flying: false, last: 0 };
  // Once a second: count the seconds on the line under the box, and see
  // whether the question can go yet.
  askTimer = setInterval(function () { trySend(); paintAsk(); }, 1000);
  trySend();
  paintAsk();
}

// ── this phone's own microphone and speaker ─────────────────────────────
// On an HTTPS page the browser lends the microphone, and then HOLD TO TALK
// means what it says on a phone: speak into it, and hear him from it. The
// recording goes whole, as 16 kHz PCM, through the same queue as a typed
// question; his reply comes back as a WAV and is played here, not in the
// house. On plain HTTP the browser lends nothing, and the button borrows
// DougVoice's ears instead.

var AudioCtx = window.AudioContext || window.webkitAudioContext;
var canRecord = !!(window.isSecureContext && AudioCtx && navigator.mediaDevices &&
                   navigator.mediaDevices.getUserMedia);
var REC_RATE = 16000, REC_MAX_MS = 20000;
var rec = null;                  // the recording under way: { ctx, chunks, rate, live, ... }
var awaiting = null;             // a recorded turn whose reply has not come back as sound yet
var speaking = null;             // the reply playing now
var speechTimer = 0;

// A phone that has just lent a page its microphone treats the page's sound
// as a telephone call: out of the earpiece, at call volume. Two things keep
// his reply out of there. The recording gets an audio context of its own,
// closed the moment the button is let go, so that nothing of the call is
// left open. And the reply is played by an ordinary media element with the
// page's audio session set to playback, which is the loudspeaker.
function audioSession(type) {
  try {
    if (navigator.audioSession) navigator.audioSession.type = type;
  } catch (e) { /* an older phone: it decides for itself */ }
}

// A phone only lets a page make sound that a touch started. The player is
// made, and played once on silence, inside the press: after that it may be
// started from code, which is how his reply gets to play later.
var player = null, playerReady = false;

function silence() {
  var n = 800, wav = new DataView(new ArrayBuffer(44 + n * 2));      // a tenth of a second
  function text(at, t) { for (var i = 0; i < t.length; i++) wav.setUint8(at + i, t.charCodeAt(i)); }
  text(0, 'RIFF'); wav.setUint32(4, 36 + n * 2, true); text(8, 'WAVEfmt ');
  wav.setUint32(16, 16, true); wav.setUint16(20, 1, true); wav.setUint16(22, 1, true);
  wav.setUint32(24, 8000, true); wav.setUint32(28, 16000, true);
  wav.setUint16(32, 2, true); wav.setUint16(34, 16, true);
  text(36, 'data'); wav.setUint32(40, n * 2, true);
  return URL.createObjectURL(new Blob([wav], { type: 'audio/wav' }));
}

function readyPlayer() {
  if (!player) {
    player = new Audio();
    player.preload = 'auto';
    player.setAttribute('playsinline', '');
  }
  if (playerReady) return;
  player.src = silence();
  var started = player.play();
  if (started && started.then) {
    started.then(function () { playerReady = true; }, function () { /* the next press tries again */ });
  } else {
    playerReady = true;
  }
}

function stopTracks(stream) {
  stream.getTracks().forEach(function (t) { t.stop(); });
}

function startRec() {
  stopSpeaking();
  readyPlayer();
  audioSession('play-and-record');
  var ctx = new AudioCtx();
  if (ctx.state === 'suspended' && ctx.resume) ctx.resume();
  var mine = rec = { ctx: ctx, chunks: [], rate: ctx.sampleRate, live: false, started: 0, timer: 0 };
  navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  }).then(function (stream) {
    if (rec !== mine) {                                    // let go before it came
      stopTracks(stream);
      audioSession('playback');
      return;
    }
    mine.stream = stream;
    mine.source = ctx.createMediaStreamSource(stream);
    mine.node = ctx.createScriptProcessor(4096, 1, 1);
    mine.node.onaudioprocess = function (e) {
      mine.chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
    };
    // It has to lead somewhere to run at all, and that must not be the speaker.
    mine.sink = ctx.createGain();
    mine.sink.gain.value = 0;
    mine.source.connect(mine.node);
    mine.node.connect(mine.sink);
    mine.sink.connect(ctx.destination);
    mine.live = true;
    mine.started = Date.now();
    mine.timer = setTimeout(function () { if (rec === mine) endRec(true); }, REC_MAX_MS);
    paintAsk();
  }, function (err) {
    if (rec === mine) rec = null;
    if (ctx.close) ctx.close();
    audioSession('playback');
    dglNote(err && (err.name === 'NotAllowedError' || err.name === 'SecurityError')
      ? 'This page is not allowed the microphone. Allow it for this site in the ' +
        'browser\'s settings, then hold the button again.'
      : 'The microphone would not start on this phone.', 9000);
    paintAsk();
  });
  paintAsk();
}

function endRec(send) {
  var r = rec;
  rec = null;
  if (!r) return;
  clearTimeout(r.timer);
  if (r.node) {
    r.node.onaudioprocess = null;
    try { r.source.disconnect(); r.node.disconnect(); r.sink.disconnect(); } catch (e) { /* gone already */ }
  }
  if (r.stream) stopTracks(r.stream);
  // The call is over: the microphone is let go, its audio context closed,
  // and the page's sound is media again, for the loudspeaker.
  if (r.ctx && r.ctx.close) { try { r.ctx.close(); } catch (e) { /* closed already */ } }
  audioSession('playback');
  if (send && r.live) {
    var pcm = toPcm16(r.chunks, r.rate, REC_RATE);
    if (pcm.length < REC_RATE * 0.4) {
      dglNote('That was too short to hear. Hold the button for as long as you are speaking.', 6000);
    } else {
      queueAsk({ audio: toBase64(pcm), rate: REC_RATE, via: 'phone', id: newId() }, 'voice');
    }
  } else if (send) {
    dglNote('The microphone had not started yet. Hold the button, wait for LISTENING, then speak.', 6000);
  }
  paintAsk();
}

// What the microphone gave (floats at the phone's own rate, usually 48 kHz)
// as what speech-to-text wants: 16-bit at 16 kHz. Each output sample is the
// average of the input it spans, which is filter enough for speech.
function toPcm16(chunks, from, to) {
  var n = 0, i, j;
  for (i = 0; i < chunks.length; i++) n += chunks[i].length;
  var all = new Float32Array(n), at = 0;
  for (i = 0; i < chunks.length; i++) { all.set(chunks[i], at); at += chunks[i].length; }
  var step = from / to, count = Math.floor(n / step), out = new Int16Array(count);
  for (i = 0; i < count; i++) {
    var a = Math.floor(i * step), b = Math.min(n, Math.max(a + 1, Math.floor((i + 1) * step)));
    var sum = 0;
    for (j = a; j < b; j++) sum += all[j];
    var v = Math.max(-1, Math.min(1, sum / (b - a)));
    out[i] = v < 0 ? v * 32768 : v * 32767;
  }
  return out;
}

function toBase64(int16) {
  var bytes = new Uint8Array(int16.buffer, int16.byteOffset, int16.byteLength), s = '';
  for (var i = 0; i < bytes.length; i += 0x8000) {
    s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(s);
}

// The recording has been taken: now his reply, as sound. It is asked for
// once a second until it is ready, or until it is plain it never will be.
function awaitSpeech(id) {
  clearInterval(speechTimer);
  awaiting = { id: id, since: Date.now(), flying: false };
  speechTimer = setInterval(fetchSpeech, 1000);
}

function endAwait() {
  clearInterval(speechTimer);
  awaiting = null;
}

function fetchSpeech() {
  var mine = awaiting;
  if (!mine || mine.flying) return;
  if (Date.now() - mine.since > 120000) {
    endAwait();
    dglNote('His reply never came back as sound. If he answered, it is in the log above.', 8000);
    paintAsk();
    return;
  }
  mine.flying = true;
  var ctl = window.AbortController ? new AbortController() : null;
  var timer = ctl ? setTimeout(function () { ctl.abort(); }, 20000) : 0;
  fetch(api('/api/douglas/speech?id=' + encodeURIComponent(mine.id)),
        { cache: 'no-store', signal: ctl ? ctl.signal : undefined }).then(function (r) {
    var sound = (r.headers.get('Content-Type') || '').indexOf('audio/') === 0;
    return sound ? r.arrayBuffer().then(function (b) { return { sound: b }; }) : r.json();
  }).then(function (got) {
    clearTimeout(timer);
    mine.flying = false;
    if (awaiting !== mine) return;             // a new press took over
    if (got && got.sound) {
      endAwait();
      playSpeech(got.sound);
    } else if (got && got.state === 'nothing') {
      endAwait();
      dglNote('Douglas did not catch any words in that. Hold the button and speak ' +
              'towards the phone.', 8000);
    } else if (got && (got.state === 'failed' || got.state === 'unknown')) {
      endAwait();
      dglNote('He answered in the log above, but the sound of it did not come.', 8000);
    }
    paintAsk();
  }, function () {
    clearTimeout(timer);
    mine.flying = false;                       // the link: ask again in a second
  });
}

function playSpeech(buf) {
  stopSpeaking();
  if (!player) {                               // no press has made one: it cannot be started
    dglNote('His reply is in the log above. Hold the button once and the next one will be spoken.', 8000);
    return;
  }
  audioSession('playback');
  var mine = speaking = { url: URL.createObjectURL(new Blob([buf], { type: 'audio/wav' })) };
  function over(failed) {
    if (speaking !== mine) return;
    stopSpeaking();
    if (failed) dglNote('This phone could not play his reply. It is in the log above.', 8000);
    paintAsk();
  }
  player.onended = function () { over(false); };
  player.onerror = function () { over(true); };
  player.src = mine.url;
  var started = player.play();
  if (started && started.catch) started.catch(function () { over(true); });
  paintAsk();
}

function stopSpeaking() {
  if (!speaking) return;
  var was = speaking;
  speaking = null;
  if (player) {
    player.onended = player.onerror = null;
    try { player.pause(); } catch (e) { /* not playing */ }
  }
  URL.revokeObjectURL(was.url);
}

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
    if (canRecord && ask) {                    // one thing on its way at a time
      dglNote('Your last one is still on its way. Press STOP to take it back.', 5000);
      return;
    }
    pressed = true;
    ptt.classList.add('pressed');
    if (navigator.vibrate) navigator.vibrate(15);
    if (canRecord) { endAwait(); startRec(); } else pttSend('start');
    repace();
  });
  function release(action) {
    return function () {
      if (!pressed) return;
      pressed = false;
      ptt.classList.remove('pressed');
      if (canRecord) endRec(action === 'stop'); else pttSend(action);
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

// ── what he remembers ────────────────────────────────────────────────────
// Closed, the card asks only how much there is. Open, it reads the list and
// the record of what he did, and looks again now and then: a conversation
// that has gone quiet turns into memories a minute or so later.
var OPEN_KEY = 'dmc-mem-open';
var memOpen = (function () {
  try { return localStorage.getItem(OPEN_KEY) === '1'; } catch (e) { return false; }
})();
var memory = memoryView($('mem'), { get: getJSON, post: post, count: $('mem-count') });
var did = didView($('did'), { get: getJSON, rows: 12 });
function tickMem() {
  if (!memOpen) { memory.glance(); return; }
  memory.refresh();
  did.refresh();
}
// What the watch raised. Asked for more often than the memory: a notice is
// the one thing here that is news.
var notices = noticesView($('ntc'), { get: getJSON, post: post, count: $('ntc-count') });
function tickNotices() { notices.refresh(); }

var approval = approvalView($('dgl-apv'), { post: post, via: 'phone' });
var switches = switchesView($('sw'), { post: post });
$('sw-head').addEventListener('click', function () {
  var open = $('sw-body').hidden;
  $('sw-body').hidden = !open;
  $('sw-head').setAttribute('aria-expanded', open ? 'true' : 'false');
});

function paintMemOpen() {
  $('mem-body').hidden = !memOpen;
  $('mem-head').setAttribute('aria-expanded', memOpen ? 'true' : 'false');
}
$('mem-head').addEventListener('click', function () {
  memOpen = !memOpen;
  try { localStorage.setItem(OPEN_KEY, memOpen ? '1' : '0'); } catch (e) { /* private mode */ }
  paintMemOpen();
  tickMem();
});
paintMemOpen();

function busy() {
  var st = lastDgl && lastDgl.voice && lastDgl.voice.state;
  // A question waiting for the link is the best reason there is to look often.
  return pressed || !!ask || !!awaiting || st === 'listening' || st === 'thinking' || st === 'speaking';
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
  every('mem', 30000, tickMem);
  every('fleet', 15000, tickFleet);
  every('ntc', 15000, tickNotices);
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
