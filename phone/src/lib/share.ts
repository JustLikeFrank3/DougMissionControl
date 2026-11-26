/* share.ts — what another app handed over through the share sheet, as a
   question for Douglas.

   A photo goes as the picture, shrunk like any other. A link or a piece of
   text goes in the words, under the note: Douglas has a tool for reading a
   page and can open the link himself. Other kinds of file (a PDF, a video)
   have no route to him yet, and the sheet says so instead of failing. */

export const SHARE_PROMPT = 'What can you tell me about this?';
export const ASK_MAX = 2000;                     // deck-api's own limit on a typed question

export type Shared =
  | { kind: 'photo'; path: string; width: number | null; height: number | null; label: string }
  | { kind: 'link'; url: string; label: string }
  | { kind: 'text'; text: string; label: string }
  | { kind: 'none'; label: string };

type Intent = {
  type?: string | null; text?: string | null; webUrl?: string | null;
  files?: { path: string; mimeType?: string; fileName?: string; width?: number | null; height?: number | null }[] | null;
};

/** What was shared, as one of the kinds there is a route for. */
export function sharedOf(intent: Intent | null | undefined): Shared {
  const i = intent || {};
  const file = (i.files || [])[0];
  if (file) {
    if (/^image\//.test(file.mimeType || '')) {
      return { kind: 'photo', path: file.path, width: file.width ?? null, height: file.height ?? null,
               label: 'A PHOTO' };
    }
    const what = /^video\//.test(file.mimeType || '') ? 'A video' : /pdf/.test(file.mimeType || '') ? 'A PDF'
      : 'That kind of file';
    return { kind: 'none', label: what + ' cannot be sent to Douglas yet: there is no route for it. ' +
      'A photo, a link or a piece of text can.' };
  }
  if (i.webUrl) return { kind: 'link', url: i.webUrl, label: 'A LINK' };
  const text = (i.text || '').trim();
  if (/^https?:\/\/\S+$/i.test(text)) return { kind: 'link', url: text, label: 'A LINK' };
  if (text) return { kind: 'text', text, label: 'TEXT' };
  return { kind: 'none', label: 'Nothing came with that share.' };
}

/** The words that go to Douglas: the note, and under it what was shared. */
export function shareText(s: Shared, note: string): string {
  const n = (note || '').trim();
  if (s.kind === 'photo' || s.kind === 'none') return n.slice(0, ASK_MAX);
  const body = s.kind === 'link' ? s.url : s.text;
  const head = n ? n + '\n\n' : '';
  return (head + body).slice(0, ASK_MAX);
}

/** Will all of it fit in one question? */
export function shareFits(s: Shared, note: string): boolean {
  if (s.kind !== 'link' && s.kind !== 'text') return true;
  const body = s.kind === 'link' ? s.url : s.text;
  return (note || '').trim().length + 2 + body.length <= ASK_MAX;
}
