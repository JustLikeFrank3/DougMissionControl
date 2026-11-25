/* notices.js — what the watch on Douglas raised, for the owner's screens.

   One view for the phone page and the panel, like memory.js. A notice is a
   sentence with a reading in it: a machine that stopped answering, a service
   that is down, a disk nearly full, a task left open. It is raised by rules
   on Douglas, with no model in it, once for as long as it holds, and it
   goes away by itself when what it says is put right.

   What can be done here is what an owner does with a notice: see it, say it
   has been seen (it is then not mentioned or said aloud again; it stays
   until it is over), and say where each severity is to go. Colour is state,
   as everywhere on this deck: red is critical, amber a warning. */

import { fmtAgo, fmtClock } from './format.js';

var ROUTE = '/api/douglas/memory';
var SEVERITIES = ['critical', 'warning', 'information'];
var CLS = { critical: 'bad', warning: 'warn', information: '' };

function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

/** How many are open, in the few words a pill has room for, and how bad the
    worst is. `null` for "could not be read", which is not "none". */
export function summary(got) {
  if (!got || !got.ok) return { text: '—', cls: '' };
  var open = got.open || [];
  if (!open.length) return { text: 'NONE', cls: 'ok' };
  var worst = open.some(function (n) { return n.severity === 'critical'; }) ? 'bad'
    : open.some(function (n) { return n.severity === 'warning'; }) ? 'busy' : '';
  var unseen = open.filter(function (n) { return !n.acked; }).length;
  return { text: open.length + ' OPEN' + (unseen && unseen < open.length ? ' · ' + unseen + ' NEW' : ''),
           cls: worst };
}

/** For the panel's top bar, from the count the Pi keeps with its probe of
    Douglas: how many warnings and worse nobody has marked as seen, or null
    when there is none (or the count could not be read: the bar then says
    what it always said). */
export function navNotices(n) {
  if (!n || !(n.new > 0)) return null;
  return { text: n.new + (n.new === 1 ? ' notice' : ' notices'), critical: n.worst === 'critical' };
}

/** An hour of the day one step on or back, round midnight. */
export function hourStep(h, by) { return (((+h || 0) + by) % 24 + 24) % 24; }

/** Since when, and what has happened to it since: one line under the sentence. */
export function meta(n, now) {
  var parts = [];
  if (n.cleared) {
    parts.push('over since ' + fmtClock(n.cleared) + ' · lasted ' + fmtAgo(n.raised, n.cleared));
  } else {
    parts.push('since ' + fmtClock(n.raised) + ' · ' + fmtAgo(n.raised, now));
    if (n.times > 1) parts.push('got worse');
    parts.push(n.acked ? 'seen' : n.told ? 'Douglas has mentioned it' : 'not yet mentioned');
    if (n.spoken) parts.push('said aloud');
  }
  return parts.join(' · ');
}

/**
 * The notices, in `host`.
 *   opts.get(path), opts.post(path, body)   this screen's way of asking deck-api
 *   opts.count                              an element that says how many are open
 * Returns { refresh() }.
 */
export function noticesView(host, opts) {
  var st = { got: null, prefs: false };
  var list = el('div', 'ntc-list'), where = el('div', 'ntc-where'), note = el('div', 'mem-note');
  host.classList.add('ntcv');
  host.innerHTML = '';
  [list, where, note].forEach(function (e) { host.appendChild(e); });

  function say(text, bad) { note.textContent = text || ''; note.classList.toggle('bad', !!bad); }

  function row(n, now) {
    var r = el('div', 'ntc ' + (n.cleared ? 'over' : CLS[n.severity] || ''));
    var top = el('div', 'ntc-top');
    top.appendChild(el('b', 'ntc-s', (n.severity || '').toUpperCase()));
    top.appendChild(el('span', 'ntc-w', n.subject || ''));
    r.appendChild(top);
    r.appendChild(el('div', 'ntc-t', n.text || ''));
    r.appendChild(el('div', 'ntc-m', meta(n, now)));
    if (!n.cleared && !n.acked) {
      r.classList.add('new');                    // room for the button beside the words
      var seen = el('button', 'ntc-ack');
      seen.type = 'button';
      seen.textContent = 'SEEN';
      seen.addEventListener('click', function () {
        opts.post(ROUTE, { op: 'notice_ack', id: n.id }).then(function (a) {
          if (!a || !a.ok) say((a && a.text) || 'node1 did not answer', true);
          else refresh();
        });
      });
      r.appendChild(seen);
    }
    return r;
  }

  function paintWhere() {
    var p = st.got && st.got.prefs;
    where.innerHTML = '';
    if (!p) return;
    var head = el('button', 'mem-chip' + (st.prefs ? ' on' : ''), 'WHERE THEY GO');
    head.type = 'button';
    head.addEventListener('click', function () { st.prefs = !st.prefs; paintWhere(); });
    where.appendChild(head);
    if (!st.prefs) return;
    function set(body) {
      body.op = 'notice_prefs';
      opts.post(ROUTE, body).then(function (a) {
        if (!a || !a.ok) say((a && a.text) || 'node1 did not answer', true);
        else refresh();
      });
    }
    where.appendChild(el('div', 'ntc-m', 'On these screens: always. Whether Douglas mentions one with his ' +
      'next answer, and whether it is said aloud in the room, is yours to say for each kind.'));
    SEVERITIES.forEach(function (sev) {
      var line = el('div', 'ntc-pref');
      line.appendChild(el('b', 'ntc-s ' + (CLS[sev] || ''), sev.toUpperCase()));
      [['tell', 'MENTIONED'], ['aloud', 'ALOUD']].forEach(function (k) {
        var on = !!(p[sev] && p[sev][k[0]]);
        var b = el('button', 'mem-chip' + (on ? ' on' : ''), k[1] + (on ? ' · ON' : ' · OFF'));
        b.type = 'button';
        b.addEventListener('click', function () {
          var body = { severity: sev };
          body[k[0]] = !on;
          set(body);
        });
        line.appendChild(b);
      });
      where.appendChild(line);
    });
    // The hours nothing is said aloud in, an hour at a touch.
    var quiet = el('div', 'ntc-pref');
    quiet.appendChild(el('b', 'ntc-s', 'NOTHING ALOUD'));
    [['quiet_from', 'FROM'], ['quiet_to', 'TO']].forEach(function (k) {
      var box = el('span', 'ntc-hour');
      [[-1, '−'], [0, k[1] + ' ' + p[k[0]] + ':00'], [1, '+']].forEach(function (s) {
        if (!s[0]) { box.appendChild(el('span', 'ntc-h', s[1])); return; }
        var b = el('button', 'mem-chip', s[1]);
        b.type = 'button';
        b.setAttribute('aria-label', (s[0] < 0 ? 'an hour earlier, ' : 'an hour later, ') + k[1].toLowerCase());
        b.addEventListener('click', function () {
          var body = {};
          body[k[0]] = hourStep(p[k[0]], s[0]);
          set(body);
        });
        box.appendChild(b);
      });
      quiet.appendChild(box);
    });
    where.appendChild(quiet);
  }

  function paint() {
    var got = st.got, now = Date.now() / 1000;
    if (opts.count) {
      var s = summary(got);
      opts.count.textContent = s.text;
      opts.count.className = (opts.countClass || 'pill') + (s.cls ? ' ' + s.cls : '');
    }
    list.innerHTML = '';
    if (!got || !got.ok) {
      // Not read is not none: what was asked for could not be shown.
      list.appendChild(el('div', 'mem-empty', (got && got.text) || 'node1 did not answer'));
      where.innerHTML = '';
      return;
    }
    (got.open || []).forEach(function (n) { list.appendChild(row(n, now)); });
    if (!(got.open || []).length) {
      list.appendChild(el('div', 'mem-empty', 'Nothing is raised. The watch on Douglas looks every twenty ' +
        'seconds: at him, and at what Mission Control sees of the rest.'));
    }
    (got.over || []).slice(0, opts.over || 5).forEach(function (n) { list.appendChild(row(n, now)); });
    paintWhere();
  }

  function refresh() {
    return opts.get(ROUTE + '?op=notices&limit=30').then(function (r) {
      st.got = r || { ok: false };
      if (r && r.ok) say('');
      paint();
    });
  }

  paint();
  return { refresh: refresh };
}
