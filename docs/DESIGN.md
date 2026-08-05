# Flight Deck — design notes

A Corsair XENEON Edge touch panel that fronts the boot orchestration in this
repo, driven by the Raspberry Pi 4B.

**Flight Deck is a real-time control and status surface, not a historical
observability platform.** Everything numeric on it is the latest reading;
the traces are a rolling hour held in the browser's memory and they die with
the page. Nothing is scraped on a schedule, nothing is retained, and nothing
is written to the Pi's thumb drive.

The rendered design, with the panel drawn at true proportion, is
[`dashboard-sketch.html`](dashboard-sketch.html).

## Target hardware

The Pi that already exists: **Raspberry Pi 4B, 4 GB, 64 GB USB thumb drive,
fan and heatsinks fitted.** No NVMe, no Pi 5.

| Link | Part | Note |
|---|---|---|
| Pi → Edge (video) | micro-HDMI → HDMI | HDMI0, nearest the USB-C jack. 2560 × 720 is 1.84M pixels — fewer than 1080p — so it is comfortable on a 4B |
| Pi → Edge (touch) | USB-A → USB-C | A **black** USB 2.0 port; HID needs no bandwidth and this keeps both blue ports free |
| Edge power | its own USB-C PD supply | The 4B supplies 1.2 A total across all four ports |
| Pi storage | the existing thumb drive | Fine, because nothing writes in a loop |

## Why the panel hangs off the Pi

The workstation is the subject of every control on the screen. A panel driven
by the workstation goes black exactly when you reboot it, which is the one
thing it cannot do. The Pi already holds the boot logic, the ssh key, and the
lock file.

The Edge accepts HDMI 2.0 alongside USB-C DP-alt, and its touch panel is a
standard USB HID digitizer, so a Pi 4B drives it with no vendor software in
the path.

## Install order

Each step is provable on its own, and each de-risks the next.

```
# 1. prove video + touch before anything is built on top
./pi/display-check.sh                     # read-only; fix every ✗ first
sudo ./pi/setup-display.sh                # cage + chromium + kiosk unit
                                          # touch all four corner marks -> PASS
# 2. wrap the orchestrator
sudo ./pi/setup-deck.sh                   # deck-api :8088, repoints fauxmo,
                                          # repoints the kiosk at the real UI
```

`setup-display.sh --force-mode` pins `video=HDMI-A-1:2560x720@60` into
`cmdline.txt` if EDID does not offer the mode. It backs the file up and
reverts if the edit would have added a second line — a two-line
`cmdline.txt` is an unbootable Pi.

## deck-api

`flightsim-boot.sh` is **not modified**. deck-api is a wrapper: it triggers
the orchestrator the same way fauxmo always has —
`FLIGHT_INTENT=<intent> flightsim-boot.sh <target> bg` — and learns what that
script is doing by following its journald output.

| Endpoint | Does |
|---|---|
| `GET /api/state` | Everything the panel renders |
| `GET /api/events` | The same, streamed over SSE on every change |
| `POST /api/boot` | `{"target":"windows\|linux","intent":"sim\|squadrons\|plain\|code"}` |
| `POST /api/launch` | Target already up — fire greeting + profile via the boot agent |
| `POST /api/abort` | Kill an in-flight run |
| `POST /api/hook/greeting` | Windows reports the greeting spoke — not wired yet |

Trust model is unchanged from `boot-agent.ps1`: the kiosk reaches deck-api
over loopback and needs no token; anything else needs `DECK_TOKEN` from
`/etc/flightsim/boot.env`, minted at install. LAN-only.

## Boot phases

| # | Phase | Signal |
|---|---|---|
| 1 | TRIGGER | `deck-api` takes the flock |
| 2 | PROBE | `trigger received — probing workstation state` |
| 3 | KICK | `sending WOL to …` / `requesting reboot into …` |
| 4 | PING | `host answers ping but no exporter yet` |
| 5 | OS UP | `<target> is up — deck online` |
| 6 | LOGON | greeting task fires — **not wired** |
| 7 | LAUNCHED | hook POST from Windows — **not wired** |

Phases 1–5 come free from the orchestrator's existing log lines. Phases 6–7
need `jarvis-greeting.ps1` to gain a callback; nothing on the Windows side is
touched today. `POST /api/hook/greeting` exists and works, the UI greys those
nodes out, and the state carries `boot.observable_max` so the panel never
implies it can see further than it can.

A boot started any other way — fauxmo not yet repointed, or the script run by
hand — is **adopted** when its first log line appears.

Warm reboot ≈ 30 s · cold boot ≈ 2 min · chained double boot ≈ 3 min.

**Unchanged dead end:** WOL cannot select a GRUB entry, so from full off the
machine always lands in the saved default.

## Live telemetry

Sampled from the exporters that **already exist** — this project installs
none of its own:

| Probe | Means |
|---|---|
| `:9107` TCP | Windows, and more reliable than `:9106` |
| `:9106` HTTP | Windows — also the GPU metric source under Windows |
| `:9105` HTTP | Linux — also the GPU metric source under Linux |
| ICMP | The NIC has power, nothing more |
| `/proc`, `/sys` | The Pi's own CPU, temp, memory, uptime |

deck-api parses the Prometheus text format those exporters already emit and
keeps **only the latest value**. It stores no series and writes no history.

The two exporters use different metric names — `gpu_temperature_celsius` on
one, `gpu_temp_c` on the other — and have changed before, so names are
matched on shape rather than pinned:

| Panel key | Matched against |
|---|---|
| `gpu_temp_c` | `gpu.*temp`, `temp.*gpu` |
| `gpu_util_pct` | `gpu.*util`, `util.*gpu` |
| `vram_used_bytes` / `vram_total_bytes` | `gpu_memory_used`, `gpu_memory_total`, `vram.*used/total` |
| `vram_pct` | derived from the two above |

A metric the exporter doesn't publish simply doesn't appear, rather than
reading zero.

### The rolling window lives in the browser

`deck-ui` keeps a ring buffer per trace — 60 minutes, capped at 1200 points,
roughly 720 samples per series at a 5 s poll. A few tens of kilobytes of
floats. It is the only history that exists anywhere in the system, and it
dies with the page.

Traces redraw only when a genuinely new sample lands, not on the 1 Hz clock
tick, because the browser is the most expensive process on a 4B.

### Gaps are information

When neither OS answers, deck-api records nothing rather than a zero and the
trace breaks. On a dual-boot machine that gap *is* the reboot — the same
event the phase track is narrating. Interpolating across it, or carrying the
last value forward, would erase the most interesting thing the panel has to
show.

## UI notes

2560 × 720 at ~180 ppi means a 44 px target is 6 mm. Every touch target is
≥ 158 px.

- Three fixed columns — **act** (600 px) / **boot then machine** (1100 px) /
  **fleet** (860 px). The boot controls are never behind a tab; reaching them
  is what you do while the machine is unusable.
- Destructive verbs arm on a 1.5 s hold with a fill sweep, not a confirm
  dialog — a modal on a 720 px-tall ribbon eats the whole screen.
- State is carried by word and shape as well as hue, so it survives
  colourblindness and a dim panel.
- One hue (cyan) for every trace; identity comes from the tile's title.
  Green/amber/red stay reserved for status. Magenta is the boot affordance
  and nothing else.
- Runs under `cage`, not a desktop: one fullscreen surface, no panels,
  nothing to blank the screen.

## Thumb-drive posture

- deck-api writes state only to `/run/flightdeck/` (tmpfs).
- `setup-display.sh` enables `tmp.mount`, moving `/tmp` to tmpfs — the
  orchestrator's `/tmp/flightsim-intent` and `/tmp/flightsim-boot.lock` are
  small, frequent writes that have no business on flash with no real wear
  levelling.
- No TSDB, so nothing writes in a loop.

## Deliberately absent

- **Prometheus, Grafana, Loki, long-term retention, historical dashboards.**
  A TSDB on flash with no real wear levelling corrupts before it dies.
- **Grafana panels embedded in the kiosk.** On a 4B that costs roughly
  700 MB more than serving numbers from deck-api and drawing them inline.
- **New exporters, SMART trend storage, PresentMon, Mac mini telemetry.**
- **NVMe, and any Pi 5 migration.**

## Layout

```
pi/display-check.sh          read-only: is the Edge's video + touch sound?
pi/setup-display.sh          cage + chromium kiosk, mode pinning, /tmp on tmpfs
pi/setup-deck.sh             installs deck-api, repoints fauxmo at it
pi/deck-api/deck_api.py      triggers, state, SSE, telemetry sampling
pi/deck-api/test_phases.py   phase mapping vs the orchestrator's real log lines
pi/deck-api/test_telemetry.py  exporter parsing, both naming schemes, gaps
pi/deck-ui/                  index.html · deck.css · deck.js · selftest.html
```

Both test files run anywhere — no Pi and no exporter needed.

## Known limits

- **Abort cannot recall a WOL packet already on the wire.** It kills the
  orchestrator and says so on the panel rather than pretending otherwise.
- Phases 6–7 are dark until the Windows callback is added.
- One boot at a time — the orchestrator's own flock still arbitrates, and a
  second trigger is rejected rather than queued.
- If `ping` is missing the poller degrades rather than dying, and says so:
  *"off" and "booting" cannot be told apart*.

## Verify on arrival

- Edge power draw — give it its own USB-C PD supply.
- That the Pi's EDID read offers 2560 × 720 @ 60 without a forced mode.
- That the digitizer's coordinate mapping is correct without calibration when
  the panel is the only output (`selftest.html` reports worst corner offset).
- Which metric names your `:9105` / `:9106` exporters actually publish —
  `test_telemetry.py` covers two shapes, but yours are the ones that matter.
