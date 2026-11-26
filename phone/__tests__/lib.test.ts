/* The app's pure logic: what the screens say, what goes to node1, and what
   is made of what comes back. No phone, no node1 and no Douglas needed. */

declare const Buffer: any;       // the test runner's, not the phone's

import { askView, AskScreen, douglasHead, meters, newId, ready, shotEdge } from '../src/lib/ask';
import { cameraSub, healthRows, journalLine, obsLine } from '../src/lib/cameras';
import { earlierLabel, earlierPath, mergeTurns, oldestTs, whoLabel } from '../src/lib/convo';
import { fleetRows, machineLine, servicesLine } from '../src/lib/fleet';
import { clock, fmtAgo, fmtDur, fmtTime } from '../src/lib/format';
import { cleanBase, parseLink } from '../src/lib/link';
import { didLine, emptyWords, listPath, memoryCount, pastLine, provenance } from '../src/lib/memory';
import { hourStep, meta, summary } from '../src/lib/notices';
import { chatRows, jobLine, jobRows, jobShort, left, needsLine, switchesPill, switchRows, waitingHead } from '../src/lib/owner';
import { int16Base64, joinInt16, resample, toBase64, toMono } from '../src/lib/pcm';
import { sharedOf, shareFits, shareText, SHARE_PROMPT } from '../src/lib/share';
import { frameReader } from '../src/lib/sse';
import { wsView } from '../src/lib/workstation';

describe('the link node1 prints', () => {
  const link = 'https://node1.tail1234.ts.net/m?token=abc%2Fdef';
  test('the whole link gives the address and the token', () => {
    expect(parseLink(link)).toEqual({ base: 'https://node1.tail1234.ts.net', token: 'abc/def', why: '' });
  });
  test('a bare token needs an address', () => {
    expect(parseLink('abcdef').why).toMatch(/token alone/);
    expect(parseLink('abcdef', 'node1.tail1234.ts.net')).toEqual(
      { base: 'https://node1.tail1234.ts.net', token: 'abcdef', why: '' });
  });
  test('plain HTTP is refused, with the way out', () => {
    const got = parseLink('http://node1:8088/m?token=abc');
    expect(got.why).toMatch(/plain HTTP/);
    expect(got.why).toMatch(/mobile-https\.sh/);
  });
  test('except to this machine, for a development build', () => {
    expect(parseLink('http://localhost:8088/m?token=abc').why).toBe('');
  });
  test('a link without a token says so', () => {
    expect(parseLink('https://node1.tail1234.ts.net/m').why).toMatch(/no token/);
  });
  test('nothing asks for something', () => {
    expect(parseLink('  ').why).toMatch(/Paste the link/);
  });
  test('an address is kept without its path', () => {
    expect(cleanBase('HTTPS://Node1.Tail.ts.net/m/')).toBe('https://node1.tail.ts.net');
    expect(cleanBase('not an address')).toBe('');
  });
});

describe('the live frame', () => {
  test('a frame is a frame once its blank line has come', () => {
    const r = frameReader();
    expect(r.feed('data: {"a"')).toEqual([]);
    expect(r.feed(':1}\n')).toEqual([]);
    expect(r.feed('\ndata: {"b":2}\n\ndata: {"c"')).toEqual(['{"a":1}', '{"b":2}']);
    expect(r.feed(':3}\r\n\r\n')).toEqual(['{"c":3}']);
  });
  test('a comment line is not data', () => {
    expect(frameReader().feed(': keep-alive\n\n')).toEqual([]);
  });
});

describe('a question on its way', () => {
  const base: AskScreen = {
    polled: true, d: { agent: true, ts: 100, voice: { state: 'idle' } }, misses: 0, late: false,
    ask: null, rec: null, speaking: false, awaiting: false, hasShot: false, housePressed: false, houseRecent: false,
    now: 100000,
  };
  test('the id is q- and 24 hex digits', () => {
    expect(newId([0, 1, 255, 16, 0, 0, 0, 0, 0, 0, 0, 171])).toBe('q-0001ff1000000000000000ab');
    expect(newId(new Uint8Array(12))).toMatch(/^q-[0-9a-f]{24}$/);
  });
  test('a photo is sent at 1280, or more where he asks, never past 4096', () => {
    expect([shotEdge(undefined), shotEdge(3072), shotEdge(900), shotEdge(9999)]).toEqual([1280, 3072, 1280, 4096]);
  });
  test('before the first poll it is worth a try', () => {
    expect(ready({ polled: false, d: null, misses: 0, late: false })).toBe(true);
  });
  test('it goes when he is idle or speaking, and not otherwise', () => {
    const k = (state: string, more = {}) => ({ polled: true, d: { agent: true, voice: { state } }, misses: 0, late: false, ...more });
    expect([ready(k('idle')), ready(k('speaking')), ready(k('thinking')), ready(k('listening'))])
      .toEqual([true, true, false, false]);
    expect(ready(k('idle', { misses: 1 }))).toBe(false);
    expect(ready(k('idle', { late: true }))).toBe(false);
    expect(ready({ polled: true, d: { agent: false }, misses: 0, late: false })).toBe(false);
  });
  test('at rest it says nothing and offers SEND', () => {
    const v = askView(base);
    expect([v.go, v.locked, v.note, v.placeholder]).toEqual(['SEND', false, '', 'Ask Douglas…']);
    expect(v.ptt).toEqual({ state: 'idle', sub: 'READY' });
    expect(v.house).toEqual({ state: 'idle', sub: 'READY', disabled: false });
  });
  test('with a photo the box asks about it', () => {
    expect(askView({ ...base, hasShot: true }).placeholder).toBe('Ask about the photo…');
  });
  test('one in flight counts its seconds and offers STOP', () => {
    const v = askView({ ...base, ask: { kind: 'photo', since: 90000, flying: true, last: 95000 } });
    expect([v.go, v.locked, v.note, v.placeholder]).toEqual(['STOP', true, 'Sending the photo… 5 s', 'Sending the photo…']);
  });
  test('one waiting on a lost link says it will go by itself', () => {
    const v = askView({ ...base, d: null, misses: 3, ask: { kind: 'text', since: 35000, flying: false, last: 0 } });
    expect(v.note).toBe('No answer from node1 yet. Your question will go by itself when the link is back: ' +
                        'keep the app open. 1:05');
    expect(v.placeholder).toBe('Waiting to send…');
  });
  test('one waiting on a busy Douglas goes next', () => {
    const v = askView({ ...base, d: { agent: true, voice: { state: 'thinking' } },
                        ask: { kind: 'text', since: 98000, flying: false, last: 0 } });
    expect(v.note).toBe('Douglas is busy. Your question goes next. 0:02');
  });
  test('no link says SEND still works', () => {
    const v = askView({ ...base, d: null, misses: 3 });
    expect(v.note).toBe('node1 is not answering. SEND still works: your question waits until the link is back.');
    expect(v.house).toEqual({ state: 'absent', sub: 'NO LINK', disabled: true });
  });
  test('an unreachable Douglas says why', () => {
    expect(askView({ ...base, d: { agent: false, why: 'douglas-agent did not answer' } }).note)
      .toBe('Douglas is unreachable: douglas-agent did not answer');
  });
  test('a slow link is said before a deadline makes it official', () => {
    expect(askView({ ...base, late: true }).note).toBe('node1 is slow to answer');
  });
  test('the recording says what the phone is doing', () => {
    expect(askView({ ...base, rec: { live: false, started: 0 } }).ptt).toEqual({ state: 'idle', sub: 'STARTING…' });
    const v = askView({ ...base, rec: { live: true, started: 97000 } });
    expect(v.ptt).toEqual({ state: 'listening', sub: 'LISTENING…' });
    expect(v.note).toBe('Listening through this phone. Let go when you have finished. 0:03');
  });
  test('his reply being fetched, then spoken', () => {
    expect(askView({ ...base, awaiting: true }).note).toBe('Fetching his reply…');
    expect(askView({ ...base, awaiting: true }).ptt.sub).toBe('THINKING…');
    expect(askView({ ...base, speaking: true }).note).toBe('Douglas is speaking. Hold the button to cut in.');
  });
  test('a press DougVoice made nothing of is said, while it is news', () => {
    const d = { agent: true, ts: 100, voice: { state: 'idle', heard: { words: 0, seconds: 4, level_db: -50, ts: 95 } } };
    expect(askView({ ...base, d, houseRecent: true }).note)
      .toBe('DougVoice heard nothing in 4 s: the room around it was quiet. It listens through its own ' +
            'microphone, not this phone\'s.');
    expect(askView({ ...base, d }).note).toBe('');
  });
  test('the house button shows the pipeline, and why it cannot be used', () => {
    expect(askView({ ...base, d: { agent: true, voice: {} } }).house)
      .toEqual({ state: 'absent', sub: 'NO VOICE PIPELINE', disabled: true });
    expect(askView({ ...base, d: { agent: false } }).house.sub).toBe('NO AGENT');
    expect(askView({ ...base, d: { agent: true, voice: {} }, housePressed: true }).house.disabled).toBe(false);
  });
  test('a missing reading is a dash, never a zero', () => {
    expect(meters({ agent: false })).toEqual([]);
    const m = meters({ agent: true, gpu: { busy_pct: 0 }, llm: {}, host: { temp_c: 61.4 } });
    expect(m[0]).toEqual(['GPU', '0', '%']);
    expect(m[1]).toEqual(['MEMORY', null, 'G']);
    expect(m[2]).toEqual(['TOK/S', null, '']);
    expect(m[4]).toEqual(['TEMP', '61', '°C']);
    const full = meters({ agent: true, gpu: { vram_used_bytes: 8 * 1073741824, vram_total_bytes: 96 * 1073741824 } });
    expect(full[1]).toEqual(['MEMORY', '8', '/96G']);
  });
  test('his head says whether he is up and on what', () => {
    expect(douglasHead({ up: false, host: 'douglas' })).toEqual(
      { pill: 'OFFLINE', cls: 'bad', sub: 'unreachable at douglas', subCls: '' });
    expect(douglasHead({ up: true, model: { state: 'running', name: 'qwen' } }).sub).toBe('qwen');
    expect(douglasHead({ up: true, model: { state: 'loading' } }).subCls).toBe('busy');
    expect(douglasHead({ up: true, model: { state: 'absent' } }).sub).toBe('model server down');
  });
});

describe('what the microphone gave', () => {
  test('chunks are one run', () => {
    const a = new Int16Array([1, 2]).buffer, b = new Int16Array([3]).buffer;
    expect(Array.from(joinInt16([a, b]))).toEqual([1, 2, 3]);
  });
  test('two channels are their mean', () => {
    expect(Array.from(toMono(new Int16Array([100, 200, -50, 50]), 2))).toEqual([150, 0]);
    const one = new Int16Array([1, 2]);
    expect(toMono(one, 1)).toBe(one);
  });
  test('48 kHz becomes 16 kHz by averaging', () => {
    expect(Array.from(resample(new Int16Array([3, 6, 9, 30, 30, 30, 1]), 48000, 16000))).toEqual([6, 30]);
    const same = new Int16Array([1, 2, 3]);
    expect(resample(same, 16000, 16000)).toBe(same);
  });
  test('bytes as base64, with and without padding', () => {
    const b = (s: string) => toBase64(Uint8Array.from(s.split('').map((c) => c.charCodeAt(0))));
    expect([b(''), b('f'), b('fo'), b('foo'), b('foob'), b('fooba'), b('foobar')])
      .toEqual(['', 'Zg==', 'Zm8=', 'Zm9v', 'Zm9vYg==', 'Zm9vYmE=', 'Zm9vYmFy']);
    const big = new Uint8Array(100000).map((_, i) => i % 251);
    expect(toBase64(big)).toBe(Buffer.from(big).toString('base64'));
  });
  test('samples go little-endian, as speech-to-text wants them', () => {
    expect(int16Base64(new Int16Array([1, -2]))).toBe(Buffer.from([1, 0, 254, 255]).toString('base64'));
  });
});

describe('the share sheet', () => {
  test('a photo goes as the picture, with the note as the words', () => {
    const s = sharedOf({ type: 'media', files: [{ path: 'file:///a.heic', mimeType: 'image/heic', width: 4032, height: 3024 }] });
    expect(s).toEqual({ kind: 'photo', path: 'file:///a.heic', width: 4032, height: 3024, label: 'A PHOTO' });
    expect(shareText(s, SHARE_PROMPT)).toBe('What can you tell me about this?');
  });
  test('a link goes in the words, under the note', () => {
    const s = sharedOf({ type: 'weburl', webUrl: 'https://example.com/a', text: 'https://example.com/a' });
    expect(s.kind).toBe('link');
    expect(shareText(s, SHARE_PROMPT)).toBe('What can you tell me about this?\n\nhttps://example.com/a');
    expect(shareText(s, '  ')).toBe('https://example.com/a');
  });
  test('text that is only an address is a link', () => {
    expect(sharedOf({ type: 'text', text: ' https://example.com ' }).kind).toBe('link');
  });
  test('text goes as text', () => {
    const s = sharedOf({ type: 'text', text: 'a paragraph' });
    expect(shareText(s, 'Why?')).toBe('Why?\n\na paragraph');
  });
  test('a PDF or a video has no route yet, and says so', () => {
    expect(sharedOf({ files: [{ path: 'x', mimeType: 'application/pdf' }] })).toEqual(
      { kind: 'none', label: 'A PDF cannot be sent to Douglas yet: there is no route for it. A photo, a link or ' +
                              'a piece of text can.' });
    expect(sharedOf({ files: [{ path: 'x', mimeType: 'video/quicktime' }] }).label).toMatch(/^A video cannot/);
    expect(sharedOf({}).kind).toBe('none');
  });
  test('more than a question holds is cut, and known to be', () => {
    const s = sharedOf({ text: 'x'.repeat(2500) });
    expect(shareFits(s, 'note')).toBe(false);
    expect(shareText(s, 'note').length).toBe(2000);
    expect(shareFits(sharedOf({ text: 'short' }), 'note')).toBe(true);
  });
});

describe('the words on the cards', () => {
  const now = 1_000_000;
  test('durations and ages', () => {
    expect([fmtDur(59), fmtDur(61), fmtAgo(now - 30, now), fmtAgo(now - 3600, now), fmtAgo(now - 3 * 86400, now), fmtAgo(0, now)])
      .toEqual(['59 s', '1:01', '30 s', '60 m', '3 d', '—']);
    expect([fmtTime(0), fmtTime(75), fmtTime(null), clock(65000)]).toEqual(['0:00', '1:15', '', '1:05']);
  });
  test('the workstation, up, off and booting', () => {
    const up = wsView({ workstation: { os: 'linux', since: now - 120 } }, now);
    expect([up.pill, up.cls, up.big, up.meta, up.canOff, up.shutdown])
      .toEqual(['ONLINE', 'ok', 'LINUX', 'up 2 m', true, 'HOLD TO SHUT DOWN LINUX']);
    const off = wsView({ workstation: { os: 'off', since: now - 30 } }, now);
    expect([off.pill, off.big, off.meta, off.canOff]).toEqual(['OFFLINE', 'POWERED OFF', 'since 30 s', false]);
    const boot = wsView({ workstation: { os: 'booting' },
                          boot: { in_flight: true, target: 'windows', elapsed: 75, phase: 3, observable_max: 7,
                                  phase_name: 'POST', intent: 'sim' } });
    expect([boot.pill, boot.cls, boot.big, boot.phase, boot.inFlight, boot.canOff])
      .toEqual(['BOOTING → WINDOWS', 'busy', '1:15', 'phase 3 / 7 · POST · sim', true, false]);
    expect(wsView({}).big).toBe('—');
  });
  test('a machine\'s line leaves out what it does not report', () => {
    expect(machineLine({ up: false }, now)).toEqual({ text: '', bad: false });
    expect(machineLine({ up: true, as_of: now, reports: { host: { temp_c: 51.6, load: 0.5 },
                                                         disk: { total_bytes: 100, used_bytes: 42 } } }, now).text)
      .toBe('52 °C · load 0.50 · disk 42 %');
    expect(machineLine({ up: true, as_of: now - 300, reports: { host: { temp_c: 50 } } }, now).text)
      .toBe('50 °C · 5 m old');
  });
  test('a service switched off on purpose is not down', () => {
    expect(servicesLine(null)).toEqual({ text: 'services not readable', bad: false, known: false });
    expect(servicesLine([{ unit: 'a', state: 'active' }, { unit: 'b', state: 'inactive', enabled: 'disabled' }]))
      .toEqual({ text: '1 of 1 services up · off: b', bad: false, known: true });
    expect(servicesLine([{ unit: 'a', state: 'failed', restarts: 2 }]))
      .toEqual({ text: '0 of 1 services up · DOWN: a · restarted: a 2×', bad: true, known: true });
  });
  test('the fleet has its six machines, and Douglas down is a fault', () => {
    const rows = fleetRows({ workstation: { os: 'windows' }, mini: { up: false, last_alive: now - 60 },
                             douglas: { up: false, last_alive: now - 120 }, voice: { up: true, satellite: false } },
                           { machines: [{ id: 'workstation', up: true, reports: { host: { power_w: 240 } } }] }, now);
    expect(rows.map((r) => r.name)).toEqual(['DOUG DUALBOOT', 'DOUG PRIME', 'MINIDOUG4', 'DOUGMOBILE', 'DOUGLAS', 'DOUGVOICE']);
    expect(rows[0]).toEqual({ name: 'DOUG DUALBOOT', says: 'windows', up: true, bad: false, more: { text: '240 W', bad: false } });
    expect(rows[2].says).toBe('off · seen 60 s');
    expect([rows[4].says, rows[4].bad]).toEqual(['offline · seen 2 m', true]);
    expect(rows[5].says).toBe('up · satellite down');
  });
  test('who said a line', () => {
    expect([whoLabel({ who: 'user', via: 'phone' }), whoLabel({ who: 'user', via: 'chatgpt' }), whoLabel({ who: 'douglas' })])
      .toEqual(['YOU', 'CHATGPT', 'DOUGLAS']);
  });
  test('earlier lines and newer ones are one log, each line once', () => {
    const a = { ts: 1, who: 'user', text: 'a' }, b = { ts: 2, who: 'douglas', text: 'b' }, c = { ts: 3, who: 'user', text: 'c' };
    expect(mergeTurns([a, b], [b, c])).toEqual([a, b, c]);
    expect(mergeTurns([c], [a])).toEqual([a, c]);
    expect(oldestTs([{ text: 'no time' }, b])).toBe(2);
    expect(oldestTs([])).toBeNull();
    expect(earlierPath(12.5, 0, 'abc')).toBe('/api/douglas/transcript?before=12.5&limit=30&chat=abc');
    expect([earlierLabel(true, false, true), earlierLabel(false, true, true), earlierLabel(false, false, false)])
      .toEqual(['LOADING…', 'EARLIER DID NOT LOAD · TOUCH TO TRY AGAIN', 'THE BEGINNING OF THE RECORD']);
  });
  test('notices: not read is not none', () => {
    expect(summary(null)).toEqual({ text: '—', cls: '' });
    expect(summary({ ok: true, open: [] })).toEqual({ text: 'NONE', cls: 'ok' });
    expect(summary({ ok: true, open: [{ severity: 'warning', acked: true }, { severity: 'critical' }] }))
      .toEqual({ text: '2 OPEN · 1 NEW', cls: 'bad' });
    expect([hourStep(23, 1), hourStep(0, -1), hourStep(7, 1)]).toEqual([0, 23, 8]);
    expect(meta({ raised: now - 600, times: 2, told: true, spoken: true }, now))
      .toMatch(/ · 10 m · got worse · Douglas has mentioned it · said aloud$/);
    expect(meta({ raised: now - 600, cleared: now - 300 }, now)).toMatch(/^over since .* · lasted 5 m$/);
  });
  test('a change waiting for a yes', () => {
    expect(waitingHead({ kind: 'push', via: 'phone' })).toBe('A PUSH · asked on the phone');
    expect(waitingHead({})).toBe('A CHANGE · asked on another line');
    expect([left(now + 125, now), left(now + 5, now), left(now - 1, now)]).toEqual(['2 m 5 s left', '5 s left', 'no longer waiting']);
  });
  test('a job never reads as done with some left', () => {
    const report = { needs: [{ n: 1, text: 'a', state: 'done' }, { n: 2, text: 'b', state: 'outstanding' },
                             { n: 3, text: 'c', state: 'blocked' }] };
    expect(needsLine(report)).toBe('1 OF 3 DONE · 1 OUTSTANDING · 1 BLOCKED');
    expect(needsLine({ needs: [{ state: 'pending' }] })).toBe('1 REQUIREMENT');
    expect(jobShort({ running: false, report })).toBe(true);
    expect(jobShort({ running: true, report })).toBe(false);
    expect(jobLine({ running: true, began: now - 120 }, now)).toBe('IN THE WORKSHOP · under way for 2 m');
    expect(jobLine({ running: false, ended: now - 60, report }, now))
      .toBe('IN THE WORKSHOP · ended 60 s ago · 1 OF 3 DONE · 1 OUTSTANDING · 1 BLOCKED');
    const rows = jobRows({ asked: 'fix it', repo: 'o/r', branch: 'b', state: 'done', needs: report.needs,
                           checks: [{ ok: false, what: 'tests', said: '2 failed' }], ran: [{ code: null, cmd: 'make' }] });
    expect(rows[0]).toEqual(['k', 'ASKED']);
    expect(rows).toContainEqual(['v', 'o/r · on b']);
    expect(rows).toContainEqual(['n', '2. b', 'outstanding']);
    expect(rows).toContainEqual(['v', 'FAILED · tests (2 failed)\ndid not finish · make']);
  });
  test('no switch is hidden, and the pill counts what is off', () => {
    const rows = switchRows({ search: 'off', memory: 'on', brand_new: 'on' });
    expect(rows.map((r) => r.name)).toEqual(['memory', 'search', 'brand_new']);
    expect(rows[2]).toEqual({ name: 'brand_new', label: 'brand_new', does: '', state: 'on' });
    expect([switchesPill(null), switchesPill({ a: 'on' }), switchesPill({ a: 'off', b: 'on' })])
      .toEqual([{ text: '—', cls: '' }, { text: 'ALL ON', cls: 'ok' }, { text: '1 OFF', cls: 'busy' }]);
    expect(switchRows(null)).toEqual([]);
  });
  test('his conversations, the one in front of him marked', () => {
    expect(chatRows({ id: 'b', list: [{ id: 'a', title: '', turns: 1, via: 'phone' },
                                      { id: 'b', title: 'Kettle', turns: 4, updated: now - 300 }] }, now))
      .toEqual([{ id: 'a', title: '(no name yet)', here: false, sub: '1 exchange · phone' },
                { id: 'b', title: 'Kettle', here: true, sub: '4 exchanges · 5 m ago' }]);
    expect(chatRows(null, now)).toEqual([]);
  });
  test('what he remembers, and where it came from', () => {
    expect(provenance({ source: 'said', via: 'phone', updated: now - 7200, confidence: 0.9, uses: 3 }, now))
      .toBe('you said so · phone · 2 h 0 m ago · sure 90% · used 3×');
    expect(pastLine({ action: 'restored', who: 'owner', at: now - 60, before: 'old' }, { text: 'new' }, now))
      .toBe('brought back by you · 60 s ago · was: old');
    expect(listPath({ status: 'active', kind: 'task', q: 'a b' }, 30))
      .toBe('/api/douglas/memory?op=list&status=active&limit=30&offset=30&kind=task&q=a%20b');
    expect([memoryCount(null), memoryCount({ active: 12, open_tasks: 2 }), memoryCount({ active: 3 })])
      .toEqual(['—', '12 KEPT · 2 TO DO', '3 KEPT']);
    expect(didLine({ action: 'search', ok: false, detail: 'timeout', ms: 2400, via: 'phone' }))
      .toBe('search · failed (timeout) · 2.4 s · phone');
    expect(emptyWords({ status: 'deleted', q: '' })).toBe('Nothing deleted.');
    expect(emptyWords({ status: 'active', q: 'x' })).toBe('Nothing he remembers matches that.');
  });
  test('a camera picture says whether it is of now', () => {
    const t = 1_700_000_000_000;
    expect(obsLine({ camera_name: 'Porch', captured_ms: t - 5000, source: 'live-triggered' }, t))
      .toMatch(/^Porch · taken .* \(5 s old\) · recorded now, on request$/);
    expect(obsLine({ camera_name: 'Porch', captured_ms: t - 900000, source: 'snapshot' }, t))
      .toMatch(/NOT LIVE: the latest picture Ring had$/);
    expect(obsLine(null)).toBe('');
    expect(cameraSub({ online: false, battery: true, ring_name: 'Front Door', name: 'Porch' }))
      .toBe('OFFLINE · battery · Ring calls it Front Door');
    expect(healthRows({ accounts: [{ email: 'a@b', state: 'expired' }] })[0][2]).toBe(true);
    expect(journalLine({ at: 0, role: 'douglas', what: 'picture', camera: 'abcdef123456', result: 'refused: off' }, {}))
      .toEqual({ text: 'never · Douglas · picture · 123456 · refused: off', bad: true });
  });
});
