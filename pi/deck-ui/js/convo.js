/* convo.js — Douglas's conversation log, scrollable back through the record.

   One log for both screens. The panel's DOUGLAS surface and the phone page
   each hand it an element, their own way of asking deck-api (the phone
   carries a token, the panel does not) and their own way of drawing a line.
   What /api/douglas carries is only the last few lines; scrolled to the top,
   or with EARLIER touched, the log asks for the lines before the oldest it
   shows and puts them above.

   The screens repaint every couple of seconds. A repaint keeps the earlier
   lines, and keeps the line the person was reading where it was: only a log
   already at the bottom follows a new line down. */

var PAGE = 30;
var ROUTE = '/api/douglas/transcript';

/* ── the pure part ───────────────────────────────────────────────────────── */

// Who said a line, as its name is shown. A line the user side of the record
// carries that came through ChatGPT was said by ChatGPT, not by the owner.
export function whoLabel(t) {
  if (!t || t.who !== 'user') return 'DOUGLAS';
  return t.via === 'chatgpt' ? 'CHATGPT' : 'YOU';
}

export function turnKey(t) {
  return (t && t.ts) + '|' + (t && t.who) + '|' + (t && t.text);
}

// Older and newer lines as one, oldest first, each line once. The two
// overlap where a page fetched from the record meets what /status carries.
export function mergeTurns(older, newer) {
  var seen = {}, out = [];
  (older || []).concat(newer || []).forEach(function (t, i) {
    var k = turnKey(t);
    if (!t || seen[k]) return;
    seen[k] = true;
    out.push({ t: t, i: i });
  });
  // A line without a time stays where it came, after the timed ones.
  function at(x) { return typeof x.t.ts === 'number' && isFinite(x.t.ts) ? x.t.ts : Infinity; }
  out.sort(function (a, b) { return at(a) - at(b) || a.i - b.i; });
  return out.map(function (x) { return x.t; });
}

export function oldestTs(turns) {
  for (var i = 0; i < (turns || []).length; i++) {
    var ts = turns[i] && turns[i].ts;
    if (typeof ts === 'number' && isFinite(ts)) return ts;
  }
  return null;
}

// Whether the log is at its newest line, give or take a finger's slack, and
// so should follow the next one down.
export function atBottom(scrollTop, scrollHeight, clientHeight, slack) {
  return scrollHeight - clientHeight - scrollTop <= (slack == null ? 24 : slack);
}

export function earlierPath(before, limit) {
  return ROUTE + '?before=' + encodeURIComponent(before) + '&limit=' + (limit || PAGE);
}

/* ── the log ─────────────────────────────────────────────────────────────── */

// opts: get(path) -> Promise of JSON or null; line(turn) -> Element;
// empty(turns === null) -> the words for an empty log, or '' for none;
// rowClass: the class of the EARLIER row.
export function convoLog(el, opts) {
  var older = [], recent = null, shown = [];
  var more = true, loading = false, failed = false, stuck = true, key = '';

  el.addEventListener('scroll', function () {
    stuck = atBottom(el.scrollTop, el.scrollHeight, el.clientHeight);
    if (el.scrollTop <= 4 && shown.length) earlier();
  });

  function earlier() {
    var before = oldestTs(shown);
    if (loading || !more || before == null) return;
    loading = true;
    failed = false;
    draw();
    opts.get(earlierPath(before)).then(function (r) {
      loading = false;
      if (r && r.ok && Array.isArray(r.lines)) {
        older = mergeTurns(r.lines, older);
        // An answer with nothing in it ends the asking even if it says more
        // remains: asking again with the same time would get the same nothing.
        more = !!r.more && r.lines.length > 0;
      } else {
        failed = true;
      }
      draw();
    });
  }

  // The first line in view, and how far below the log's top it sits, so a
  // rebuild can put it back where it was.
  function anchor() {
    var top = el.getBoundingClientRect().top;
    for (var c = el.firstChild; c; c = c.nextSibling) {
      if (!c.dataset || !c.dataset.k) continue;
      var r = c.getBoundingClientRect();
      if (r.bottom > top) return { k: c.dataset.k, off: r.top - top };
    }
    return null;
  }

  function draw() {
    // Earlier lines, once there are any, keep every line seen since: what
    // /status carries slides on, and a line slid out of it must not leave a
    // hole between the earlier ones and the newest.
    shown = recent === null ? older
      : older.length ? (older = mergeTurns(older, recent)) : recent;
    var k = recent === null ? 'none' : shown.length + ':' + oldestTs(shown) + ':' +
      (shown.length ? shown[shown.length - 1].ts : 0) + ':' + more + loading + failed;
    if (k === key) return false;               // nothing new: leave the scroll alone
    key = k;
    var a = stuck ? null : anchor();
    el.innerHTML = '';
    if (recent === null || !shown.length) {
      var words = opts.empty(recent === null);
      if (words) {
        var e = document.createElement('div');
        e.className = opts.emptyClass || 'empty';
        e.textContent = words;
        el.appendChild(e);
      }
      return true;
    }
    var row = document.createElement('div');
    row.className = opts.rowClass;
    row.textContent = loading ? 'LOADING…' : failed ? 'EARLIER DID NOT LOAD · TOUCH TO TRY AGAIN'
      : more ? 'EARLIER' : 'THE BEGINNING OF THE RECORD';
    row.classList.toggle('end', !more && !failed);
    if (more && !loading) row.onclick = earlier;
    el.appendChild(row);
    shown.forEach(function (t) {
      var div = opts.line(t);
      div.dataset.k = turnKey(t);
      el.appendChild(div);
    });
    if (!a) { el.scrollTop = el.scrollHeight; return true; }
    var top = el.getBoundingClientRect().top;
    for (var c = el.firstChild; c; c = c.nextSibling) {
      if (c.dataset && c.dataset.k === a.k) {
        el.scrollTop += c.getBoundingClientRect().top - top - a.off;
        break;
      }
    }
    return true;
  }

  return {
    // Draws the lines /api/douglas carries (null: the agent did not answer).
    // True when the log was drawn again, false when nothing had changed.
    paint: function (turns) { recent = turns; return draw(); },
    // A picture that has just loaded made the log taller: one that was
    // following the newest line goes on following it.
    follow: function () { if (stuck) el.scrollTop = el.scrollHeight; },
    toBottom: function () { stuck = true; el.scrollTop = el.scrollHeight; },
  };
}
