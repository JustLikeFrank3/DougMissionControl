/* cameras.js — DougRing in Mission Control: the owner's camera switches.

   DougRing, on this Pi, holds the Ring link and keeps the rules; this view
   only shows them and asks for changes. What is on it:

   - the switch for all cameras: a touch switches them all off, at once, for
     Douglas and for this screen; switching them on again takes a hold;
   - each camera Ring shares: its name here (tap to rename), whether it is
     online, and its own switch (off until switched on, with a hold);
   - LOOK: a picture taken now, shown here and nowhere else. It lives in
     this page's memory until it is closed, and is never saved;
   - the link to Ring, the service's health, and the journal of every
     access;
   - FORGET RING: drops the tokens (a hold). Linking again is the way back;
     Ring's own console ("Disconnect all") ends it on Ring's side.

   opts: get(path) -> JSON, post(path, body) -> JSON, picture(body) ->
   Promise<{blob, obs} | {error}>, type(title, text, save) to ask for a
   name, count: an element for the number switched on. */

import { wireHold } from './ui.js';
import { fmtAgo } from './format.js';

function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function ago(ms) { return ms ? fmtAgo(ms / 1000) + ' ago' : 'never'; }

function holdButton(label, act, cls, text) {
  var b = el('button', 'sw-b off hold-btn' + (cls ? ' ' + cls : ''));
  b.type = 'button';
  b.setAttribute('aria-label', 'hold to ' + label);
  b.appendChild(el('i', 'hold'));
  b.appendChild(el('span', '', (text || 'OFF') + ' · HOLD'));
  wireHold(b, act);
  return b;
}

function tapButton(text, cls, act, aria) {
  var b = el('button', 'sw-b ' + cls, text);
  b.type = 'button';
  if (aria) b.setAttribute('aria-label', aria);
  b.addEventListener('click', act);
  return b;
}

/** What the picture is, in one line: camera, when, how old, and whether it is of now. */
export function obsLine(o, now) {
  if (!o) return '';
  var taken = o.captured_ms ? new Date(o.captured_ms) : null;
  var age = o.captured_ms ? Math.max(0, Math.round(((now || Date.now()) - o.captured_ms) / 1000)) : null;
  var live = o.source === 'live-triggered' && age != null && age < 120;
  return (o.camera_name || 'camera') + ' · ' +
    (taken ? 'taken ' + taken.toLocaleTimeString() + ' (' + age + ' s old)' : 'time unknown') + ' · ' +
    (live ? 'recorded now, on request' : 'NOT LIVE: the latest picture Ring had');
}

export function camerasView(host, opts) {
  var last = '', busy = {};
  host.classList.add('swv', 'camv');
  host.innerHTML = '';
  var head = el('div', 'cam-head'), list = el('div', 'sw-list cam-list'), shot = el('div', 'cam-shot');
  var note = el('div', 'mem-note'), health = el('div', 'cam-health'), jr = el('div', 'cam-journal');
  [head, list, shot, note, health, jr].forEach(function (x) { host.appendChild(x); });
  shot.hidden = true;
  var url = null;

  function said(a, ok) {
    note.classList.toggle('bad', !(a && a.ok));
    note.textContent = a && a.ok ? ok : (a && (a.message || a.error || a.code)) || 'node1 did not answer';
    if (a && a.ok) refresh();
  }
  function change(body, ok) { note.textContent = ''; opts.post('/api/cameras', body).then(function (a) { said(a, ok); }); }

  function closeShot() {
    if (url) URL.revokeObjectURL(url);
    url = null;
    shot.innerHTML = '';
    shot.hidden = true;
  }

  function look(c) {
    if (busy[c.id]) return;
    busy[c.id] = true;
    closeShot();
    shot.hidden = false;
    shot.appendChild(el('div', 'mem-note', 'Asking ' + c.name + ' for a picture of now… (up to a minute)'));
    opts.picture({ camera: c.id }).then(function (r) {
      busy[c.id] = false;
      closeShot();
      shot.hidden = false;
      if (!r || r.error) {
        shot.appendChild(el('div', 'mem-note bad', 'No picture: ' + ((r && r.error) || 'node1 did not answer')));
      } else {
        url = URL.createObjectURL(r.blob);
        var img = el('img', 'cam-img');
        img.alt = 'what ' + c.name + ' saw';
        img.src = url;
        shot.appendChild(img);
        var cap = el('div', 'cam-cap' + (/NOT LIVE/.test(obsLine(r.obs)) ? ' stale' : ''), obsLine(r.obs));
        shot.appendChild(cap);
      }
      shot.appendChild(tapButton('CLOSE', 'on', closeShot, 'close the picture'));
      refresh();
    });
  }

  function camRow(c, globalOn) {
    var line = el('div', 'sw ' + (c.enabled ? 'on' : 'off'));
    var words = el('div', 'sw-w');
    var name = el('b', 'cam-name', c.name);
    name.title = 'tap to rename';
    name.addEventListener('click', function () {
      if (!opts.type) return;
      opts.type('Name for ' + (c.ring_name || 'this camera'), c.name, function (v) {
        change({ op: 'name', id: c.id, name: v }, 'Renamed.');
      });
    });
    words.appendChild(name);
    words.appendChild(el('small', c.online ? '' : 'bad',
      (c.online ? 'online' : 'OFFLINE') + (c.battery ? ' · battery' : '') + (c.doorbell ? ' · doorbell' : '') +
      (c.ring_name && c.ring_name !== c.name ? ' · Ring calls it ' + c.ring_name : '')));
    line.appendChild(words);
    if (c.enabled && globalOn && c.online) line.appendChild(tapButton('LOOK', 'on cam-look', function () { look(c); },
      'take a picture from ' + c.name));
    if (c.enabled) {
      line.appendChild(tapButton('ON', 'on', function () { change({ op: 'disable', id: c.id }, c.name + ' is off.'); },
        'switch ' + c.name + ' off'));
    } else {
      line.appendChild(holdButton('switch ' + c.name + ' on', function () {
        change({ op: 'enable', id: c.id }, c.name + ' is on.');
      }));
    }
    return line;
  }

  function paint(v) {
    var key = JSON.stringify(v || null);
    if (key === last) return;                   // unchanged: a hold under way is left alone
    last = key;
    head.innerHTML = ''; list.innerHTML = ''; health.innerHTML = ''; jr.innerHTML = '';
    if (!v || !v.installed) {
      list.appendChild(el('div', 'mem-empty', v ? 'DougRing is not installed on node1 (dougring/setup.sh).'
        : 'node1 did not answer.'));
      if (opts.count) opts.count.textContent = '—';
      return;
    }
    if (!v.up) {
      list.appendChild(el('div', 'mem-empty', 'DougRing is not answering on node1: ' + (v.error || '')));
      return;
    }
    var st = v.status || {}, cams = v.cameras || [];
    var on = cams.filter(function (c) { return c.enabled; }).length;
    if (opts.count) opts.count.textContent = st.global_on ? on + ' on' : 'OFF';
    // the switch for all of them
    var g = el('div', 'sw ' + (st.global_on ? 'on' : 'off'));
    var gw = el('div', 'sw-w');
    gw.appendChild(el('b', '', 'All cameras'));
    gw.appendChild(el('small', '', st.global_on ? 'Douglas may look through the cameras switched on below'
      : 'nobody can have a picture, Douglas included'));
    g.appendChild(gw);
    g.appendChild(st.global_on
      ? tapButton('ON', 'on', function () { change({ op: 'switch', on: false }, 'All cameras are off.'); },
        'switch all cameras off')
      : holdButton('switch all cameras on', function () { change({ op: 'switch', on: true }, 'Cameras are on.'); }));
    head.appendChild(g);
    // the cameras
    if (!cams.length) {
      list.appendChild(el('div', 'mem-empty', (st.accounts || []).length
        ? (v.cameras_error ? 'Ring did not answer: ' + v.cameras_error : 'Ring shares no camera with DougRing.')
        : 'No Ring account is linked yet: "Log in with Ring" in the Ring Developer Console.'));
    }
    cams.forEach(function (c) { list.appendChild(camRow(c, st.global_on)); });
    // the link and the service
    var acct = (st.accounts || [])[0];
    var h = st.health || {}, t = st.telemetry || {};
    var rowsOf = [
      ['Ring link', acct ? acct.email + ' · ' + (acct.state === 'linked' ? 'linked' : 'LINK AGAIN') +
        ' · token refreshed ' + ago((acct.refreshed_at || 0) * 1000) : 'not linked'],
      ['last picture', ago(h.last_success)],
      ['last access', ago(h.last_access)],
      ['last error', h.last_error ? (h.last_error.status + ' ' + (h.last_error.code || '') + ', ' + ago(h.last_error.at)) : 'none'],
      ['service', 'DougRing ' + (st.version || '') + ' · ' + (t.pictures || 0) + ' pictures · ' + (t.ring_calls || 0) +
        ' calls to Ring · ' + (t.ring_errors || 0) + ' errors' + (st.throttled_for_s ? ' · Ring asked to wait ' +
        st.throttled_for_s + ' s' : '')],
    ];
    rowsOf.forEach(function (r) {
      var line = el('div', 'cam-row');
      line.appendChild(el('span', 'k', r[0]));
      line.appendChild(el('span', /AGAIN|^[45]\d\d /.test(r[1]) ? 'v bad' : 'v', r[1]));
      health.appendChild(line);
    });
    if (acct) {
      var f = el('div', 'sw');
      var fw = el('div', 'sw-w');
      fw.appendChild(el('b', '', 'Forget the Ring account'));
      fw.appendChild(el('small', '', 'drops the tokens and switches everything off; linking again is the way back'));
      f.appendChild(fw);
      f.appendChild(holdButton('forget Ring', function () {
        change({ op: 'forget' }, 'Forgotten here. To end it at Ring too: "Disconnect all" in the Ring Developer Console.');
      }, 'cam-forget', 'FORGET'));
      health.appendChild(f);
    }
    // the journal
    jr.appendChild(el('h3', 'cam-jh', 'WHO LOOKED'));
    var names = {};
    cams.forEach(function (c) { names[c.id] = c.name; });
    (v.journal || []).slice(0, 15).forEach(function (j) {
      jr.appendChild(el('div', 'cam-j' + (/^(refused|failed)/.test(j.result || '') ? ' bad' : ''),
        ago(j.at) + ' · ' + (j.role === 'douglas' ? 'Douglas' : 'you') + ' · ' + j.what +
        (j.camera ? ' · ' + (names[j.camera] || j.camera.slice(-6)) : '') +
        (j.result && j.result !== 'ok' ? ' · ' + j.result : '') +
        (j.source ? ' · ' + j.source + (j.age_s != null ? ', ' + j.age_s + ' s old' : '') : '')));
    });
    if (!(v.journal || []).length) jr.appendChild(el('div', 'mem-empty', 'Nobody has looked yet.'));
  }

  function refresh() { return opts.get('/api/cameras').then(paint); }

  return { refresh: refresh, paint: paint, close: closeShot };
}
