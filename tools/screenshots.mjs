/* tools/screenshots.mjs — render every Flight Deck surface at true panel
   pixels and write the PNGs the README embeds.

   The panel is 2560 x 720 hardware that lives on a Pi; photographing it gives
   reflections, keystone and a phone's colour balance. This renders the real
   deck-ui against a scripted state frame instead, so the images in the README
   are the actual layout and are reproducible after any change.

   Needs node, playwright-core and a Chromium. It is a documentation tool, not
   a test — nothing in the repo depends on it:
       npm install playwright-core
       node tools/screenshots.mjs [--browser /path/to/chrome]
*/

import { createServer } from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const UI = path.join(here, '..', 'pi', 'deck-ui');
const OUT = path.join(here, '..', 'docs', 'img');
const PORT = 8137;

const arg = (name, fallback) => {
  const i = process.argv.indexOf(name);
  return i > -1 ? process.argv[i + 1] : fallback;
};

const TYPES = { '.html': 'text/html', '.css': 'text/css',
                '.js': 'application/javascript', '.svg': 'image/svg+xml' };

/* A state frame with everything populated, so the screenshots show the panel
   doing its job rather than a rack of em dashes. Values are plausible for this
   deployment: a Baron on the ground at Orlando, a boot in flight, two HP
   panels on DDC. */
// Real time, not a fixed epoch: the rolling window and every "last seen" on
// the page are relative to the browser's clock, so a frozen timestamp renders
// a fleet last seen 300 days ago and sparklines bunched against the axis.
const now = Math.floor(Date.now() / 1000);
const FRAME = {
  server_time: now,
  version: 'deck-api 0.1',
  workstation: { os: 'windows', since: now - 640, last_alive: now - 2,
                 ip: '192.168.68.50', agent: true },
  boot: { in_flight: true, target: 'windows', intent: 'sim', phase: 4,
          phase_name: 'PING', started: now - 37, elapsed: 37, result: null,
          observable_max: 5, os_up_at: null },
  pi: { temp_c: 44.2, load: 0.41, mem_pct: 38, uptime: '9 d', cpu_pct: 7 },
  telemetry: { ts: now, source: 'windows',
               ws: { gpu_temp_c: 47, gpu_util_pct: 12, vram_pct: 2.8 }, pi: {} },
  sim: { link: true, session: true, aircraft: 'Beechcraft Baron G58', checked: now },
  nav_plan: [ { name: 'ORLANDO EXEC', lat: 28.545, lon: -81.333 },
              { name: 'WALT DISNEY WORLD', lat: 28.385, lon: -81.564 },
              { name: 'LAKELAND LINDER', lat: 27.988, lon: -82.018 } ],
  mini: { up: true, ssh: true, screen: false, ip: '192.168.68.68', last_alive: now - 4 },
  surface: { active: 'deck', default: 'evals', previous: null, episode: 'boot',
             manual_in_episode: false,
             all: ['deck', 'evals', 'nav', 'sim', 'displays'] },
  evals: { grafana: false, mode: null, url: null, checked: now, playlist: null,
           view: 'auto', view_url: null, dashboards: [] },
  events: [
    { t: now - 4,   source: 'probe', text: 'workstation: booting -> windows', level: 'info' },
    { t: now - 37,  source: 'boot',  text: 'reboot requested', level: 'info' },
    { t: now - 37,  source: 'deck',  text: 'auto-switched to deck (boot episode)', level: 'info' },
    { t: now - 38,  source: 'deck',  text: 'trigger: sim -> windows', level: 'info' },
    { t: now - 620, source: 'probe', text: 'workstation: off -> linux', level: 'info' },
  ],
  profiles: {},
};

/* What the fast-polling surfaces fetch directly, outside the SSE frame. */
const SIM = { link: true, session: true, state: {
  aircraft: 'Beechcraft Baron G58', seq: 482,
  readouts: { lat: 28.545, lon: -81.333, gs_kt: 0, trk_true: 358, alt_ft: 112,
              ias_kt: 0, hdg_mag: 2, vs_fpm: 0, rpm: 0, fuel_gal: 146.6,
              pitch_deg: 0, bank_deg: 0 },
  controls: {
    gear:           { state: 'DOWN', pct: 100, handle: 'DOWN' },
    flaps:          { index: 0, count: 3, angle: 0 },
    parking_brake:  { state: 'ON' },
    landing_lights: { state: 'OFF' },
    autopilot:      { state: 'OFF', hdg_bug: 0, alt_ft: 3000, vs_fpm: 0, spd_kt: 85 },
  } } };

const MONITORS = { available: true, default_inputs: ['vga', 'hdmi1', 'hdmi2'],
  monitors: [
    { index: 1, desc: 'HP M32f FHD', position: 'LEFT',  x: 0,    y: 0,
      w: 1920, h: 1080, primary: true,  ddc: true, input_raw: 18, input: 'hdmi2', inputs: null },
    { index: 0, desc: 'HP M32f FHD', position: 'RIGHT', x: 1920, y: 0,
      w: 1920, h: 1080, primary: false, ddc: true, input_raw: 17, input: 'hdmi1', inputs: null },
  ] };

const MEDIA = { source: 'windows', active: true, playing: true,
  title: 'Rondo Alla Turca', artist: 'Mozart', album: 'Sonata No. 11',
  app: 'spotify', position_s: 74, duration_s: 213, art_id: 'x',
  can: { play_pause: true, next: true, prev: true } };

const streams = new Set();
/** Push a frame naming the surface to show. The page owns which surface is
    active — it is state, not a class the harness can set behind its back. */
const showSurface = (name) => {
  const frame = { ...FRAME, surface: { ...FRAME.surface, active: name } };
  for (const res of streams) res.write('data: ' + JSON.stringify(frame) + '\n\n');
};

const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  const json = (o) => { res.writeHead(200, { 'Content-Type': 'application/json' });
                        res.end(JSON.stringify(o)); };
  if (url.pathname === '/api/events') {
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    streams.add(res);
    req.on('close', () => streams.delete(res));
    // A short history, not one frame: the sparklines and the stat tiles plot
    // the rolling window, and a single sample draws nothing at all.
    const trace = [44, 45, 46, 45, 47, 49, 52, 50, 48, 47];
    const body = trace.map((t, i) => 'data: ' + JSON.stringify({
      ...FRAME,
      server_time: now - (trace.length - 1 - i) * 30,
      telemetry: { ts: now - (trace.length - 1 - i) * 30, source: 'windows',
                   ws: { gpu_temp_c: t, gpu_util_pct: 8 + i * 2, vram_pct: 2.4 + i / 20 },
                   pi: {} },
    }) + '\n\n').join('');
    res.write(body);
    return;   // held open: closing it makes the page reconnect and re-render
  }
  if (url.pathname === '/api/state')   return json(FRAME);
  if (url.pathname === '/api/sim')     return json(SIM);
  if (url.pathname === '/api/monitor') return json(MONITORS);
  if (url.pathname === '/api/media')   return json(MEDIA);
  if (url.pathname.startsWith('/api/')) return json({});
  const rel = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
  try {
    const body = await readFile(path.join(UI, rel));
    res.writeHead(200, { 'Content-Type': TYPES[path.extname(rel)] || 'text/plain' });
    res.end(body);
  } catch { res.writeHead(404); res.end('not found'); }
});

const SHOTS = [
  ['deck',    'deck',     'DECK — boot in flight, fleet, rolling telemetry'],
  ['nav',     'nav',      'NAV — moving map, flight plan, steer'],
  ['sim',     'sim',      'SIM — observed aircraft state and controls'],
  ['screens', 'displays', 'SCREENS — one card per monitor, DDC input switching'],
  ['evals',   'evals',    'jobContext — the existing Grafana wallboard, framed'],
];

const main = async () => {
  await mkdir(OUT, { recursive: true });
  await new Promise(r => server.listen(PORT, '127.0.0.1', r));
  // --serve leaves the harness up so the panel can be opened in a real browser
  // against a scripted state frame — the only way to look at a surface that
  // needs a sim, a booting workstation and two monitors all at once.
  if (process.argv.includes('--serve')) {
    console.log(`harness on http://127.0.0.1:${PORT}/  (ctrl-c to stop)`);
    return new Promise(() => {});
  }
  const { chromium } = await import('playwright-core');
  const exe = arg('--browser', process.env.CHROMIUM ||
                  '/opt/pw-browsers/chromium-1194/chrome-linux/chrome');
  const browser = await chromium.launch({ executablePath: exe });
  const page = await browser.newPage({ viewport: { width: 2560, height: 720 } });
  await page.goto(`http://127.0.0.1:${PORT}/index.html`);
  await page.waitForTimeout(1200);

  for (const [file, surface, caption] of SHOTS) {
    showSurface(surface);
    await page.waitForTimeout(400);
    await page.evaluate((s) => {
      // The live panes are revealed by their own poll; force them for the shot
      // so a screenshot never depends on poll timing.
      const show = (liveId, phId) => {
        const live = document.getElementById(liveId), ph = document.getElementById(phId);
        if (live) live.hidden = false;
        if (ph) ph.hidden = true;
      };
      if (s === 'sim') show('sim-live', 'sim-ph');
      if (s === 'nav') show('navp-live', 'navp-ph');
      if (s === 'displays') show('dsp-live', 'dsp-ph');
    }, surface);
    await page.waitForTimeout(700);
    const out = path.join(OUT, `${file}.png`);
    await page.screenshot({ path: out });
    console.log(`  ${path.relative(process.cwd(), out)}  — ${caption}`);
  }

  await browser.close();
  server.close();
};

main().catch(e => { console.error(e); process.exit(1); });
