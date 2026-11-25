/* owner.js — the two things only the owner's screens do to Douglas.

   An answer. What he may not do without a yes (push, change the owner's
   records, forget, start a job made of somebody else's words) he reads back
   and leaves waiting. The yes can be said to him on the line it was asked
   on, or given here: a hold on APPROVE, bound to the number of exactly what
   was read back, or a tap on REFUSE. From the chat page a typed yes does
   not count at all, and here is the only place to give one.

   A switch. Each thing he could use, and whether he has it. A tap switches
   it off; switching it on again takes a hold. It is written down on
   Douglas by a program of its own that he cannot reach, and he finds out
   at his next turn. A switch takes a tool away, not a subject: with web
   pages or the search still on, he can read by them what the tool read.

   One view of each for the phone page and the panel, like memory.js. */

import { wireHold } from './ui.js';

var WHERE = { webui: 'the chat page', phone: 'the phone', deck: 'the panel', voice: 'by voice', chat: 'a chat window' };
var WHAT = { push: 'A PUSH', job: 'A JOB', forget: 'FORGETTING', records: 'A CHANGE TO YOUR RECORDS' };
/* What each tool is, in a few words, and whether it can change anything. */
export var TOOLS = [
  ['memory', 'his memory', 'notes and recalls'],
  ['fleet', 'the machines at home', 'reads'],
  ['self', 'his own machine', 'reads'],
  ['weather', 'the weather', 'reads'],
  ['news', 'the news', 'reads'],
  ['wikipedia', 'the Wikipedia lookup', 'reads; with web pages on he can still read its pages'],
  ['read_page', 'web pages', 'reads'],
  ['search', 'web search', 'reads; what is searched for leaves the house'],
  ['reddit', 'Reddit search', 'reads; what is searched for leaves the house'],
  ['look', 'looking at pictures', 'reads'],
  ['draw', 'drawing', 'makes pictures'],
  ['edit', 'editing pictures', 'makes pictures'],
  ['jobcontext', 'your records', 'changes them on a yes'],
  ['repo', 'the workshop', 'clones, runs jobs, pushes on a yes']
];

function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

/** How long a change still waits, in the few words there is room for. */
export function left(until, now) {
  var s = Math.floor((+until || 0) - now);
  if (!(s > 0)) return 'no longer waiting';
  return (s >= 60 ? Math.floor(s / 60) + ' m ' : '') + (s % 60) + ' s left';
}

/** The line over a change that is waiting: what it is and where it was asked for. */
export function waitingHead(p) {
  return (WHAT[p && p.kind] || 'A CHANGE') + ' · asked on ' + (WHERE[p && p.via] || 'another line');
}

/** The switches as rows: every tool there is a word for, then any the
    pipeline has that this page has not heard of, so that none is hidden. */
export function switchRows(tools) {
  if (!tools) return [];
  var named = TOOLS.filter(function (t) { return t[0] in tools; }).map(function (t) {
    return { name: t[0], label: t[1], does: t[2], state: tools[t[0]] };
  });
  var known = TOOLS.map(function (t) { return t[0]; });
  Object.keys(tools).sort().forEach(function (k) {
    if (known.indexOf(k) < 0) named.push({ name: k, label: k, does: '', state: tools[k] });
  });
  return named;
}

/**
 * The change that is waiting, in `host` (hidden when there is none).
 *   opts.post(path, body), opts.via ('phone' or 'deck')
 * Returns { paint(pending) }: call it with every frame of his state.
 */
export function approvalView(host, opts) {
  var shown = null, timer = null, busy = false;
  var head = el('div', 'apv-h'), until = el('span', 'apv-u'), text = el('div', 'apv-t');
  var acts = el('div', 'mem-acts'), note = el('div', 'mem-note');
  var yes = el('button', 'go hold-btn'), no = el('button', '', 'REFUSE');
  yes.type = no.type = 'button';
  yes.appendChild(el('i', 'hold'));
  yes.appendChild(el('span', '', 'HOLD TO APPROVE'));
  acts.appendChild(yes);
  acts.appendChild(no);
  host.classList.add('apv');
  host.innerHTML = '';
  [head, until, text, acts, note].forEach(function (e) { host.appendChild(e); });
  host.hidden = true;

  function answer(ok) {
    if (!shown || busy) return;
    busy = true;
    note.textContent = '';
    // The number is the one that was on the screen when it was pressed.
    opts.post('/api/douglas/approval', { id: shown.id, yes: ok, via: opts.via }).then(function (a) {
      busy = false;
      if (!a || !a.ok) {
        note.textContent = (a && (a.message || a.text)) || 'node1 did not answer';
        note.classList.add('bad');
      }
    });
  }
  wireHold(yes, function () { answer(true); });
  no.addEventListener('click', function () { answer(false); });

  function tick() {
    if (shown) until.textContent = left(shown.until, Date.now() / 1000);
  }

  return {
    paint: function (p) {
      p = p && p.id ? p : null;
      if ((p && p.id) !== (shown && shown.id)) {
        note.textContent = '';
        note.classList.remove('bad');
        if (p) {
          head.textContent = waitingHead(p);
          text.textContent = p.text || '';
        }
      }
      shown = p;
      host.hidden = !p;
      if (p && !timer) timer = setInterval(tick, 1000);
      if (!p && timer) { clearInterval(timer); timer = null; }
      tick();
    }
  };
}

/**
 * What he may use, in `host`.
 *   opts.post(path, body)
 * Returns { paint(tools) }.
 */
export function switchesView(host, opts) {
  var last = '', note = el('div', 'mem-note'), list = el('div', 'sw-list');
  host.classList.add('swv');
  host.innerHTML = '';
  host.appendChild(list);
  host.appendChild(note);

  function set(name, on) {
    note.textContent = '';
    opts.post('/api/douglas/switch', { tool: name, on: on }).then(function (a) {
      note.classList.toggle('bad', !(a && a.ok));
      note.textContent = a && a.ok ? 'He finds out at his next turn.'
        : (a && (a.message || a.text)) || 'node1 did not answer';
      if (a && a.ok && opts.changed) opts.changed();
    });
  }

  function row(r) {
    var line = el('div', 'sw ' + r.state);
    var words = el('div', 'sw-w');
    words.appendChild(el('b', '', r.label));
    words.appendChild(el('small', '', r.state === 'absent' ? 'not set up on Douglas' : r.does));
    line.appendChild(words);
    if (r.state === 'on') {
      var off = el('button', 'sw-b on', 'ON');
      off.type = 'button';
      off.setAttribute('aria-label', 'switch ' + r.label + ' off');
      off.addEventListener('click', function () { set(r.name, false); });
      line.appendChild(off);
    } else if (r.state === 'off') {
      // Giving something back takes a hold; taking it away, a touch.
      var on = el('button', 'sw-b off hold-btn');
      on.type = 'button';
      on.setAttribute('aria-label', 'hold to switch ' + r.label + ' on');
      on.appendChild(el('i', 'hold'));
      on.appendChild(el('span', '', 'OFF · HOLD'));
      wireHold(on, function () { set(r.name, true); });
      line.appendChild(on);
    } else {
      line.appendChild(el('span', 'sw-b none', '—'));
    }
    return line;
  }

  return {
    paint: function (tools) {
      var key = JSON.stringify(tools || null);
      if (key === last) return;                 // the same as is on the screen: a hold in progress is left alone
      last = key;
      list.innerHTML = '';
      var rows = switchRows(tools);
      if (!rows.length) {
        list.appendChild(el('div', 'mem-empty', 'His switches could not be read: the voice pipeline on Douglas ' +
          'is not answering, or is older than the switches.'));
        return;
      }
      rows.forEach(function (r) { list.appendChild(row(r)); });
    }
  };
}
