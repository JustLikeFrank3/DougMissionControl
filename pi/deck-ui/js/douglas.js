/* douglas.js — Douglas, the AI, on the panel.

   Three things live here:
     - the DOUGLAS status line under the DECK boot tiles, painted from the SSE
       frame's fleet probe (s.douglas), which runs whether or not anyone is
       looking;
     - the DOUGLAS surface, painted from /api/douglas (douglas-agent's /status
       passed through), polled only while that surface shows;
     - push-to-talk. The panel has no microphone, so a press asks Douglas to
       have DougVoice listen and a release ends the utterance — the same as
       DougVoice's own button. The button's colour is the voice pipeline's
       OBSERVED state from the next poll, never the press: a press Douglas
       did not hear must not look heard.

   Absent is not zero: with the agent unreachable every meter reads "—", not
   0 %, because an idle Douglas and a missing one are different facts. */

import { $, rows, post, getJSON } from './ui.js';
import { openKeyboard, closeKeyboard, keyboardOpen } from './keyboard.js';
import { fmtAgo } from './format.js';
import { memoryView, didView } from './memory.js';
import { servicesLine, diskPct } from './fleet.js';
import { noticesView, navNotices } from './notices.js';
import { approvalView, switchesView, jobView } from './owner.js';
import { camerasView } from './cameras.js';
import { convoLog, whoLabel } from './convo.js';

var POLL_MS = 2000, POLL_FAST_MS = 500;   // fast while talking: state moves
var timer = null, timerMs = 0, last = null, pressed = false, convo = null;
var MEM_MS = 20000;                       // his memory moves by the minute, not the second
var tab = 'convo', memory = null, did = null, notices = null, memTimer = null;
var approval = null, switches = null, job = null, cameras = null;

/* ── DECK status line + nav subtitle (SSE frame) ──────────────────────── */

export function paintDouglasTile(dgl) {
  dgl = dgl || {};
  var m = dgl.model || {};
  var state, text;
  if (!dgl.up) { state = 'down'; text = 'UNREACHABLE'; }
  else if (m.state === 'running') {
    state = 'running';
    text = (m.name ? m.name.toUpperCase() + ' · ' : '') + 'ONLINE';
  } else if (m.state === 'loading') { state = 'loading'; text = 'LOADING MODEL'; }
  else { state = 'down'; text = 'MODEL SERVER DOWN'; }
  // What the watch raised and nobody has seen yet, wherever the panel is:
  // under his name in the top bar, and on his line here.
  var ntc = navNotices(dgl.notices);
  var bar = $('dgl-tile');
  if (bar) {
    bar.dataset.state = state;
    $('dgl-tile-sub').textContent = text + (ntc ? ' · ' + ntc.text.toUpperCase() : '');
  }
  var nav = $('nav-douglas');
  if (nav) {
    nav.textContent = !dgl.up ? 'offline' : state === 'loading' ? 'loading'
      : state !== 'running' ? 'no model' : ntc ? ntc.text : 'online';
    nav.parentNode.classList.toggle('alert', state !== 'running' || !!ntc);
    nav.parentNode.classList.toggle('grave', !!ntc && ntc.critical);
  }
}

/* ── surface poll ─────────────────────────────────────────────────────── */

export function douglasPoll(on) {
  if (!on && keyboardOpen()) closeKeyboard();     // leaving the surface
  memoryPoll(on);
  var want = !on ? 0 : pressed || isBusy(last) ? POLL_FAST_MS : POLL_MS;
  if (want === timerMs) return;
  if (timer) { clearInterval(timer); timer = null; }
  timerMs = want;
  if (want) { tick(); timer = setInterval(tick, want); }
}

/* What he remembers and what he did are read only while their tab shows;
   the count beside MEMORY is kept while the surface does. */
function tickMemory() {
  if (!memory) return;
  if (tab === 'mem') memory.refresh();
  else memory.glance();
  if (tab === 'did') did.refresh();
  if (tab === 'cam' && cameras) cameras.refresh();
  if (notices) notices.refresh();           // its count shows whichever tab is up
}

function memoryPoll(on) {
  if (!on === !memTimer) return;
  if (memTimer) { clearInterval(memTimer); memTimer = null; }
  if (on) { tickMemory(); memTimer = setInterval(tickMemory, MEM_MS); }
}

function showTab(name) {
  tab = name;
  [].forEach.call($('dgl-tabs').children, function (b) {
    b.classList.toggle('on', b.dataset.tab === name);
  });
  $('dgl-log').hidden = $('dgl-askbar').hidden = name !== 'convo';
  $('dgl-mem').hidden = name !== 'mem';
  $('dgl-ntc').hidden = name !== 'ntc';
  $('dgl-sw').hidden = name !== 'sw';
  $('dgl-cam').hidden = name !== 'cam';
  if (cameras) { if (name === 'cam') cameras.refresh(); else cameras.close(); }
  $('dgl-did').hidden = name !== 'did';
  if (name === 'convo' && convo) convo.toBottom();
  tickMemory();
}

function isBusy(d) {
  var st = d && d.voice && d.voice.state;
  return st === 'listening' || st === 'thinking' || st === 'speaking';
}

function tick() {
  getJSON('/api/douglas').then(function (d) {
    paint(d);
    // Re-pace: speed up while a conversation is moving, settle after.
    if (timerMs) douglasPoll(true);
  });
}

var VOICE_SUB = {
  idle: 'READY',
  listening: 'LISTENING…',
  thinking: 'THINKING…',
  speaking: 'SPEAKING…',
};

function paint(d) {
  last = d;
  d = d || {};
  var agent = !!d.agent;
  var voice = d.voice || {};
  // A change of his that waits for a yes shows whichever tab is up; what he
  // may use has a tab of its own.
  if (approval) approval.paint(agent ? voice.pending : null);
  if (job) job.paint(agent ? voice.job : null);
  if (switches) {
    var tools = agent ? voice.tools : null;
    switches.paint(tools);
    var off = tools ? Object.keys(tools).filter(function (k) { return tools[k] === 'off'; }).length : 0;
    $('dgl-sw-count').textContent = off ? off + ' OFF' : '';
  }
  var vstate = agent && VOICE_SUB[voice.state] ? voice.state : 'absent';

  var ptt = $('dgl-ptt');
  ptt.dataset.state = vstate;
  // Usable only when there is a pipeline to hear it; otherwise the button
  // is honestly off rather than a press that goes nowhere.
  ptt.disabled = vstate === 'absent' && !pressed;
  $('dgl-ptt-s').textContent = !agent ? 'NO AGENT'
    : vstate === 'absent' ? 'NO VOICE PIPELINE' : VOICE_SUB[vstate];

  rows($('dgl-voice-rows'), [
    ['dougvoice', !agent ? '—' : voice.satellite ? 'connected' : 'not connected'],
    ['last heard', agent && voice.last_turn ? fmtAgo(voice.last_turn) : '—'],
  ]);
  // Asking needs the agent and a pipeline that is not mid-turn.
  var canAsk = agent && vstate !== 'absent' && vstate !== 'thinking' && vstate !== 'listening';
  $('dgl-askbar').disabled = !canAsk;
  $('dgl-askbar-s').textContent = !agent ? 'Douglas is unreachable'
    : vstate === 'absent' ? 'no voice pipeline on Douglas'
    : vstate === 'thinking' ? 'Douglas is thinking…' : 'tap to type';
  if (!agent) $('dgl-note').textContent = d.why || 'douglas-agent did not answer';
  else if ($('dgl-note').dataset.sticky !== '1') $('dgl-note').textContent = '';

  paintLog(agent ? (d.transcript || []) : null);
  paintSys(agent ? d : null);
}

function paintLog(turns) {
  if (convo) convo.paint(turns);
}

function turnEl(t) {
  var me = t.who === 'user';
  var div = document.createElement('div');
  div.className = 'dgl-turn ' + (me ? 'user' : 'douglas');
  div.innerHTML = '<span class="who"><b></b> <span class="via"></span></span>' +
                  '<span class="txt"></span>';
  div.querySelector('b').textContent = whoLabel(t);
  div.querySelector('.via').textContent =
    '· ' + (t.via || 'voice') + ' · ' + clock(t.ts);
  div.querySelector('.txt').textContent = t.text || '';
  if (!me && t.image) div.appendChild(picture(t.image));
  return div;
}

// A picture he drew comes as an id on his line. The element is kept from one
// repaint to the next, so a new turn does not fetch every picture in the log
// again. A touch fills the panel with it; another puts it back.
var pics = {};
function picture(id) {
  if (pics[id]) return pics[id];
  var img = pics[id] = new Image();
  img.className = 'dgl-pic';
  img.alt = 'a picture Douglas drew';
  img.onload = function () { if (convo) convo.follow(); };
  img.onerror = function () { delete pics[id]; img.alt = 'his picture did not load'; };
  img.onclick = function () { img.classList.toggle('big'); };
  img.src = '/api/douglas/picture?id=' + encodeURIComponent(id);
  return img;
}

function clock(ts) {
  if (!ts) return '';
  var d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function gb(b) { return b == null ? null : b / 1073741824; }
function num(v, dp) { return v == null || !isFinite(v) ? null : Number(v).toFixed(dp); }

function paintSys(d) {
  var host = (d && d.host) || {}, gpu = (d && d.gpu) || {}, llm = (d && d.llm) || {};
  $('dgl-model').textContent = d && llm.model ? llm.model.toUpperCase()
    : d ? (llm.state === 'loading' ? 'LOADING' : 'NO MODEL') : '—';

  // Unified memory: on this APU the model lives in GTT as much as in the
  // carved-out VRAM, so the honest "how full is Douglas" is the sum.
  var gUsed = (gpu.vram_used_bytes || 0) + (gpu.gtt_used_bytes || 0);
  var gTot = (gpu.vram_total_bytes || 0) + (gpu.gtt_total_bytes || 0);
  var meters = [
    ['GPU', num(gpu.busy_pct, 0), '%', gpu.busy_pct],
    ['MODEL MEMORY', gTot ? num(gb(gUsed), 1) : null,
     gTot ? '/ ' + Math.round(gb(gTot)) + ' GB' : 'GB',
     gTot ? 100 * gUsed / gTot : null],
    ['GENERATE', num(llm.gen_tps, 1), 'tok/s', null],
    ['PROMPT', num(llm.prompt_tps, 0), 'tok/s', null],
    ['CPU', num(host.cpu_pct, 0), '%', host.cpu_pct],
    ['RAM', host.mem_total_bytes ? num(gb(host.mem_used_bytes), 1) : null,
     host.mem_total_bytes ? '/ ' + Math.round(gb(host.mem_total_bytes)) + ' GB' : 'GB',
     host.mem_total_bytes ? 100 * host.mem_used_bytes / host.mem_total_bytes : null],
    ['APU TEMP', num(host.temp_c, 0), '°C', null],
    ['POWER', num(host.power_w, 0), 'W', null],
  ];
  var box = $('dgl-meters');
  if (box.children.length !== meters.length) {
    box.innerHTML = '';
    meters.forEach(function () {
      var m = document.createElement('div');
      m.className = 'dgl-m';
      m.innerHTML = '<div class="k"></div><div class="v"></div>' +
                    '<div class="mb"><i></i></div>';
      box.appendChild(m);
    });
  }
  meters.forEach(function (row, i) {
    var m = box.children[i];
    m.querySelector('.k').textContent = row[0];
    var v = m.querySelector('.v');
    v.classList.toggle('stale', row[1] == null);
    v.innerHTML = '';
    v.appendChild(document.createTextNode(row[1] == null ? '—' : row[1]));
    var u = document.createElement('span');
    u.className = 'u';
    u.textContent = row[2];
    v.appendChild(u);
    var bar = m.querySelector('.mb');
    bar.hidden = row[3] == null;
    if (row[3] != null) {
      var pct = Math.max(0, Math.min(100, row[3]));
      bar.firstChild.style.width = pct + '%';
      bar.firstChild.classList.toggle('hot', pct > 90);
    }
  });

  // His services and his disk, as his own machine reads them: the same
  // reading he is given when asked how he is. An agent from before it could
  // tell has no such field, and the row is a dash, not "none".
  var svc = d && 'units' in d ? servicesLine(d.units) : null;
  var disk = d && d.disks && d.disks[0], full = diskPct(disk);
  rows($('dgl-sys-rows'), [
    ['requests', d ? (llm.processing != null ? llm.processing + ' active' : '—') +
      (llm.deferred ? ' · ' + llm.deferred + ' queued' : '') +
      (llm.slots ? ' · ' + (llm.busy_slots != null ? llm.busy_slots + ' of ' : '') + llm.slots + ' slots' : '')
      : '—'],
    ['context', d && llm.ctx ? llm.ctx.toLocaleString() + ' tokens' : '—'],
    ['services', svc && svc.text ? svc.text : '—'],
    ['disk', full != null ? full + ' % of ' + Math.round(gb(disk.total_bytes)) + ' GB used' : '—'],
    ['up', d && host.uptime_s ? fmtAgo(Date.now() / 1000 - host.uptime_s).replace(/ ago$/, '') : '—'],
  ]);
  // Colour is state: a service that should be up and is not is a fault.
  var srow = $('dgl-sys-rows').children[2];
  if (srow) srow.classList.toggle('bad', !!(svc && svc.bad));
}

/* ── push-to-talk ─────────────────────────────────────────────────────── */

export function wireDouglas() {
  var ptt = $('dgl-ptt');
  if (!ptt) return;

  // The panel is the owner's own screen, on the Pi itself: what it asks of
  // his memory needs no token. Typing goes through the on-screen keyboard.
  memory = memoryView($('dgl-mem'), {
    get: getJSON, post: post, count: $('dgl-mem-count'),
    type: function (title, text, save) { openKeyboard(title, save, text); },
  });
  did = didView($('dgl-did'), { get: getJSON, rows: 40 });
  approval = approvalView($('dgl-apv'), { post: post, via: 'deck' });
  switches = switchesView($('dgl-sw'), { post: post });
  // The panel is loopback: the picture route needs no token here.
  cameras = camerasView($('dgl-cam'), {
    get: getJSON, post: post, count: $('dgl-cam-count'),
    type: function (title, text, save) { openKeyboard(title, save, text); },
    picture: function (body) {
      return fetch('/api/cameras/image', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                           body: JSON.stringify(body) }).then(function (r) {
        if (!r.ok || !/^image\//.test(r.headers.get('Content-Type') || '')) {
          return r.json().then(function (j) { return { error: j.error || j.code || 'HTTP ' + r.status }; },
            function () { return { error: 'HTTP ' + r.status }; });
        }
        var obs = null;
        try { obs = JSON.parse(r.headers.get('X-Observation') || 'null'); } catch (e) { obs = null; }
        return r.blob().then(function (b) { return { blob: b, obs: obs }; });
      }, function () { return { error: 'node1 did not answer' }; });
    },
  });
  job = jobView($('dgl-job'));
  notices = noticesView($('dgl-ntc'), { get: getJSON, post: post, count: $('dgl-ntc-count'),
                                        countClass: 'ntc-n', over: 8 });
  convo = convoLog($('dgl-log'), {
    get: getJSON, line: turnEl, rowClass: 'dgl-earlier', emptyClass: 'dgl-empty',
    empty: function (none) {
      return none ? 'No transcript: douglas-agent is not answering.'
        : 'Nothing said yet. Hold to talk, or speak to DougVoice.';
    },
  });
  [].forEach.call($('dgl-tabs').children, function (b) {
    b.addEventListener('click', function () { showTab(b.dataset.tab); });
  });

  function note(msg, sticky) {
    var n = $('dgl-note');
    n.textContent = msg || '';
    n.dataset.sticky = sticky ? '1' : '';
    if (sticky) setTimeout(function () { n.dataset.sticky = ''; }, 5000);
  }

  function send(action) {
    return post('/api/douglas/ptt', { action: action }).then(function (r) {
      if (!r) note('deck-api did not answer', true);
      else if (!r.ok) note(r.message || 'Douglas refused', true);
      return r;
    });
  }

  // The panel has no keyboard: the ASK bar opens one. The reply lands in
  // the conversation like every reply; the keyboard only carries the words.
  $('dgl-askbar').addEventListener('click', function () {
    if ($('dgl-askbar').disabled) return;
    openKeyboard('ASK DOUGLAS', function (text) {
      return post('/api/douglas/say', { text: text, via: 'deck' }).then(function (r) {
        if (r && r.ok) { tick(); return null; }
        return r ? (r.message || 'Douglas refused') : 'deck-api did not answer';
      });
    });
  });

  // Pointer events with capture: this panel's Chromium delivers pointer
  // events, not Touch Events, and capture keeps a thumb that drifts off the
  // button from ending the utterance early.
  ptt.addEventListener('pointerdown', function (e) {
    if (ptt.disabled || pressed) return;
    e.preventDefault();
    try { ptt.setPointerCapture(e.pointerId); } catch (ignored) { }
    pressed = true;
    ptt.classList.add('pressed');
    douglasPoll(true);
    send('start');
  });
  function release(action) {
    return function () {
      if (!pressed) return;
      pressed = false;
      ptt.classList.remove('pressed');
      send(action);
    };
  }
  ptt.addEventListener('pointerup', release('stop'));
  // A cancelled pointer (palm, system gesture) is not "I finished speaking".
  ptt.addEventListener('pointercancel', release('cancel'));
  ptt.addEventListener('contextmenu', function (e) { e.preventDefault(); });
}
