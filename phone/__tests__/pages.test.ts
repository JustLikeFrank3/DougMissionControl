/* The menu: which pages there are, and what it says beside each. */

import { badge, HOME, menuMark, pageForPush, pageOf, PAGES, titleOf } from '../src/lib/pages';

describe('the pages of the deck', () => {
  test('every part of the deck has one, each named once', () => {
    const ids = PAGES.map((p) => p.id);
    expect(ids).toEqual(['workstation', 'douglas', 'notices', 'memory', 'chats', 'tools', 'cameras', 'fleet', 'phone']);
    expect(new Set(PAGES.map((p) => p.title)).size).toBe(PAGES.length);
  });
  test('a kept page that is no longer one opens on Douglas', () => {
    expect([pageOf('memory'), pageOf('gone'), pageOf(undefined)]).toEqual(['memory', HOME, HOME]);
    expect(HOME).toBe('douglas');
    expect(titleOf('tools')).toBe('WHAT HE MAY USE');
  });
  test('a tapped notification leads to what it is about', () => {
    expect([pageForPush('approval'), pageForPush('notice'), pageForPush('probe'), pageForPush('test'), pageForPush(undefined)])
      .toEqual(['douglas', 'notices', 'fleet', 'notices', 'notices']);
  });
});

describe('what the menu says beside a page', () => {
  const up = { workstation: { os: 'linux' }, douglas: { up: true, model: { state: 'running' } } };
  test('nothing before there is a frame', () => {
    expect(PAGES.map((p) => badge(p.id, null, null))).toEqual(PAGES.map(() => null));
    expect(menuMark(null, null)).toBe('');
  });
  test('the workstation and Douglas say whether they are up', () => {
    expect(badge('workstation', up, null)).toEqual({ text: 'ONLINE', tone: 'ok' });
    expect(badge('douglas', up, null)).toEqual({ text: 'ONLINE', tone: 'ok' });
    expect(badge('douglas', { douglas: { up: false } }, null)).toEqual({ text: 'OFFLINE', tone: 'bad' });
  });
  test('a change waiting for a yes is said first, and marks the button', () => {
    const d = { agent: true, voice: { pending: { id: 'abc' } } };
    expect(badge('douglas', up, d)).toEqual({ text: 'WAITING FOR A YES', tone: 'busy' });
    expect(menuMark(up, d)).toBe('busy');
  });
  test('notices: not read is not none', () => {
    expect(badge('notices', up, null)).toBeNull();
    const n = (notices: object) => ({ douglas: { up: true, notices } });
    expect(badge('notices', n({ open: 0, new: 0, worst: null }), null)).toEqual({ text: 'NONE', tone: 'ok' });
    expect(badge('notices', n({ open: 2, new: 0, worst: null }), null)).toEqual({ text: '2 OPEN', tone: '' });
    expect(badge('notices', n({ open: 2, new: 1, worst: 'warning' }), null)).toEqual({ text: '1 NEW', tone: 'busy' });
    expect(badge('notices', n({ open: 3, new: 2, worst: 'critical' }), null)).toEqual({ text: '2 NEW', tone: 'bad' });
    expect([menuMark(n({ open: 2, new: 0 }), null), menuMark(n({ open: 2, new: 1, worst: 'warning' }), null),
            menuMark(n({ open: 3, new: 2, worst: 'critical' }), null)]).toEqual(['', 'busy', 'bad']);
  });
  test('his tools only when one is off, his conversations when they are kept', () => {
    const d = (voice: object) => ({ agent: true, voice });
    expect(badge('tools', up, d({ tools: { search: 'on' } }))).toBeNull();
    expect(badge('tools', up, d({ tools: { search: 'off', draw: 'off', look: 'on' } }))).toEqual({ text: '2 OFF', tone: 'busy' });
    expect(badge('chats', up, d({ chats: { kept: 4 } }))).toEqual({ text: '4 KEPT', tone: '' });
    expect(badge('chats', up, { agent: false })).toBeNull();
  });
});
