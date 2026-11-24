/* memory.js — what Douglas remembers, for the owner to read and put right.

   One view for both screens. The panel's DOUGLAS surface and the phone page
   each hand it an element, their own way of asking deck-api (the phone
   carries a token, the panel does not) and, where the screen has no
   keyboard, their own way of typing. Everything else is the same on both.

   Nothing here remembers anything: every line is what his memory holds, as
   it gave it, and a memory that could not be read is said so, never shown
   as an empty one.

   The memory is his to use and the owner's to keep. Douglas notes things
   and recalls them. Reading all of it, rewording a memory, bringing one
   back and removing one for good happen only here, through a door he has
   no key to. A deletion can be undone, so it takes a tap; a purge cannot,
   so it takes a hold, like everything else on this deck that cannot be
   taken back. */

import { wireHold } from './ui.js';
import { fmtAgo, fmtClock } from './format.js';

var HOW = { owner: 'you set this', said: 'you said so', observed: 'he saw it happen',
            inferred: 'he worked it out' };
var STATES = [['active', 'KEPT'], ['superseded', 'REPLACED'], ['deleted', 'DELETED']];
var KINDS = ['preference', 'fact', 'project', 'decision', 'task', 'lesson', 'fleet', 'summary'];
var WHO = { owner: 'you', douglas: 'him', memory: 'the memory itself' };
var PAGE = 30;
var ROUTE = '/api/douglas/memory';

function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function button(cls, text, fire) {
  var b = el('button', cls);
  b.type = 'button';
  b.appendChild(el('span', '', text));
  if (fire) b.addEventListener('click', fire);
  return b;
}

/** One earlier thing that happened to a memory: what, by whom, when, and
    what it said before where that is not what it says now. */
export function pastLine(h, m, now) {
  return (h.action === 'restored' ? 'brought back' : h.action) + ' by ' + (WHO[h.who] || h.who) +
    ' · ' + fmtAgo(h.at, now) + ' ago' +
    (h.before && h.before !== m.text ? ' · was: ' + h.before : '') +
    (h.action === 'kept over a weaker note' && h.after ? ' · the note: ' + h.after : '');
}

/** Where a memory came from and how far to trust it, in one line. */
export function provenance(m, now) {
  var parts = [HOW[m.source] || m.source || 'unknown'];
  if (m.via) parts.push(m.via);
  parts.push(fmtAgo(m.updated, now) + ' ago');
  parts.push('sure ' + Math.round((m.confidence || 0) * 100) + '%');
  if (m.uses) parts.push('used ' + m.uses + '×');
  if (m.done) parts.push('done ' + fmtAgo(m.done, now) + ' ago');
  return parts.join(' · ');
}

/**
 * The memory, in `host`.
 *   opts.get(path)        -> Promise of deck-api's JSON, or null for no answer
 *   opts.post(path, body) -> the same
 *   opts.type(title, text, save)
 *                         this screen's way of typing, where it has no
 *                         keyboard of its own: save(text) resolves to null,
 *                         or to what went wrong. Without it, a text box is
 *                         put in the page.
 *   opts.count            an element that says how much he remembers
 * Returns { refresh() }: look again, unless something is being typed.
 */
export function memoryView(host, opts) {
  var st = { status: 'active', kind: '', q: '', rows: [], total: null, open: 0,
             detail: null, editing: false, stats: null, seq: 0 };
  var bar = el('div', 'mem-bar'), states = el('div', 'mem-states'), kinds = el('div', 'mem-kinds');
  var list = el('div', 'mem-list');
  var more = button('mem-more', 'MORE', function () { load(true); });
  var note = el('div', 'mem-note');
  var find = null;
  host.classList.add('memv');
  host.innerHTML = '';

  if (!opts.type) {                       // a screen with a keyboard: a box to search in
    find = el('form', 'mem-find');
    var box = el('input');
    box.type = 'search';
    box.placeholder = 'Search what he remembers…';
    box.autocomplete = 'off';
    box.maxLength = 80;
    box.setAttribute('enterkeyhint', 'search');
    find.appendChild(box);
    find.addEventListener('submit', function (e) {
      e.preventDefault();
      st.q = box.value.trim();
      box.blur();
      load();
    });
    box.addEventListener('input', function () {
      if (!box.value && st.q) { st.q = ''; load(); }       // cleared with the ×
    });
    host.appendChild(find);
  }
  bar.appendChild(states);
  bar.appendChild(kinds);
  [bar, list, more, note].forEach(function (e) { host.appendChild(e); });
  note.setAttribute('role', 'status');

  function say(text, bad) {
    note.textContent = text || '';
    note.classList.toggle('bad', !!bad);
  }

  function ask(body) {
    return opts.post(ROUTE, body).then(function (r) {
      if (!r) say('node1 did not answer', true);
      else if (!r.ok) say(r.text || 'refused', true);
      return r && r.ok ? r : null;
    });
  }

  function chip(row, text, on, fire) {
    var c = button('mem-chip' + (on ? ' on' : ''), text, fire);
    row.appendChild(c);
    return c;
  }

  // Two rows of switches: which memories (kept, replaced, deleted), then
  // which kind, and only the kinds he has any of.
  function paintBar() {
    var by = (st.stats && st.stats.by_kind) || {};
    var at = kinds.scrollLeft;
    states.innerHTML = kinds.innerHTML = '';
    STATES.forEach(function (s) {
      var n = st.stats ? st.stats[s[0]] : null;
      chip(states, s[1] + (n ? ' ' + n : ''), st.status === s[0], function () {
        st.status = s[0]; st.open = 0; load();
      });
    });
    var have = KINDS.filter(function (k) { return by[k] || st.kind === k; });
    if (have.length > 1 || st.kind) {
      chip(kinds, 'ALL', !st.kind, function () { st.kind = ''; st.open = 0; load(); });
      have.forEach(function (k) {
        chip(kinds, k.toUpperCase() + (st.status === 'active' && by[k] ? ' ' + by[k] : ''),
             st.kind === k, function () { st.kind = st.kind === k ? '' : k; st.open = 0; load(); });
      });
    }
    kinds.scrollLeft = at;                // repainted, it stays where a thumb left it
    if (opts.type) {                      // no keyboard here: a chip opens one
      chip(states, st.q ? '“' + st.q + '” ×' : 'SEARCH', !!st.q, function () {
        if (st.q) { st.q = ''; load(); return; }
        opts.type('SEARCH WHAT HE REMEMBERS', '', function (text) {
          st.q = text.slice(0, 80);
          return load().then(function () { return null; });
        });
      });
    }
    if (opts.count) {
      opts.count.textContent = !st.stats ? '—'
        : st.stats.active + ' KEPT' + (st.stats.open_tasks ? ' · ' + st.stats.open_tasks + ' TO DO' : '');
    }
  }

  function acts(m) {
    var row = el('div', 'mem-acts');
    if (m.status === 'active') {
      row.appendChild(button('', 'CORRECT', function (e) { e.stopPropagation(); edit(m); }));
      if (m.kind === 'task' && !m.done) {
        row.appendChild(button('', 'DONE', function (e) {
          e.stopPropagation();
          ask({ op: 'correct', id: m.id, done: true }).then(function (r) {
            if (r) { say('Marked as done.'); refresh(true); }
          });
        }));
      }
      row.appendChild(button('warn', 'DELETE', function (e) {
        e.stopPropagation();
        ask({ op: 'delete', id: m.id }).then(function (r) {
          if (r) { say('Deleted. It is under DELETED, to bring back or to purge.'); refresh(true); }
        });
      }));
    } else {
      row.appendChild(button('', 'BRING BACK', function (e) {
        e.stopPropagation();
        ask({ op: 'restore', id: m.id }).then(function (r) {
          if (r) { say('Brought back.'); refresh(true); }
        });
      }));
      var purge = button('bad hold-btn', 'HOLD TO PURGE');
      purge.appendChild(el('i', 'hold'));
      purge.addEventListener('click', function (e) { e.stopPropagation(); });
      wireHold(purge, function () {
        ask({ op: 'purge', id: m.id }).then(function (r) {
          if (r) { say('Purged. It is gone from his memory for good.'); st.open = 0; refresh(true); }
        });
      });
      row.appendChild(purge);
    }
    return row;
  }

  function edit(m) {
    function save(text) {
      return opts.post(ROUTE, { op: 'correct', id: m.id, text: text }).then(function (r) {
        if (!r) return 'node1 did not answer';
        if (!r.ok) return r.text || 'refused';
        st.editing = false;
        say('Corrected. It is your word from now on.');
        refresh(true);
        return null;
      });
    }
    if (opts.type) { opts.type('CORRECT WHAT HE REMEMBERS', m.text, save); return; }
    st.editing = true;
    paint();
    var row = list.querySelector('[data-id="' + m.id + '"]');
    if (!row) { st.editing = false; return; }
    var area = el('textarea', 'mem-edit');
    area.value = m.text;
    area.maxLength = 600;
    area.rows = 4;
    area.addEventListener('click', function (e) { e.stopPropagation(); });
    var keep = el('div', 'mem-acts');
    keep.appendChild(button('go', 'SAVE', function (e) {
      e.stopPropagation();
      var text = area.value.trim();
      if (!text || text === m.text) { st.editing = false; paint(); return; }
      save(text).then(function (err) { if (err) say(err, true); });
    }));
    keep.appendChild(button('', 'CANCEL', function (e) {
      e.stopPropagation(); st.editing = false; paint();
    }));
    row.querySelector('.mem-t').replaceWith(area);
    row.querySelector('.mem-acts').replaceWith(keep);
    area.focus();
  }

  function paintRow(m, now) {
    var open = st.open === m.id;
    var row = el('div', 'mem' + (open ? ' open' : '') + (m.done ? ' done' : ''));
    row.dataset.id = m.id;
    var top = el('div', 'mem-top');
    top.appendChild(el('b', 'mem-k', (m.kind || '').toUpperCase()));
    top.appendChild(el('span', 'mem-s', m.subject || ''));
    row.appendChild(top);
    row.appendChild(el('div', 'mem-t', m.text || ''));
    row.appendChild(el('div', 'mem-p', provenance(m, now)));
    if (open) {
      var past = (st.detail && st.detail.id === m.id && st.detail.history) || [];
      past.slice(0, 4).forEach(function (h) {
        if (h.action === 'kept') return;
        row.appendChild(el('div', 'mem-h', pastLine(h, m, now)));
      });
      row.appendChild(acts(m));
    }
    row.addEventListener('click', function () {
      if (st.editing) return;
      st.open = open ? 0 : m.id;
      st.detail = null;
      paint();
      if (!open) {
        opts.get(ROUTE + '?op=get&id=' + m.id).then(function (d) {
          if (d && d.ok && st.open === m.id && !st.editing) { st.detail = d; paint(); }
        });
      }
    });
    return row;
  }

  function paint() {
    var now = Date.now() / 1000;
    paintBar();
    list.innerHTML = '';
    st.rows.forEach(function (m) { list.appendChild(paintRow(m, now)); });
    if (!st.rows.length && st.total !== null) {
      list.appendChild(el('div', 'mem-empty', st.q ? 'Nothing he remembers matches that.'
        : st.status === 'deleted' ? 'Nothing deleted.'
        : st.status === 'superseded' ? 'Nothing has been replaced by something newer.'
        : 'Nothing kept yet. He keeps what is worth keeping once a conversation has gone quiet, ' +
          'and at once when told to remember something.'));
    }
    more.hidden = !(st.total > st.rows.length);
    // What is said about a change is said where the eye is: in the row that
    // is open, and above the list when none is.
    var at = list.querySelector('.mem.open');
    if (at) at.appendChild(note);
    else host.insertBefore(note, list);
  }

  function load(further) {
    var mine = ++st.seq;
    var path = ROUTE + '?op=list&status=' + st.status + '&limit=' + PAGE +
      '&offset=' + (further ? st.rows.length : 0) + (st.kind ? '&kind=' + st.kind : '') +
      (st.q ? '&q=' + encodeURIComponent(st.q) : '');
    return opts.get(path).then(function (r) {
      if (mine !== st.seq) return;                  // a newer question is out
      if (!r || !r.ok) {
        // Not read is not empty: what was on screen stays, and it says so.
        say((r && r.text) || 'node1 did not answer', true);
        if (st.total === null) list.innerHTML = '';
        paintBar();
        return;
      }
      st.rows = further ? st.rows.concat(r.memories || []) : (r.memories || []);
      st.total = r.total || 0;
      if (note.classList.contains('bad')) say('');
      paint();
    });
  }

  function refresh(now) {
    if (st.editing && !now) return Promise.resolve();
    opts.get(ROUTE + '?op=stats').then(function (r) {
      st.stats = r && r.ok ? r : st.stats;
      paintBar();
    });
    return load();
  }

  // Only how much he remembers, for a screen that is not showing the list.
  function glance() {
    return opts.get(ROUTE + '?op=stats').then(function (r) {
      st.stats = r && r.ok ? r : st.stats;
      paintBar();
    });
  }

  paint();
  return { refresh: refresh, glance: glance };
}

/**
 * The record of what he did, newest first, in `host`: which tool, to do
 * what, on whose asking, whether it worked and how long it took. Written by
 * him as he goes, where he can add to it and neither read nor change it.
 * Returns { refresh() }.
 */
export function didView(host, opts) {
  host.classList.add('mem-did');
  function paint(r) {
    if (!r || !r.ok) {
      host.textContent = (r && r.text) || 'node1 did not answer';
      return;
    }
    host.innerHTML = '';
    (r.events || []).forEach(function (e) {
      var row = el('div', 'ev' + (e.ok ? '' : ' warn'));
      row.appendChild(el('time', '', fmtClock(e.at)));
      row.appendChild(el('b', '', e.name || e.kind || ''));
      var what = [e.action, e.ok ? e.detail : 'failed' + (e.detail ? ' (' + e.detail + ')' : ''),
                  e.ms ? (e.ms >= 1000 ? (e.ms / 1000).toFixed(1) + ' s' : e.ms + ' ms') : '', e.via]
        .filter(Boolean).join(' · ');
      row.appendChild(el('span', '', what));
      host.appendChild(row);
    });
    if (!host.children.length) host.textContent = 'nothing yet';
  }
  return {
    refresh: function () {
      return opts.get(ROUTE + '?op=events&limit=' + (opts.rows || 12)).then(paint);
    },
  };
}
