/* photo.ts — a photo for Douglas. The phone's camera or library is his eye.

   The photo is scaled down here, on the phone: a 12-megapixel original is
   megabytes he has no use for, and saving it again also turns whatever the
   phone shot (HEIC included) into JPEG. How far down: to 1280 pixels on its
   long side, unless Douglas has said he can make use of more. */

import { manipulateAsync, SaveFormat } from 'expo-image-manipulator';
import * as ImagePicker from 'expo-image-picker';

export type Shot = { b64: string; uri: string };

export async function shrink(uri: string, edge: number): Promise<Shot> {
  // Looked at first as the phone would show it, turned the right way up:
  // the size a picker reports is sometimes the sensor's, on its side.
  const seen = await manipulateAsync(uri, []);
  const k = Math.min(1, edge / Math.max(seen.width, seen.height));
  const actions = k < 1
    ? [{ resize: seen.width >= seen.height ? { width: Math.max(1, Math.round(seen.width * k)) }
                                           : { height: Math.max(1, Math.round(seen.height * k)) } }]
    : [];
  const out = await manipulateAsync(uri, actions, { compress: 0.85, format: SaveFormat.JPEG, base64: true });
  if (!out.base64) throw new Error('encode');
  return { b64: out.base64, uri: out.uri };
}

/** A photo from the camera or the library, shrunk; null when none was
    taken; a string when the phone would not allow it. */
export async function pick(from: 'camera' | 'library', edge: number): Promise<Shot | string | null> {
  if (from === 'camera') {
    const may = await ImagePicker.requestCameraPermissionsAsync();
    if (!may.granted) return 'This app is not allowed the camera. Allow it in Settings, under Doug.';
  }
  const got = from === 'camera'
    ? await ImagePicker.launchCameraAsync({ mediaTypes: ['images'], quality: 1 })
    : await ImagePicker.launchImageLibraryAsync({ mediaTypes: ['images'], quality: 1 });
  if (got.canceled || !got.assets || !got.assets[0]) return null;
  return shrink(got.assets[0].uri, edge);
}
