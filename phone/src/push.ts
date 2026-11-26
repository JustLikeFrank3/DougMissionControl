/* push.ts — notices and approvals on a phone whose app is closed.

   The phone asks Apple for a device token and hands it to node1, which
   sends a notification through Apple's push service for each new warning or
   critical notice, for a change of Douglas's that is waiting for a yes, and
   for a machine that has stopped answering (pi/push-setup.sh). No Expo
   account and no Expo server is involved: the token is Apple's own. */

import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';

import type { Deck } from './deck';

// One that arrives while the app is in front is still shown: the card it is
// about may be a screen away.
Notifications.setNotificationHandler({
  handleNotification: async () => ({ shouldShowBanner: true, shouldShowList: true, shouldPlaySound: false,
                                     shouldSetBadge: false }),
});

function within<T>(p: Promise<T>, ms: number): Promise<T> {
  return Promise.race([p, new Promise<T>((_, no) => setTimeout(() => no(new Error('slow')), ms))]);
}

/** This phone's token from Apple, or '' when it has none to give. */
export async function deviceToken(): Promise<string> {
  try {
    const t = await within(Notifications.getDevicePushTokenAsync(), 10000);
    return t.type === 'ios' && typeof t.data === 'string' ? t.data.toLowerCase() : '';
  } catch {
    return '';
  }
}

export async function allowed(): Promise<boolean> {
  try { return (await Notifications.getPermissionsAsync()).granted; } catch { return false; }
}

/** Ask the owner, then Apple, then node1. '' when it is on, or why not. */
export async function turnOn(deck: Deck, ask: boolean): Promise<string> {
  let may = await allowed();
  if (!may && ask) {
    try { may = (await Notifications.requestPermissionsAsync()).granted; } catch { may = false; }
  }
  if (!may) return 'This app is not allowed to notify. Allow it in Settings, under Doug.';
  const token = await deviceToken();
  if (!token) {
    return Device.isDevice ? 'Apple gave this phone no token. Is it online?'
      : 'A simulator has no token from Apple: this works on a real phone.';
  }
  // A build installed from Xcode uses Apple's sandbox service. node1 finds
  // out for itself if this one does not.
  const r = await deck.post('/api/push/register', { token, env: 'development', model: Device.modelName || '' });
  if (!r) return 'node1 did not answer';
  return r.ok ? '' : r.message || 'node1 refused';
}

export async function turnOff(deck: Deck): Promise<string> {
  const token = await deviceToken();
  if (!token) return '';
  const r = await deck.post('/api/push/forget', { token });
  return r && r.ok ? '' : 'node1 did not answer';
}
