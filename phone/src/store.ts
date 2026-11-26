/* store.ts — the few things the app keeps between runs.

   node1's address and the deck token go in the Keychain, never in a URL and
   never in plain storage. Which cards were left open is kept beside them:
   it is a few bytes, and one store is one thing to reason about. */

import * as SecureStore from 'expo-secure-store';

const LINK = 'dmc-deck-link';
const PREFS = 'dmc-prefs';

export type Link = { base: string; token: string };
export type Prefs = { open?: Record<string, boolean>; push?: boolean; page?: string };

export async function loadLink(): Promise<Link | null> {
  try {
    const got = JSON.parse((await SecureStore.getItemAsync(LINK)) || 'null');
    return got && typeof got.base === 'string' && typeof got.token === 'string' ? got : null;
  } catch {
    return null;
  }
}

export async function saveLink(link: Link): Promise<void> {
  try { await SecureStore.setItemAsync(LINK, JSON.stringify(link)); } catch { /* this run only */ }
}

export async function forgetLink(): Promise<void> {
  try { await SecureStore.deleteItemAsync(LINK); } catch { /* nothing was kept */ }
}

let prefs: Prefs = {};

export async function loadPrefs(): Promise<Prefs> {
  try { prefs = JSON.parse((await SecureStore.getItemAsync(PREFS)) || '{}') || {}; } catch { prefs = {}; }
  return prefs;
}

export function getPrefs(): Prefs { return prefs; }

export function setPrefs(change: Prefs): void {
  prefs = { ...prefs, ...change };
  SecureStore.setItemAsync(PREFS, JSON.stringify(prefs)).catch(() => { /* this run only */ });
}
