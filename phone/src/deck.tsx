/* deck.tsx — the one way this app talks to deck-api.

   The app is a second client of the routes the phone page uses, and of
   nothing else: it does not talk to Douglas, DougRing or the workstation
   directly. Every request carries DECK_TOKEN in an Authorization header,
   which no address, history or proxy log gets to keep.

   A phone's link to node1 is not a wire. Over the tailnet it can drop to a
   relay, or stall while the phone moves between access points, and a
   request on its own waits about a minute before it admits anything is
   wrong. So every request has a deadline, and one that misses it comes back
   as null, "no answer", exactly as a refused connection does. */

import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { AppState } from 'react-native';
import { fetch } from 'expo/fetch';

import { frameReader } from './lib/sse';

const READ_MS = 8000;        // deck-api's own slowest read gives up after three seconds
const COMMAND_MS = 30000;    // outlasts its slowest command: a shutdown waits up to twenty
const QUIET_MS = 40000;      // a frame comes at least every fifteen seconds

type Init = { method?: string; body?: string; headers?: Record<string, string> };
export type Sound = { sound: Uint8Array } | { state?: string } | null;
export type Picture = { b64: string; type: string; obs: any } | { error: string };

export class Deck {
  constructor(readonly base: string, readonly token: string, private readonly forbidden: () => void) {}

  headers(extra?: Record<string, string>): Record<string, string> {
    return { ...(this.token ? { Authorization: 'Bearer ' + this.token } : {}), ...extra };
  }

  /** For an image component: a bare URL would get a 403. */
  image(path: string) {
    return { uri: this.base + path, headers: this.headers() };
  }

  /** One request, read to its end inside the deadline. `read` turns the
      response into what the caller wants; null for no answer or a 403. */
  private async ask<T>(path: string, init: Init, ms: number, read: (r: any) => Promise<T>): Promise<T | null> {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), ms);
    try {
      const r = await fetch(this.base + path, { ...init, headers: this.headers(init.headers), signal: ctl.signal });
      if (r.status === 403) { this.forbidden(); return null; }
      return await read(r);
    } catch {
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  get(path: string, headers?: Record<string, string>): Promise<any> {
    return this.ask(path, { headers }, READ_MS, (r) => r.json());
  }

  post(path: string, body?: object, ms?: number): Promise<any> {
    return this.ask(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(body || {}) }, ms || COMMAND_MS, (r) => r.json());
  }

  /** His reply to a spoken turn: the sound of it when it is ready, or what
      became of it. */
  speech(id: string): Promise<Sound> {
    return this.ask('/api/douglas/speech?id=' + encodeURIComponent(id), {}, 20000, async (r) => {
      const sound = (r.headers.get('Content-Type') || '').indexOf('audio/') === 0;
      return sound ? { sound: new Uint8Array(await r.arrayBuffer()) } : r.json();
    });
  }

  /** A picture from a camera, taken now. Lives in the app's memory until it
      is closed, and is never saved. */
  async picture(body: object, toBase64: (b: Uint8Array) => string): Promise<Picture> {
    const got = await this.ask<Picture>('/api/cameras/image', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }, 180000, async (r) => {
      const type = r.headers.get('Content-Type') || '';
      if (!r.ok || !/^image\//.test(type)) {
        try { const j = await r.json(); return { error: j.error || j.code || 'HTTP ' + r.status }; }
        catch { return { error: 'HTTP ' + r.status }; }
      }
      let obs = null;
      try { obs = JSON.parse(r.headers.get('X-Observation') || 'null'); } catch { obs = null; }
      return { b64: toBase64(new Uint8Array(await r.arrayBuffer())), type, obs };
    });
    return got || { error: 'node1 did not answer' };
  }

  /** The live frame: one `data:` line per change, and at least every
      fifteen seconds. Returns the way to close it. */
  stream(onFrame: (s: any) => void, onEnd: () => void): () => void {
    const ctl = new AbortController();
    let closed = false, last = Date.now();
    const quiet = setInterval(() => { if (Date.now() - last > QUIET_MS) ctl.abort(); }, 5000);
    (async () => {
      try {
        const r = await fetch(this.base + '/api/events',
                              { headers: this.headers({ Accept: 'text/event-stream' }), signal: ctl.signal });
        if (r.status === 403) { closed = true; this.forbidden(); return; }
        if (!r.ok || !r.body) throw new Error('no stream');
        const reader = r.body.getReader(), text = new TextDecoder(), frames = frameReader();
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          last = Date.now();
          for (const data of frames.feed(text.decode(value, { stream: true }))) {
            try { onFrame(JSON.parse(data)); } catch { /* keep the last good frame */ }
          }
        }
      } catch { /* the link: the caller connects again */ }
      clearInterval(quiet);
      if (!closed) onEnd();
    })();
    return () => { closed = true; clearInterval(quiet); ctl.abort(); };
  }
}

/* ── the live frame, and whether anybody is looking ─────────────────────── */

export type Conn = { text: string; cls: '' | 'live' | 'dead' };
type Ctx = { deck: Deck; state: any; active: boolean; conn: Conn };
const DeckContext = createContext<Ctx | null>(null);
export const DeckProvider = DeckContext.Provider;

export function useDeck(): Ctx {
  const c = useContext(DeckContext);
  if (!c) throw new Error('no deck');
  return c;
}

/** Is the app in front? A phone puts it to sleep, and the stream dies under
    it: it is closed on the way out and opened again on return, so a frame
    from an hour ago never looks live. */
export function useActive(): boolean {
  const [active, setActive] = useState(AppState.currentState !== 'background');
  useEffect(() => {
    const sub = AppState.addEventListener('change', (s) => setActive(s === 'active'));
    return () => sub.remove();
  }, []);
  return active;
}

export function useConnection(deck: Deck | null, active: boolean): { state: any; conn: Conn } {
  const [state, setState] = useState<any>(null);
  const [conn, setConn] = useState<Conn>({ text: 'connecting…', cls: '' });
  useEffect(() => {
    if (!deck || !active) return;
    let over = false, close: (() => void) | null = null, retry: ReturnType<typeof setTimeout> | null = null;
    const connect = () => {
      if (over) return;
      // A probe first: a stream cannot show a 403.
      deck.get('/api/state').then((s) => {
        if (over) return;
        if (!s) {
          // No answer is not a verdict: keep trying while the app is being looked at.
          setConn({ text: 'node1 unreachable', cls: 'dead' });
          retry = setTimeout(connect, 5000);
          return;
        }
        setState(s);
        setConn({ text: 'live', cls: 'live' });
        close = deck.stream(setState, () => {
          if (over) return;
          setConn({ text: 'reconnecting…', cls: 'dead' });
          retry = setTimeout(connect, 2000);
        });
      });
    };
    connect();
    return () => {
      over = true;
      if (retry) clearTimeout(retry);
      if (close) close();
    };
  }, [deck, active]);
  return { state, conn };
}

/**
 * One reading, asked for again every `ms` while the app is in front and
 * `on` is true. One request at a time: a slow link stretches everything,
 * and calls fired regardless pile up on the handful of connections a phone
 * opens to node1, where the one that matters, a question for Douglas,
 * queues behind them.
 *
 * Returns the last answer (undefined before the first, null for "no
 * answer") and a way to ask now.
 */
export function usePoll(path: string, ms: number, on = true): [any, () => void] {
  const { deck, active } = useDeck();
  const [data, setData] = useState<any>(undefined);
  const flying = useRef(false), again = useRef(false), epoch = useRef(0);
  const ask = useCallback(() => {
    if (flying.current) { again.current = true; return; }
    const mine = epoch.current;
    flying.current = true;
    deck.get(path).then((d) => {
      if (mine !== epoch.current) return;       // the app slept meanwhile: stale
      flying.current = false;
      setData(d);
      if (again.current) { again.current = false; ask(); }
    });
  }, [deck, path]);
  useEffect(() => {
    if (!active || !on) return;
    ask();
    const id = setInterval(ask, ms);
    return () => {
      clearInterval(id);
      // Whatever is in flight belongs to an app about to sleep. Its answer
      // must not be painted over a fresher one, nor hold up the first poll
      // when the app wakes.
      epoch.current++;
      flying.current = false;
      again.current = false;
    };
  }, [active, on, ms, ask]);
  return [data, ask];
}
