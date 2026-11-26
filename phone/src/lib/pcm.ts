/* pcm.ts — what the microphone gave, as what speech-to-text wants: 16-bit
   mono at 16 kHz, in base64, with no WAV header. The same averaging as the
   page's toPcm16: each output sample is the mean of the input it spans,
   which is filter enough for speech. */

export const REC_RATE = 16000;
export const REC_MAX_MS = 20000;
export const REC_MIN_S = 0.4;

/** Chunks of 16-bit samples (as the microphone delivered them) as one run. */
export function joinInt16(chunks: ArrayBuffer[]): Int16Array {
  let n = 0;
  for (const c of chunks) n += c.byteLength >> 1;
  const all = new Int16Array(n);
  let at = 0;
  for (const c of chunks) {
    const part = new Int16Array(c, 0, c.byteLength >> 1);
    all.set(part, at);
    at += part.length;
  }
  return all;
}

/** Several channels interleaved as one, by taking the mean of each frame. */
export function toMono(samples: Int16Array, channels: number): Int16Array {
  if (channels <= 1) return samples;
  const out = new Int16Array(Math.floor(samples.length / channels));
  for (let i = 0; i < out.length; i++) {
    let sum = 0;
    for (let c = 0; c < channels; c++) sum += samples[i * channels + c];
    out[i] = Math.round(sum / channels);
  }
  return out;
}

/** From the rate the phone recorded at to the one wanted. */
export function resample(all: Int16Array, from: number, to: number): Int16Array {
  if (from === to || !from) return all;
  const step = from / to, n = all.length;
  const count = Math.floor(n / step);
  const out = new Int16Array(count);
  for (let i = 0; i < count; i++) {
    const a = Math.floor(i * step);
    const b = Math.min(n, Math.max(a + 1, Math.floor((i + 1) * step)));
    let sum = 0;
    for (let j = a; j < b; j++) sum += all[j];
    out[i] = Math.round(sum / (b - a));
  }
  return out;
}

const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

/** Bytes as base64. Written out because a phone's JavaScript has no Buffer,
    and its btoa wants a string of characters, not bytes. */
export function toBase64(bytes: Uint8Array): string {
  const out: string[] = [];
  let part = '';
  for (let i = 0; i < bytes.length; i += 3) {
    const a = bytes[i], b = i + 1 < bytes.length ? bytes[i + 1] : 0, c = i + 2 < bytes.length ? bytes[i + 2] : 0;
    part += B64[a >> 2] + B64[((a & 3) << 4) | (b >> 4)] +
      (i + 1 < bytes.length ? B64[((b & 15) << 2) | (c >> 6)] : '=') +
      (i + 2 < bytes.length ? B64[c & 63] : '=');
    if (part.length >= 8192) { out.push(part); part = ''; }
  }
  out.push(part);
  return out.join('');
}

export function int16Base64(samples: Int16Array): string {
  return toBase64(new Uint8Array(samples.buffer, samples.byteOffset, samples.byteLength));
}
