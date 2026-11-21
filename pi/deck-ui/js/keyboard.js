/* keyboard.js — an on-screen keyboard for a panel that has none.

   The DOUGLAS surface's ASK bar opens it over the whole surface; SEND hands
   the text to the caller and closes it, CLOSE keeps it as a draft. Keys
   fire on pointer-down (wireTap), because this panel's Chromium speaks
   pointer events and a key that waits for release feels broken. Backspace
   repeats while held, like every real keyboard.

   One instance, built on first open, so a page that never asks costs no
   DOM at all. */

import { $, wireTap } from './ui.js';

var ROWS = [
  ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0', 'BKSP'],
  ['q', 'w', 'e', 'r', 't', 'y', 'u', 'i', 'o', 'p', "'"],
  ['a', 's', 'd', 'f', 'g', 'h', 'j', 'k', 'l', '?', '!'],
  ['SHIFT', 'z', 'x', 'c', 'v', 'b', 'n', 'm', ',', '.', '-'],
  ['CLOSE', 'SPACE', 'SEND'],
];
var LABEL = { BKSP: '⌫', SHIFT: '⇧', SPACE: 'space', CLOSE: 'CLOSE', SEND: 'SEND' };
var MAX = 2000;

var root = null, text = '', shift = false, onSend = null, busy = false;

function build() {
  root = $('kbd');
  root.innerHTML =
    '<div class="kbd-head"><span class="kbd-title" id="kbd-title"></span>' +
    '<span class="kbd-hint" id="kbd-hint"></span></div>' +
    '<div class="kbd-text" id="kbd-text"></div>' +
    '<div class="kbd-keys" id="kbd-keys"></div>';
  var keys = $('kbd-keys');
  ROWS.forEach(function (row) {
    var r = document.createElement('div');
    r.className = 'kbd-row';
    row.forEach(function (k) {
      var b = document.createElement('button');
      b.className = 'kbd-key' + (LABEL[k] ? ' kbd-' + k.toLowerCase() : '');
      b.dataset.k = k;
      b.textContent = LABEL[k] || k;
      if (k === 'BKSP') wireRepeat(b, backspace);
      else wireTap(b, function () { press(k); });
      r.appendChild(b);
    });
    keys.appendChild(r);
  });
}

/* Backspace: one delete on press, then repeating after a beat while held. */
function wireRepeat(b, fn) {
  var t1 = null, t2 = null;
  function stop() { clearTimeout(t1); clearInterval(t2); t1 = t2 = null;
                    b.classList.remove('tapped'); }
  b.addEventListener('pointerdown', function (e) {
    e.preventDefault();
    b.classList.add('tapped');
    fn();
    t1 = setTimeout(function () { t2 = setInterval(fn, 70); }, 450);
  });
  ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (ev) {
    b.addEventListener(ev, stop);
  });
}

function backspace() { text = text.slice(0, -1); paint(); }

function press(k) {
  if (busy) return;
  if (k === 'CLOSE') { close(); return; }
  if (k === 'SEND') { send(); return; }
  if (k === 'SHIFT') { shift = !shift; paint(); return; }
  if (text.length >= MAX) return;
  var ch = k === 'SPACE' ? ' ' : k;
  // Capitals: SHIFT for one key, or automatically at the start of a sentence.
  if (/^[a-z]$/.test(ch) && (shift || /(^|[.?!]\s+)$/.test(text))) ch = ch.toUpperCase();
  shift = false;
  text += ch;
  paint();
}

function paint() {
  $('kbd-text').textContent = text;
  $('kbd-text').classList.toggle('empty', !text);
  root.querySelector('.kbd-shift').classList.toggle('on', shift);
  root.querySelector('.kbd-send').disabled = busy || !text.trim();
}

function hint(msg, bad) {
  var h = $('kbd-hint');
  h.textContent = msg || '';
  h.classList.toggle('bad', !!bad);
}

function send() {
  var t = text.trim();
  if (!t || busy || !onSend) return;
  busy = true;
  hint('sending…');
  paint();
  onSend(t).then(function (err) {
    busy = false;
    if (err) { hint(err, true); paint(); return; }   // keep the text to retry
    text = '';
    close();
  });
}

/**
 * Open the keyboard.
 *   title  — what it is for, shown in its header
 *   send   — function(text) -> Promise resolving to null on success, or an
 *            error message to show (the text is kept so it can be resent)
 */
export function openKeyboard(title, send) {
  if (!root) build();
  onSend = send;
  shift = false;
  busy = false;
  $('kbd-title').textContent = title;
  hint('');
  root.hidden = false;
  paint();
}

export function closeKeyboard() { close(); }

function close() {
  if (root) root.hidden = true;
}

export function keyboardOpen() { return !!root && !root.hidden; }
