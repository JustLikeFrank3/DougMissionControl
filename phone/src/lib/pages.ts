/* pages.ts — the deck's pages, and what the menu says beside each.

   The phone page is one long column. The app has the room to give each
   part of the deck a page of its own, reached from a menu in the top bar:
   less to scroll past, and only the page in front asks node1 for anything
   beyond the live frame and Douglas's state. */

import { douglasHead } from './ask';
import { wsView } from './workstation';

export type PageId = 'workstation' | 'douglas' | 'notices' | 'memory' | 'chats' | 'tools' | 'cameras' | 'fleet' | 'phone';
export type Tone = '' | 'ok' | 'busy' | 'bad';
export type Page = { id: PageId; title: string; says: string };

export const PAGES: Page[] = [
  { id: 'workstation', title: 'WORKSTATION', says: 'start it, stop it, what is playing' },
  { id: 'douglas', title: 'DOUGLAS', says: 'talk to him, see what he said' },
  { id: 'notices', title: 'NOTICES', says: 'what the watch on Douglas has raised' },
  { id: 'memory', title: 'MEMORY', says: 'what he remembers, and what he did' },
  { id: 'chats', title: 'CONVERSATIONS', says: 'the one in front of him, and the ones kept' },
  { id: 'tools', title: 'WHAT HE MAY USE', says: 'a switch for each of his tools' },
  { id: 'cameras', title: 'CAMERAS', says: 'the Ring cameras, and who looked' },
  { id: 'fleet', title: 'FLEET', says: 'every machine, and the events' },
  { id: 'phone', title: 'THIS PHONE', says: 'notifications, and its link to node1' },
];

export const HOME: PageId = 'douglas';

/** A page id from somewhere it could be anything (a kept preference). */
export function pageOf(id: unknown): PageId {
  return PAGES.some((p) => p.id === id) ? (id as PageId) : HOME;
}

export function titleOf(id: PageId): string {
  return (PAGES.filter((p) => p.id === id)[0] || PAGES[0]).title;
}

/** Where a notification that was tapped leads. */
export function pageForPush(kind: unknown): PageId {
  return kind === 'approval' ? 'douglas' : kind === 'probe' ? 'fleet' : 'notices';
}

/** What the menu says beside a page, from the live frame and Douglas's
    state: only where there is something to say, and never a guess. */
export function badge(id: PageId, state: any, d: any): { text: string; tone: Tone } | null {
  const s = state || {};
  if (id === 'workstation' && state) {
    const v = wsView(s);
    return { text: v.pill, tone: v.cls };
  }
  if (id === 'douglas' && state) {
    if (d && d.agent && d.voice && d.voice.pending && d.voice.pending.id) return { text: 'WAITING FOR A YES', tone: 'busy' };
    const h = douglasHead(s.douglas);
    return { text: h.pill, tone: h.cls };
  }
  if (id === 'notices') {
    const n = s.douglas && s.douglas.notices;
    if (!n) return null;
    if (n.new > 0) return { text: n.new + ' NEW', tone: n.worst === 'critical' ? 'bad' : 'busy' };
    return n.open > 0 ? { text: n.open + ' OPEN', tone: '' } : { text: 'NONE', tone: 'ok' };
  }
  if (id === 'tools') {
    const tools = d && d.agent && d.voice ? d.voice.tools : null;
    if (!tools) return null;
    const off = Object.keys(tools).filter((k) => tools[k] === 'off').length;
    return off ? { text: off + ' OFF', tone: 'busy' } : null;
  }
  if (id === 'chats') {
    const chats = d && d.agent && d.voice ? d.voice.chats : null;
    return chats ? { text: (chats.kept || 0) + ' KEPT', tone: '' } : null;
  }
  return null;
}

/** Does anything in the menu want looking at? The colour of the mark on the
    menu button, or '' for none. */
export function menuMark(state: any, d: any): Tone {
  const n = state && state.douglas && state.douglas.notices;
  if (n && n.new > 0 && n.worst === 'critical') return 'bad';
  if (d && d.agent && d.voice && d.voice.pending && d.voice.pending.id) return 'busy';
  return n && n.new > 0 ? 'busy' : '';
}
