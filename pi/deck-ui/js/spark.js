/* spark.js — the rolling-window traces and the stat tiles above them.

   Draws from the series buffer it is handed rather than reaching for a global,
   so the same code renders a live panel and a scripted one. */

import { $ } from './ui.js';
import { fmtVal, fmtAgo } from './format.js';
import { WINDOW_S } from './series.js';

/* ── sparkline ────────────────────────────────────────────────────────── */

export function drawSpark(svg, series) {
  // The stat tiles put data-key on the .stat div, not on their svg — so the
  // svg's own key is undefined there and this looked up series.points(undefined),
  // which is why every stat spark has read "no data — exporter not
  // answering" since the day it shipped, healthy exporter or not. Fall back
  // to the parent's key.
  var key = svg.dataset.key ||
    (svg.parentNode && svg.parentNode.dataset && svg.parentNode.dataset.key);
  var data = series.points(key) || [];
  var vb = svg.viewBox.baseVal;
  var W = vb.width, H = vb.height;
  var wide = svg.classList.contains('wide');
  var padL = wide ? 34 : 0, padT = wide ? 16 : 4, padB = wide ? 6 : 4;
  var padR = wide ? 62 : 4;

  var pts = data.filter(function (p) { return p.v !== null; });
  if (!pts.length) {
    svg.innerHTML = '<text x="' + (W / 2) + '" y="' + (H / 2 + 4) +
      '" text-anchor="middle">no data — exporter not answering</text>';
    return;
  }

  // x always spans the full window, so a gap reads as a gap rather than
  // the remaining data stretching to fill the box.
  var now = Date.now() / 1000;
  var t0 = now - WINDOW_S;

  var lo = svg.dataset.lo !== undefined ? Number(svg.dataset.lo) : null;
  var hi = svg.dataset.hi !== undefined ? Number(svg.dataset.hi) : null;
  if (lo === null || hi === null) {
    var vs = pts.map(function (p) { return p.v; });
    lo = Math.min.apply(null, vs);
    hi = Math.max.apply(null, vs);
    var span = Number(svg.dataset.minSpan || 1);
    if (hi - lo < span) {                    // don't dramatise flat data
      var mid = (hi + lo) / 2;
      lo = mid - span / 2; hi = mid + span / 2;
    }
    var pad = (hi - lo) * 0.12;
    lo -= pad; hi += pad;
  }

  var X = function (t) { return padL + (W - padL - padR) * Math.max(0, Math.min(1, (t - t0) / WINDOW_S)); };
  var Y = function (v) { return padT + (H - padT - padB) * (1 - (v - lo) / (hi - lo || 1)); };

  // Split into contiguous runs so gaps break the line.
  var runs = [], cur = [];
  data.forEach(function (p) {
    if (p.v === null) { if (cur.length) { runs.push(cur); cur = []; } }
    else cur.push(p);
  });
  if (cur.length) runs.push(cur);

  var out = [];
  if (wide) {
    [0.5, 0].forEach(function (f) {
      var y = padT + (H - padT - padB) * f;
      out.push('<path class="sk-g" d="M' + padL + ' ' + y.toFixed(1) +
               'H' + (W - padR) + '"/>');
    });
    out.push('<text x="0" y="' + (padT + 10) + '">' + Math.round(hi) + '</text>');
    out.push('<text x="0" y="' + (H - padB) + '">' + Math.round(lo) + '</text>');
  }

  runs.forEach(function (run) {
    var d = run.map(function (p, i) {
      return (i ? 'L' : 'M') + X(p.t).toFixed(1) + ' ' + Y(p.v).toFixed(1);
    }).join('');
    if (run.length > 1) {
      var base = (H - padB).toFixed(1);
      out.push('<path class="sk-a" d="' + d +
               'L' + X(run[run.length - 1].t).toFixed(1) + ' ' + base +
               'L' + X(run[0].t).toFixed(1) + ' ' + base + 'Z"/>');
    }
    out.push('<path class="sk-l" d="' + d + '"/>');
  });

  var last = pts[pts.length - 1];
  var fresh = (now - last.t) < 30;
  if (fresh) {
    out.push('<circle class="sk-d" cx="' + X(last.t).toFixed(1) +
             '" cy="' + Y(last.v).toFixed(1) + '" r="' + (wide ? 4 : 3) + '"/>');
  }
  if (wide) {
    out.push('<text class="now" x="' + (W - padR + 8) + '" y="' +
             Y(last.v).toFixed(1) + '" dy="6">' +
             fmtVal(last.v, svg.dataset.unit) + '</text>');
  }

  svg.innerHTML = out.join('');
}

export function drawAllSparks(series) {
  [].forEach.call(document.querySelectorAll('svg.spark'),
                function (svg) { drawSpark(svg, series); });

  // A stale number in primary type is a lie readable from across the room —
  // and this fires exactly when the workstation is down or rebooting, the one
  // moment someone actually looks. Stale (> ~3 scrape intervals) dims the
  // value AND stamps its age beside it in the same glance; a value older
  // than two minutes stops being shown at all.
  [].forEach.call(document.querySelectorAll('.stat'), function (el) {
    var p = series.latest(el.dataset.key);
    var v = el.querySelector('.v');
    var age = p ? Date.now() / 1000 - p.t : Infinity;
    var stale = age > 15;
    v.className = 'v' + (stale ? ' stale' : '');
    if (!p || age > 120) {
      v.innerHTML = '—';
    } else {
      v.innerHTML =
        (Math.abs(p.v) >= 100 ? Math.round(p.v) : Math.round(p.v * 10) / 10) +
        '<span class="u">' + (el.dataset.unit || '') + '</span>' +
        (stale ? '<span class="age">· ' + fmtAgo(p.t) + ' ago</span>' : '');
    }
  });

  var mins = Math.round(WINDOW_S / 60);
  $('win').textContent = mins + ' min · in memory';
}
