/* douglas.ts — talking to Douglas: his state, a question on its way, this
   phone's microphone and speaker, and DougVoice's ears in the house.

   A port of the Douglas half of pi/deck-ui/m/m.js. What is the same: the
   poll and its pacing, the queue (one question at a time, retried while the
   link is down, given up after five minutes with the words and photo still
   in the box), the ids that make a retry safe, and every word on the line
   under the box. What is not: the page's workarounds for a browser's
   microphone and speaker. Here the recording comes from the phone as 16-bit
   PCM, and the audio session is set to playback once it stops, so his reply
   comes out of the loudspeaker and not the earpiece. */

import { useCallback, useEffect, useRef, useState } from 'react';
import { Keyboard } from 'react-native';
import {
  createAudioPlayer, requestRecordingPermissionsAsync, setAudioModeAsync, useAudioStream,
} from 'expo-audio';
import type { AudioPlayer } from 'expo-audio';
import * as Crypto from 'expo-crypto';
import { File, Paths } from 'expo-file-system';
import * as Haptics from 'expo-haptics';

import { Deck, useDeck } from './deck';
import {
  Ask, AskKind, AskView, ASK_GIVE_UP_MS, ASK_RETRY_MS, Known, askView, busy, newId, ready, shotEdge, SHOT_EDGE,
} from './lib/ask';
import { int16Base64, joinInt16, REC_MAX_MS, REC_MIN_S, REC_RATE, resample, toMono } from './lib/pcm';
import type { Shot } from './photo';

type Rec = { live: boolean; started: number; chunks: ArrayBuffer[]; rate: number; channels: number;
             timer: ReturnType<typeof setTimeout> | null };
type Awaiting = { id: string; since: number; flying: boolean };
type Speaking = { player: AudioPlayer; file: File };

export type Douglas = {
  d: any;                                   // his frame, or null when node1 gave none
  view: AskView;
  bad: boolean;                             // the line under the box is a failure being read
  eyes: boolean;                            // has he said he can see?
  edge: number;                             // the long side a photo is sent at
  text: string;
  setText: (t: string) => void;
  shot: Shot | null;
  setShot: (s: Shot | null) => void;
  send: () => void;                         // SEND, or STOP while one is on its way
  // '' when taken, or why not. `apart` for one that did not come from the
  // ask box (the share sheet's): what is typed there stays when it has gone.
  queue: (body: Record<string, unknown>, kind: AskKind, apart?: boolean) => string;
  say: (msg: string, ms?: number) => void;  // a failure the person has to see
  refresh: () => void;
  pttIn: () => void;
  pttOut: (send: boolean) => void;
  houseIn: () => void;
  houseOut: (action: 'stop' | 'cancel') => void;
};

function playback(): Promise<void> {
  return setAudioModeAsync({ allowsRecording: false, playsInSilentMode: true, shouldRouteThroughEarpiece: false })
    .catch(() => { /* the phone decides for itself */ });
}

export function useDouglas(): Douglas {
  const { deck, active } = useDeck();
  const [, setTick] = useState(0);
  const bump = useCallback(() => setTick((n) => n + 1), []);
  const [text, setText] = useState('');
  const [shot, setShot] = useState<Shot | null>(null);

  const k = useRef<Known>({ polled: false, d: null, misses: 0, late: false }).current;
  const ask = useRef<Ask | null>(null);
  const rec = useRef<Rec | null>(null);
  const awaiting = useRef<Awaiting | null>(null);
  const speaking = useRef<Speaking | null>(null);
  const sticky = useRef<{ text: string; until: number } | null>(null);
  const sight = useRef({ eyes: false, edge: SHOT_EDGE });
  const pressed = useRef(false), housePressed = useRef(false), houseAt = useRef(0);
  const flying = useRef(0), again = useRef(false), epoch = useRef(0), lastTick = useRef(0);
  const box = useRef({ text, shot });
  box.current = { text, shot };
  // The poll wakes the queue and the queue wakes the poll: each reaches the
  // other through here, so neither holds an old copy of it.
  const wake = useRef({ trySend: () => {}, tick: (_fresh?: boolean) => {} });

  const { stream } = useAudioStream({
    sampleRate: REC_RATE, channels: 1, encoding: 'int16',
    onBuffer: (b) => {
      const r = rec.current;
      if (!r || !r.live) return;
      r.rate = b.sampleRate;
      r.channels = b.channels;
      r.chunks.push(b.data);
    },
  });

  // A failure the person has to see: red, and held for a few seconds so the
  // next poll does not wipe it before it has been read.
  const say = useCallback((msg: string, ms?: number) => {
    sticky.current = { text: msg, until: Date.now() + (ms || 5000) };
    bump();
  }, [bump]);

  /* ── the poll ─────────────────────────────────────────────────────────── */

  // One missed poll is the link, not Douglas: the frame on screen stays
  // through it, with a word that node1 is slow. Three in a row and that
  // frame is too old to stand behind.
  const got = useCallback((d: any) => {
    k.polled = true;
    k.late = false;
    k.misses = d ? 0 : k.misses + 1;
    if (d || k.misses >= 3 || !k.d) k.d = d || null;
    if (d && d.agent) {
      // The camera is offered once he has said he can see, and stays
      // through a lost link: a photo can be taken now and go when node1 is back.
      sight.current = { eyes: !!(d.voice || {}).eyes, edge: shotEdge((d.voice || {}).shot) };
    }
    wake.current.trySend();                     // a question may have been waiting for this
    bump();
  }, [bump, k]);

  const tick = useCallback((fresh?: boolean) => {
    lastTick.current = Date.now();
    if (flying.current) {
      if (fresh) again.current = true;
      // His status normally takes a tenth of a second. Still waiting on it
      // seconds later is a slow link, and worth saying before a deadline
      // makes it official.
      if (Date.now() - flying.current >= 2500 && !k.late) { k.late = true; bump(); }
      return;
    }
    const mine = epoch.current;
    flying.current = Date.now();
    deck.get('/api/douglas').then((d) => {
      if (mine !== epoch.current) return;       // the app slept meanwhile: stale
      flying.current = 0;
      got(d);
      if (again.current) { again.current = false; tick(); }
    });
  }, [deck, got, bump, k]);

  /* ── a question on its way ────────────────────────────────────────────── */

  const endAsk = () => { ask.current = null; };

  const trySend = useCallback(() => {
    const mine = ask.current;
    if (!mine || mine.flying) return;
    const now = Date.now();
    if (now - mine.since > ASK_GIVE_UP_MS) {
      endAsk();
      say('That could not be sent in five minutes. Your words and photo are still here: ' +
          'press SEND to try again.', 12000);
      return;
    }
    if (!ready(k) || now - mine.last < ASK_RETRY_MS) return;
    mine.flying = true;
    mine.last = now;
    // A photo gets longer: it is a few hundred kilobytes (a few megabytes,
    // where Douglas has asked for a bigger one) to push through whatever
    // link the phone has, and deck-api then gives Douglas twenty seconds to
    // take it.
    deck.post(mine.kind === 'voice' ? '/api/douglas/hear' : '/api/douglas/say', mine.body,
              mine.kind === 'text' ? 12000 : 45000).then((r) => {
      if (ask.current !== mine) return;         // stopped while it was out
      mine.flying = false;
      if (r && r.ok) {
        endAsk();
        if (mine.kind === 'voice') {
          awaitSpeech(String(mine.body.id));    // his reply comes back as sound
        } else if (!mine.apart) {
          setText('');
          setShot(null);
          Keyboard.dismiss();                   // so the reply is visible
        }
      } else if (r && !r.retry) {
        // Douglas or the deck said no, and asking again will not change
        // that. Nothing is thrown away: the words and the photo stay.
        endAsk();
        say(r.message || 'Douglas refused', 8000);
      }
      // Anything else is the link, or Douglas gone for a moment: the
      // question stays where it is and goes again when a poll says it can.
      bump();
      wake.current.tick(true);                  // shows our line now; 'thinking' speeds the poll
    });
    bump();
  }, [deck, say, bump, k]);
  wake.current = { trySend, tick };

  const queue = useCallback((body: Record<string, unknown>, kind: AskKind, apart?: boolean): string => {
    if (ask.current) return 'Your last one is still on its way. Press STOP to take it back.';
    sticky.current = null;                      // a new try: the last failure has been read
    ask.current = { body, kind, since: Date.now(), flying: false, last: 0, apart };
    trySend();
    bump();
    return '';
  }, [trySend, bump]);

  const send = useCallback(() => {
    if (ask.current) {                          // the button reads STOP
      const out = ask.current.flying;
      endAsk();
      if (out) say('Stopped. If it had already reached Douglas, his answer will still appear.', 6000);
      else bump();
      return;
    }
    const words = box.current.text.trim(), photo = box.current.shot;
    if (!words && !photo) return;               // a photo alone asks "what is this?"
    const body: Record<string, unknown> = { text: words, via: 'phone', id: newId(Crypto.getRandomBytes(12)) };
    if (photo) body.image = photo.b64;
    queue(body, photo ? 'photo' : 'text');
  }, [queue, say, bump]);

  /* ── this phone's own microphone and speaker ──────────────────────────── */

  const stopSpeaking = useCallback(() => {
    const was = speaking.current;
    if (!was) return;
    speaking.current = null;
    try { was.player.pause(); was.player.remove(); } catch { /* gone already */ }
    try { was.file.delete(); } catch { /* gone already */ }
  }, []);

  const playSpeech = useCallback(async (id: string, bytes: Uint8Array) => {
    stopSpeaking();
    await playback();
    try {
      const file = new File(Paths.cache, 'douglas-' + id + '.wav');
      if (file.exists) file.delete();
      file.create();
      file.write(bytes);
      const player = createAudioPlayer({ uri: file.uri });
      const mine: Speaking = { player, file };
      speaking.current = mine;
      player.addListener('playbackStatusUpdate', (st) => {
        if (speaking.current === mine && st.didJustFinish) { stopSpeaking(); bump(); }
      });
      player.play();
    } catch {
      stopSpeaking();
      say('This phone could not play his reply. It is in the log above.', 8000);
    }
    bump();
  }, [stopSpeaking, say, bump]);

  const endAwait = () => { awaiting.current = null; };

  // The recording has been taken: now his reply, as sound. It is asked for
  // once a second until it is ready, or until it is plain it never will be.
  function awaitSpeech(id: string) {
    awaiting.current = { id, since: Date.now(), flying: false };
  }

  const fetchSpeech = useCallback(() => {
    const mine = awaiting.current;
    if (!mine || mine.flying) return;
    if (Date.now() - mine.since > 120000) {
      endAwait();
      say('His reply never came back as sound. If he answered, it is in the log above.', 8000);
      return;
    }
    mine.flying = true;
    deck.speech(mine.id).then((r: any) => {
      mine.flying = false;                      // no answer is the link: ask again in a second
      if (awaiting.current !== mine || !r) return;   // a new press took over
      if (r.sound) {
        endAwait();
        playSpeech(mine.id, r.sound);
      } else if (r.state === 'nothing') {
        endAwait();
        say('Douglas did not catch any words in that. Hold the button and speak towards the phone.', 8000);
      } else if (r.state === 'failed' || r.state === 'unknown') {
        endAwait();
        say('He answered in the log above, but the sound of it did not come.', 8000);
      }
      bump();
    });
  }, [deck, playSpeech, say, bump]);

  const endRec = useCallback((sendIt: boolean) => {
    const r = rec.current;
    rec.current = null;
    if (!r) return;
    if (r.timer) clearTimeout(r.timer);
    try { stream.stop(); } catch { /* never started */ }
    // The microphone is let go, and the phone's sound is media again, for
    // the loudspeaker.
    playback();
    if (sendIt && r.live) {
      const pcm = resample(toMono(joinInt16(r.chunks), r.channels), r.rate, REC_RATE);
      if (pcm.length < REC_RATE * REC_MIN_S) {
        say('That was too short to hear. Hold the button for as long as you are speaking.', 6000);
      } else {
        queue({ audio: int16Base64(pcm), rate: REC_RATE, via: 'phone', id: newId(Crypto.getRandomBytes(12)) }, 'voice');
      }
    } else if (sendIt) {
      say('The microphone had not started yet. Hold the button, wait for LISTENING, then speak.', 6000);
    }
    bump();
  }, [stream, queue, say, bump]);

  const startRec = useCallback(async () => {
    stopSpeaking();
    const mine: Rec = { live: false, started: 0, chunks: [], rate: REC_RATE, channels: 1, timer: null };
    rec.current = mine;
    bump();
    try {
      const may = await requestRecordingPermissionsAsync();
      if (!may.granted) throw new Error('denied');
      await setAudioModeAsync({ allowsRecording: true, playsInSilentMode: true });
      if (rec.current !== mine) { playback(); return; }     // let go before it came
      await stream.start();
      if (rec.current !== mine) {
        try { stream.stop(); } catch { /* not running */ }
        playback();
        return;
      }
      mine.live = true;
      mine.started = Date.now();
      mine.timer = setTimeout(() => { if (rec.current === mine) { pressed.current = false; endRec(true); } }, REC_MAX_MS);
    } catch (e) {
      if (rec.current === mine) rec.current = null;
      playback();
      say(e instanceof Error && e.message === 'denied'
        ? 'This app is not allowed the microphone. Allow it in Settings, under Doug, then hold the button again.'
        : 'The microphone would not start on this phone.', 9000);
    }
    bump();
  }, [stream, stopSpeaking, endRec, say, bump]);

  const pttIn = useCallback(() => {
    if (pressed.current) return;
    if (ask.current) {                          // one thing on its way at a time
      say('Your last one is still on its way. Press STOP to take it back.', 5000);
      return;
    }
    pressed.current = true;
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light).catch(() => {});
    endAwait();
    startRec();
  }, [startRec, say]);

  const pttOut = useCallback((sendIt: boolean) => {
    if (!pressed.current) return;
    pressed.current = false;
    endRec(sendIt);
  }, [endRec]);

  /* ── DougVoice's ears, in the house ───────────────────────────────────── */

  const houseSend = useCallback((action: string) => {
    deck.post('/api/douglas/ptt', { action }, 8000).then((r) => {
      if (!(r && r.ok)) say(r ? (r.message || 'Douglas refused') : 'No answer from node1');
    });
  }, [deck, say]);

  const houseIn = useCallback(() => {
    if (housePressed.current) return;
    housePressed.current = true;
    houseAt.current = Date.now();
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light).catch(() => {});
    houseSend('start');
    bump();
  }, [houseSend, bump]);

  const houseOut = useCallback((action: 'stop' | 'cancel') => {
    if (!housePressed.current) return;
    housePressed.current = false;
    houseAt.current = Date.now();
    houseSend(action);
    bump();
  }, [houseSend, bump]);

  /* ── the clock all of it runs on ──────────────────────────────────────── */

  useEffect(() => {
    if (!active) return;
    tick();
    const id = setInterval(() => {
      const now = Date.now();
      const often = busy(k, { pressed: pressed.current || housePressed.current, ask: !!ask.current,
                              awaiting: !!awaiting.current });
      if (often || now - lastTick.current >= 3000) tick();
      trySend();
      fetchSpeech();
      // Counts the seconds on the line under the box, and lets go of a
      // failure once it has been read.
      let draw = !!(ask.current || rec.current || awaiting.current);
      if (sticky.current && now >= sticky.current.until) { sticky.current = null; draw = true; }
      if (draw) bump();
    }, 700);
    return () => {
      clearInterval(id);
      // The app is about to sleep: a recording does not outlive it, and an
      // answer still in flight must not be painted over a fresher one.
      if (pressed.current) { pressed.current = false; endRec(false); }
      epoch.current++;
      flying.current = 0;
      again.current = false;
    };
  }, [active, tick, trySend, fetchSpeech, endRec, bump, k]);

  useEffect(() => () => { stopSpeaking(); }, [stopSpeaking]);

  const now = Date.now();
  const held = sticky.current && now < sticky.current.until ? sticky.current.text : '';
  const view = askView({
    ...k, now,
    ask: ask.current, rec: rec.current, speaking: !!speaking.current, awaiting: !!awaiting.current,
    hasShot: !!shot, housePressed: housePressed.current, houseRecent: now - houseAt.current < 30000,
  });
  if (held) view.note = held;

  return {
    d: k.d, view, bad: !!held, eyes: sight.current.eyes, edge: sight.current.edge,
    text, setText, shot, setShot, send, queue, say, refresh: () => tick(true),
    pttIn, pttOut, houseIn, houseOut,
  };
}

export type { Deck };
