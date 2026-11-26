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
import { fmtAgo } from './format.js';

var WHERE = { webui: 'the chat page', phone: 'the phone', deck: 'the panel', voice: 'by voice', chat: 'a chat window' };
var WHAT = { push: 'A PUSH', job: 'A JOB', forget: 'FORGETTING', records: 'A CHANGE TO YOUR RECORDS' };
/* What each tool is, in a few words, and whether it can change anything. */
export var TOOLS = [
  ['memory', 'his memory', 'notes and recalls'],
  ['fleet', 'the machines at home', 'reads'],
  ['camera', 'the Ring cameras', 'looks through the ones switched on under CAMERAS'],
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

/** How the requirements a job was handed stand, in a few words; '' for a
    job that was handed none. Counted here from each one's own state, so a
    job that ended with some left never reads as done. */
export function needsLine(report) {
  var needs = (report && report.needs) || [];
  if (!needs.length) return '';
  var count = function (s) { return needs.filter(function (n) { return n.state === s; }).length; };
  var done = count('done'), blocked = count('blocked'), pending = count('pending');
  var out = needs.length - done - blocked - pending;
  if (pending === needs.length) return needs.length + ' REQUIREMENT' + (needs.length === 1 ? '' : 'S');
  return done + ' OF ' + needs.length + ' DONE' + (out ? ' · ' + out + ' OUTSTANDING' : '')
    + (blocked ? ' · ' + blocked + ' BLOCKED' : '');
}

/** The job in his workshop, in a line: that it is under way and for how
    long, or that it ended and how long ago, and where it was handed
    requirements, how many of them are done. */
export function jobLine(job, now) {
  if (!job) return '';
  var needs = needsLine(job.report);
  return (job.running ? 'IN THE WORKSHOP · under way' + (job.began ? ' for ' + fmtAgo(job.began, now) : '')
    : 'IN THE WORKSHOP · ended ' + (job.ended ? fmtAgo(job.ended, now) + ' ago' : 'a while ago'))
    + (needs ? ' · ' + needs : '');
}

/** Does a job that has ended fall short of what it was handed? */
export function jobShort(job) {
  var needs = (job && job.report && job.report.needs) || [];
  return !!job && !job.running && needs.some(function (n) { return n.state !== 'done'; });
}

/** What a job was handed and what became of it, as rows to show under its
    line: [kind, text, state]. kind is 'k' (a heading), 'v' (text under
    it) or 'n' (a requirement, with its state). Whose words each is, is in
    the heading: the person's, the workshop's own record, or the worker's. */
export function jobRows(report) {
  var r = report || {}, rows = [];
  var add = function (k, v) { if (v) { rows.push(['k', k]); rows.push(['v', v]); } };
  add('ASKED', r.asked);
  add('WHERE', r.repo ? r.repo + (r.base_branch ? ' · from ' + r.base_branch : '')
    + (r.base ? ' @ ' + r.base : '') + (r.branch ? ' · on ' + r.branch : '') : '');
  (r.before || []).forEach(function (b, i) { add(i ? '' : 'SAID JUST BEFORE', b); });
  (r.context || []).forEach(function (c) { add('HANDED OVER WITH IT · ' + (c.from || 'the conversation'), c.text); });
  if ((r.needs || []).length) rows.push(['k', 'REQUIREMENTS · ' + needsLine(r) + ' · his own word on each']);
  (r.needs || []).forEach(function (n) {
    rows.push(['n', n.n + '. ' + n.text + (n.accept ? '\nDone when: ' + n.accept : '')
      + (n.note ? '\n' + n.note : ''), n.state || 'outstanding']);
  });
  add('CHANGED', (r.stat || '') + ((r.files || []).length ? (r.stat ? '\n' : '') + r.files.join('\n') : ''));
  var ran = (r.checks || []).map(function (c) {
    return (c.ok ? 'passed' : 'FAILED') + ' · ' + c.what + (c.ok || !c.said ? '' : ' (' + c.said + ')');
  }).concat((r.ran || []).map(function (c) {
    return (c.code === null || c.code === undefined ? 'did not finish' : 'exit ' + c.code) + ' · ' + c.cmd;
  }));
  if (r.looked === 'ok') ran.push('a second look at the change found nothing to put right');
  if (r.looked === 'found') ran.push('a second look found things to put right, and he went back to them');
  if (r.state && r.state !== 'running' && r.state !== 'queued' && r.kind !== 'look') {
    add('CHECKS AND WHAT WAS RUN', ran.join('\n') || 'nothing was run on the result');
  }
  add('WHAT WENT WRONG', r.error);
  add('HIS OWN ACCOUNT · not checked', r.summary);
  add('PULL REQUEST', r.pr);
  return rows;
}

/** The job in his workshop, in `host` (hidden when there is none): its
    line, what it was asked, and on a tap what it was handed and what became
    of it. Returns { paint(job) }. */
export function jobView(host) {
  var head = el('div', 'job-h'), what = el('div', 'job-t'), more = el('div', 'job-d'), open = false, last = '';
  host.classList.add('jobl');
  host.innerHTML = '';
  host.appendChild(head);
  host.appendChild(what);
  host.appendChild(more);
  host.hidden = true;
  more.hidden = true;
  host.addEventListener('click', function () {
    if (!more.childNodes.length) return;
    open = !open;
    more.hidden = !open;
    host.classList.toggle('open', open);
  });
  return {
    paint: function (job) {
      host.hidden = !job;
      if (!job) return;
      host.classList.toggle('on', !!job.running);
      host.classList.toggle('short', jobShort(job));
      var rows = job.report ? jobRows(job.report) : [];
      head.textContent = jobLine(job, Date.now() / 1000) + (rows.length ? (open ? ' · tap to close' : ' · tap for more') : '');
      what.textContent = job.what || '';
      var now = JSON.stringify(rows);
      if (now === last) return;          // nothing new: what is being read stays where it is
      last = now;
      more.innerHTML = '';
      rows.forEach(function (r) {
        var row = el('div', 'job-' + r[0] + (r[2] ? ' ' + r[2] : ''));
        if (r[0] === 'n') row.appendChild(el('span', 'job-s', String(r[2]).toUpperCase()));
        row.appendChild(el('span', '', r[1]));
        more.appendChild(row);
      });
      if (!rows.length) { open = false; more.hidden = true; host.classList.remove('open'); }
    }
  };
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

var CHAT_WHERE = { webui: 'chat page', phone: 'phone', deck: 'panel', voice: 'voice', chat: 'chat window' };

/** His conversations as rows, the one last spoken in first: its name, how
    much was said in it and when, and whether it is the one in front of him. */
export function chatRows(chats, now) {
  if (!chats || !Array.isArray(chats.list)) return [];
  return chats.list.map(function (c) {
    var n = c.turns || 0;
    return { id: c.id, title: c.title || '(no name yet)', here: c.id === chats.id,
             sub: n + (n === 1 ? ' exchange' : ' exchanges')
               + (c.updated ? ' · ' + fmtAgo(c.updated, now) + ' ago' : '')
               + (CHAT_WHERE[c.via] ? ' · ' + CHAT_WHERE[c.via] : '') };
  });
}

/** What goes beside the tab or the card's name: how many are kept. */
export function chatsCount(chats) {
  return chats ? (chats.kept || 0) + ' KEPT' : '';
}

/**
 * His conversations, in `host`: a new one, another put in front of him, a
 * name for one, one deleted (a hold).
 *   opts.post(path, body); opts.name(title, done): asks for a name and calls
 *   done(text), where the screen has a way to type one; opts.changed()
 * Returns { paint(chats) }: call it with every frame of his state.
 */
export function chatsView(host, opts) {
  var last = '', note = el('div', 'mem-note'), list = el('div', 'ch-list'), bar = el('div', 'ch-bar');
  var fresh = el('button', 'sw-b on ch-new', 'NEW CONVERSATION');
  fresh.type = 'button';
  host.classList.add('swv');
  host.innerHTML = '';
  bar.appendChild(fresh);
  bar.appendChild(note);
  host.appendChild(bar);
  host.appendChild(list);

  function ask(body, done) {
    note.textContent = '';
    opts.post('/api/douglas/chat', body).then(function (a) {
      note.classList.toggle('bad', !(a && a.ok));
      note.textContent = a && a.ok ? done : (a && (a.message || a.text)) || 'node1 did not answer';
      if (a && a.ok && opts.changed) opts.changed();
    });
  }
  fresh.addEventListener('click', function () { ask({ op: 'new' }, 'A new one is in front of him.'); });

  function row(r) {
    var line = el('div', 'sw ch' + (r.here ? ' here' : ''));
    var words = el('div', 'sw-w');
    words.appendChild(el('b', '', r.title));
    words.appendChild(el('small', '', r.sub));
    line.appendChild(words);
    if (r.here) {
      line.appendChild(el('span', 'sw-b none ch-b', 'IN FRONT OF HIM'));
    } else {
      var open = el('button', 'sw-b on ch-b', 'OPEN');
      open.type = 'button';
      open.setAttribute('aria-label', 'open ' + r.title);
      open.addEventListener('click', function () { ask({ op: 'switch', id: r.id }, 'It is in front of him.'); });
      line.appendChild(open);
    }
    if (opts.name) {
      var name = el('button', 'sw-b ch-b', 'NAME');
      name.type = 'button';
      name.addEventListener('click', function () {
        opts.name(r.title, function (text) {
          if (text && text.trim()) ask({ op: 'rename', id: r.id, title: text }, 'Named.');
        });
      });
      line.appendChild(name);
    }
    // Opening one is a touch; deleting one takes a hold.
    var gone = el('button', 'sw-b off hold-btn ch-b');
    gone.type = 'button';
    gone.setAttribute('aria-label', 'hold to delete ' + r.title);
    gone.appendChild(el('i', 'hold'));
    gone.appendChild(el('span', '', 'HOLD · DELETE'));
    wireHold(gone, function () { ask({ op: 'delete', id: r.id }, 'Deleted.'); });
    line.appendChild(gone);
    return line;
  }

  return {
    paint: function (chats) {
      // Not the times: a list drawn again every second would let go of a hold.
      var key = JSON.stringify(chats ? [chats.id, chats.list.map(function (c) { return [c.id, c.title, c.turns]; })] : null);
      if (key === last) return;
      last = key;
      list.innerHTML = '';
      fresh.hidden = !chats;
      var rows = chatRows(chats, Date.now() / 1000);
      if (!chats) {
        list.appendChild(el('div', 'mem-empty', 'His conversations are not kept: the voice pipeline on Douglas ' +
          'is not answering, is older than this, or has VOICE_CHATS_DIR set to "-".'));
      } else if (!rows.length) {
        list.appendChild(el('div', 'mem-empty', 'None is kept yet. One is, from the first thing said in it.'));
      }
      rows.forEach(function (r) { list.appendChild(row(r)); });
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
