/* ask.ts — a question on its way to Douglas, and what the screen says of it.

   The link from a phone to node1 comes and goes: a minute or two at a time
   when the phone changes access point, which is exactly when someone has
   walked off to photograph something. So "no answer" is not the end of a
   question. It waits, and goes by itself on the first poll that says node1
   is back and Douglas is free. It carries an id, so a copy that did arrive,
   with only its reply lost on the way back, is not asked twice.

   This is the page's paintAsk() and ready(), as functions of what is known.
   The app always has its own microphone, so the big button is about what
   the phone is doing; the smaller one borrows DougVoice's ears in the house
   and shows the pipeline's observed state. */

import { clock } from './format';

export const ASK_GIVE_UP_MS = 5 * 60 * 1000;
export const ASK_RETRY_MS = 2500;
export const SHOT_EDGE = 1280;

export type AskKind = 'text' | 'photo' | 'voice';
export type Ask = { body: Record<string, unknown>; kind: AskKind; since: number; flying: boolean; last: number;
                    apart?: boolean };      // not what is in the ask box: the box is left alone when it has gone

const VOICE_SUB: Record<string, string> = { idle: 'READY', listening: 'LISTENING…', thinking: 'THINKING…',
                                            speaking: 'SPEAKING…' };

/** "q-" and 24 hex digits, from 12 random bytes. */
export function newId(bytes: Uint8Array | number[]): string {
  let s = '';
  for (let j = 0; j < 12; j++) s += ('0' + ((bytes[j] || 0) & 255).toString(16)).slice(-2);
  return 'q-' + s;
}

/** The longest side a photo is sent at: 1280, or more where Douglas has
    said he can make use of it, and never past 4096. */
export function shotEdge(asked: unknown): number {
  return Math.min(4096, Math.max(SHOT_EDGE, +(asked as number) || 0));
}

export type Known = {
  polled: boolean;             // has a poll come back yet, with a frame or without?
  d: any;                      // the frame on screen; null when node1 gave none
  misses: number;              // polls in a row that got no answer
  late: boolean;               // a poll has been out for seconds: the link is slow
};

/** Can a question go to Douglas this very moment? Before the first poll is
    back nothing is known, so it is worth a try. */
export function ready(k: Known): boolean {
  const d = k.d || {}, st = (d.voice || {}).state;
  return !k.polled || (!!d.agent && !k.misses && !k.late && (st === 'idle' || st === 'speaking'));
}

/** Is anything going on that is worth polling often for? */
export function busy(k: Known, c: { pressed: boolean; ask: boolean; awaiting: boolean }): boolean {
  const st = k.d && k.d.voice && k.d.voice.state;
  return c.pressed || c.ask || c.awaiting || st === 'listening' || st === 'thinking' || st === 'speaking';
}

export type AskScreen = Known & {
  ask: Pick<Ask, 'kind' | 'since' | 'flying' | 'last'> | null;
  rec: { live: boolean; started: number } | null;
  speaking: boolean;
  awaiting: boolean;
  hasShot: boolean;
  housePressed: boolean;       // the DougVoice button is held
  houseRecent: boolean;        // and was, within the last half minute
  now: number;                 // milliseconds
};

export type AskView = {
  ptt: { state: string; sub: string };
  house: { state: string; sub: string; disabled: boolean };
  go: 'SEND' | 'STOP';
  locked: boolean;
  placeholder: string;
  note: string;
};

export function askView(c: AskScreen): AskView {
  const d = c.d || {};
  const agent = !!d.agent, voice = d.voice || {};
  const vstate: string = agent && VOICE_SUB[voice.state] ? voice.state : 'absent';

  // This phone's own microphone: the button is about what the phone is
  // doing, and is never out of reach.
  const mine = c.rec ? (c.rec.live ? 'listening' : 'idle') : c.speaking ? 'speaking'
    : (c.ask && c.ask.kind === 'voice') || c.awaiting ? 'thinking' : 'idle';
  const ptt = { state: mine, sub: c.rec ? (c.rec.live ? 'LISTENING…' : 'STARTING…')
    : c.speaking ? 'SPEAKING…' : mine === 'thinking' ? 'THINKING…' : 'READY' };

  // DougVoice's ears, in the house: its colour is the pipeline's observed
  // state, never the press.
  const house = {
    state: c.polled ? vstate : 'absent',
    sub: !c.polled ? '—' : !c.d ? 'NO LINK' : !agent ? 'NO AGENT'
      : vstate === 'absent' ? 'NO VOICE PIPELINE' : VOICE_SUB[vstate],
    disabled: !c.polled || (vstate === 'absent' && !c.housePressed),
  };

  // What stands between a question and Douglas, if anything. Before the
  // first poll is back nothing is known, and nothing is claimed.
  const link = c.polled && (!c.d || c.misses > 0 || c.late);
  const why = !c.polled ? ''
    : !c.d ? 'node1 is not answering'
    : !agent ? 'Douglas is unreachable'
    : vstate === 'absent' ? 'no voice pipeline on Douglas'
    : vstate === 'thinking' ? 'Douglas is thinking…'
    : vstate === 'listening' ? 'Douglas is listening…' : '';
  const sending = !c.ask ? '' : c.ask.kind === 'photo' ? 'Sending the photo…'
    : c.ask.kind === 'voice' ? 'Sending what you said…' : 'Sending…';
  const placeholder = c.ask ? (c.ask.flying ? sending : 'Waiting to send…')
    : why || (c.hasShot ? 'Ask about the photo…' : 'Ask Douglas…');

  let note = '';
  if (c.rec) {
    note = c.rec.live
      ? 'Listening through this phone. Let go when you have finished. ' + clock(c.now - c.rec.started)
      : 'Starting the microphone…';
  } else if (c.ask && c.ask.flying) {
    const secs = Math.round((c.now - c.ask.last) / 1000);
    note = sending + (secs >= 3 ? ' ' + secs + ' s' : '');
  } else if (c.ask) {
    const waited = clock(c.now - c.ask.since);
    note = vstate === 'thinking' || vstate === 'listening'
      ? 'Douglas is busy. Your question goes next. ' + waited
      : link ? 'No answer from node1 yet. Your question will go by itself when the link ' +
               'is back: keep the app open. ' + waited
      : why ? why + '. Your question will go by itself when that clears: keep the app open. ' + waited
      : sending;
  } else {
    // What DougVoice made of the last press, while it is still news. A
    // press that gets no answer is otherwise indistinguishable from a
    // Douglas who has stopped working.
    const heard = voice.heard;
    let deaf = '';
    if (c.speaking) {
      deaf = 'Douglas is speaking. Hold the button to cut in.';
    } else if (c.awaiting && !why) {
      deaf = 'Fetching his reply…';
    } else if (c.houseRecent && heard && heard.words === 0 && d.ts - heard.ts < 20) {
      deaf = 'DougVoice heard nothing in ' + heard.seconds + ' s' +
        (heard.level_db < -45 ? ': the room around it was quiet' : '') +
        '. It listens through its own microphone, not this phone\'s.';
    }
    note = !c.d && c.polled
        ? 'node1 is not answering. SEND still works: your question waits until the link is back.'
      : !agent && d.why ? 'Douglas is unreachable: ' + d.why
      : why || deaf || (c.misses || c.late ? 'node1 is slow to answer' : '');
  }
  return { ptt, house, go: c.ask ? 'STOP' : 'SEND', locked: !!c.ask, placeholder, note };
}

/** The six meters under his name, as [label, value or null, unit]. */
export function meters(d: any): [string, string | null, string][] {
  if (!d || !d.agent) return [];
  const host = d.host || {}, gpu = d.gpu || {}, llm = d.llm || {};
  const fix = (v: unknown, dp: number) => (typeof v === 'number' && isFinite(v) ? v.toFixed(dp) : null);
  const G = 1073741824;
  const used = (gpu.vram_used_bytes || 0) + (gpu.gtt_used_bytes || 0);
  const tot = (gpu.vram_total_bytes || 0) + (gpu.gtt_total_bytes || 0);
  return [
    ['GPU', fix(gpu.busy_pct, 0), '%'],
    ['MEMORY', tot ? fix(used / G, 0) : null, tot ? '/' + Math.round(tot / G) + 'G' : 'G'],
    ['TOK/S', fix(llm.gen_tps, 1), ''],
    ['CPU', fix(host.cpu_pct, 0), '%'],
    ['TEMP', fix(host.temp_c, 0), '°C'],
    ['POWER', fix(host.power_w, 0), 'W'],
  ];
}

/** The two lines over the meters: whether he is up, and which model. */
export function douglasHead(dgl: any): { pill: string; cls: 'ok' | 'bad'; sub: string; subCls: '' | 'ok' | 'busy' } {
  const g = dgl || {}, m = g.model || {};
  return {
    pill: g.up ? 'ONLINE' : 'OFFLINE',
    cls: g.up ? 'ok' : 'bad',
    sub: !g.up ? 'unreachable at ' + (g.host || 'douglas')
      : m.state === 'running' ? (m.name || 'model serving')
      : m.state === 'loading' ? 'loading model…' : 'model server down',
    subCls: g.up && m.state === 'running' ? 'ok' : g.up && m.state === 'loading' ? 'busy' : '',
  };
}
