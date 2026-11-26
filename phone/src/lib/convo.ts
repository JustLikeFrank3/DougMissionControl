/* convo.ts — Douglas's conversation log: the pure part of
   pi/deck-ui/js/convo.js. What /api/douglas carries is only the last few
   lines; scrolled to the top, the log asks for the lines before the oldest
   it shows and puts them above. */

export type Turn = { who?: string; via?: string; ts?: number; text?: string; image?: string };

const PAGE = 30;
const ROUTE = '/api/douglas/transcript';

/** Who said a line. One the user side of the record carries that came
    through ChatGPT was said by ChatGPT, not by the owner. */
export function whoLabel(t: Turn | null | undefined): string {
  if (!t || t.who !== 'user') return 'DOUGLAS';
  return t.via === 'chatgpt' ? 'CHATGPT' : 'YOU';
}

export function turnKey(t: Turn | null | undefined): string {
  return (t && t.ts) + '|' + (t && t.who) + '|' + (t && t.text);
}

/** Older and newer lines as one, oldest first, each line once. */
export function mergeTurns(older: Turn[] | null | undefined, newer: Turn[] | null | undefined): Turn[] {
  const seen: Record<string, boolean> = {};
  const out: { t: Turn; i: number }[] = [];
  (older || []).concat(newer || []).forEach((t, i) => {
    const k = turnKey(t);
    if (!t || seen[k]) return;
    seen[k] = true;
    out.push({ t, i });
  });
  // A line without a time stays where it came, after the timed ones.
  const at = (x: { t: Turn }) => (typeof x.t.ts === 'number' && isFinite(x.t.ts) ? x.t.ts : Infinity);
  out.sort((a, b) => {
    const d = at(a) - at(b);
    return (isNaN(d) ? 0 : d) || a.i - b.i;
  });
  return out.map((x) => x.t);
}

export function oldestTs(turns: Turn[] | null | undefined): number | null {
  for (const t of turns || []) {
    const ts = t && t.ts;
    if (typeof ts === 'number' && isFinite(ts)) return ts;
  }
  return null;
}

export function earlierPath(before: number, limit?: number, chat?: string): string {
  return ROUTE + '?before=' + encodeURIComponent(before) + '&limit=' + (limit || PAGE) +
    (chat ? '&chat=' + encodeURIComponent(chat) : '');
}

/** The words on the row above the oldest line. */
export function earlierLabel(loading: boolean, failed: boolean, more: boolean): string {
  return loading ? 'LOADING…' : failed ? 'EARLIER DID NOT LOAD · TOUCH TO TRY AGAIN'
    : more ? 'EARLIER' : 'THE BEGINNING OF THE RECORD';
}
