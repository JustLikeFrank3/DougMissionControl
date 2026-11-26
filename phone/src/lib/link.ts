/* link.ts — what the first-run screen makes of what it is given.

   `sudo ./pi/mobile-link.sh` on node1 prints https://<node1>.<tailnet>.ts.net/m?token=…
   and a QR code of it. Pasted or scanned, the whole link gives both things
   the app needs: where node1 is, and the token. A bare token gives only
   that, and the address is typed beside it. */

export type Parsed = { base: string; token: string; why: string };

/** The address as the app keeps it: scheme and host (and port), no path. */
export function cleanBase(text: string): string {
  const t = (text || '').trim();
  if (!t) return '';
  const m = /^(https?:\/\/)?([A-Za-z0-9.-]+(?::\d{1,5})?)(?:[/?#].*)?$/i.exec(t);
  if (!m) return '';
  return (m[1] ? m[1].toLowerCase() : 'https://') + m[2].toLowerCase();
}

/** Is this one of the addresses a development build may use plain HTTP for? */
export function isLocal(base: string): boolean {
  return /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(base);
}

/**
 * What was pasted or scanned, as an address and a token. `why` says what is
 * wrong with it, or is '' when it will do. `known` is the address already
 * typed or kept, used when only a token was given.
 */
export function parseLink(text: string, known?: string): Parsed {
  const v = (text || '').trim();
  if (!v) return { base: '', token: '', why: 'Paste the link node1 printed, or scan its QR code.' };
  const inLink = /[?&]token=([^&#\s]+)/.exec(v);
  let token = v, base = cleanBase(known || '');
  if (inLink) {
    try { token = decodeURIComponent(inLink[1]); } catch { token = inLink[1]; }
    const at = cleanBase(v.split(/[?#]/)[0].replace(/\/m\/?$/, ''));
    if (at) base = at;
  } else if (/^https?:\/\//i.test(v) || /\s/.test(v)) {
    return { base: cleanBase(v), token: '', why: 'That has no token in it. The link ends in ?token=…' };
  }
  if (!base) return { base: '', token, why: 'That is the token alone. Add node1\'s address: https://<node1>.<tailnet>.ts.net' };
  if (base.indexOf('http://') === 0 && !isLocal(base)) {
    return { base, token, why: 'That link is plain HTTP, which an iPhone app may not use. Run ' +
      'sudo ./pi/mobile-https.sh on node1, then mobile-link.sh again for the https:// link.' };
  }
  return { base, token, why: '' };
}
