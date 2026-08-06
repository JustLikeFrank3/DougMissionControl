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

## Surfaces (design pass 3 — approved, not yet built)

Flight Deck is one console with four first-class surfaces behind a
**persistent 72 px strip**: `DECK` · `EVALS` · `MEDIA` · `SIM`, each a
196 × 72 px button (~27 × 10 mm at ~180 ppi). No hamburger, no nested menus,
no desktop-sized tabs. The strip also carries host status (OS, GPU, link,
clock) and a permanent `HOME`, which satisfies "persistent system status in
SIM mode" on *every* surface rather than only there.

Moving the wordmark and clock into the strip pays for most of its height —
DECK's left rail drops its own eyebrow row, so the 648 px that remain still
fit the existing layout.

### Switching

Automatic, but scoped to **episodes** rather than a cooldown timer:

| Trigger | Effect |
|---|---|
| idle | `EVALS` is the default; the last manual choice is remembered |
| a boot begins, *any* origin | opens a boot episode → `DECK` |
| MSFS reports ready | opens a sim episode → `SIM` |
| simulator exits | closes the episode → restores the pre-episode surface |
| **manual tap during an episode** | ends automatic switching **for that episode** |

A fixed cooldown either expires mid-boot and yanks the screen away, or
outlasts the event and stops working. Episode scoping means "stop switching"
lasts exactly as long as the thing you told it to stop switching for.

"MSFS ready" comes from the sim-agent reporting a live SimConnect session,
not from a process list — *running* is a weaker claim than *ready*.

## EVALS — the original reason for the panel

Reclaiming the TV from the jobContext wallboard is the use case that put the
Edge on the Pi, and it stays.

### The Grafana stack is pre-existing infrastructure, not Flight Deck scope

The jobContext wallboard *already runs on this Pi*. jobContextMCP's README
describes "a Raspberry Pi running k3s + Prometheus + Grafana, federating the
AKS cluster", with provisioned kiosk dashboards (`kiosk-evals`,
`kiosk-provenance`, `kiosk-cloud`, `kiosk-ollama`, `kiosk-gaming`) and a
`wallboard-kiosk.sh` that probes `up{job=gpu-windows}` to swap playlists by
booted OS. That script is not in this repo — it lives on the Pi.

**Preserve all of it.** The earlier "no Prometheus, no Grafana" rule was
about Flight Deck not *introducing* a metrics stack. It was never a licence
to remove one that already exists and serves the original use case. Treat
k3s, Prometheus, Grafana, the five kiosk dashboards, and the playlist logic
as an **external, pre-existing dependency**.

Flight Deck therefore **integrates with the existing kiosk rather than
replacing it**. What changes is only which component sits at the top:

- The **playlist behaviour is preserved**, including the OS-aware swap.
- The **dashboards are untouched** — Flight Deck provisions none and edits
  none.
- Flight Deck becomes the single **display-state controller**, so that two
  things are not independently deciding what is on screen.

There is one hard constraint underneath that: two compositors cannot share
one framebuffer. Whichever process hosts the display has to host the other's
output too. That decision must be made from the live configuration, not from
this document.

### Inspect before touching anything

`pi/inspect-wallboard.sh` is read-only and writes
`docs/pi-wallboard-survey.md`: what launches the browser today, whether it
owns tty1/DRM, the Grafana health/dashboards/playlists, whether anonymous
viewing is allowed, k3s and Prometheus state, and the memory headroom that is
actually left. It also copies any `wallboard-kiosk.sh` it finds into
`pi/wallboard/`, so that now-load-bearing script finally lands in git.

`pi/setup-display.sh` **refuses to run** when it detects an existing kiosk,
and prints that command instead. Override only after reading the survey, with
`--adopt-kiosk`. It also no longer changes the default systemd target.

### Integration order

1. **Host the existing playlist as-is.** Grafana is already on this Pi, so
   `EVALS` is a `?kiosk` playlist URL on localhost. No new backend, no
   duplicated dashboards, and the existing playlist selection preserved.
2. If the 16:9 boards prove too tall at 720 px, add a `kiosk-edge` board
   **in jobContext** with the *same queries* in a 5:1 layout. Same data, same
   logic, different geometry — that is not duplication.
3. Only as a fallback, a thin Flight-Deck-native presentation reading
   jobContext's existing `/metrics` (Prometheus text, which deck-api already
   parses). Still presentation; still no business logic moved.

jobContext remains authoritative. Flight Deck stores no eval data, re-derives
no verdicts, and invents no thresholds. It shows what the boards already
show: mean judge score and worst entry, hallucination rate, verdict flip
rate, Layer 1 smoke pass rate, active alerts, per-dimension means, CoV
instability by entry, time since last suite run, eval pushes by kind, runs
without a provenance record, and judge ⇄ provenance agreement by bucket.

### Display-state controller

One component decides what is on screen, and the existing playlist keeps
running underneath it:

| Condition | Display state |
|---|---|
| jobContext normal / idle | **Grafana playlist** (EVALS) — the existing kiosk |
| boot requested, any origin | **DECK** |
| Windows gaming online | DECK, or the existing `kiosk-gaming` playlist |
| sim agent appears | **SIM** |
| user selects MEDIA | **MEDIA** |
| MSFS exits | previous / default → back to the playlist |

Manual selection always wins, scoped by episode (below), so the screen never
changes while it is being used.

**Unreachable ≠ zero.** deck-api health-checks Grafana and the jobContext API
independently of the embedded frame; if either is down, EVALS covers the
frame with `JOBCONTEXT UNREACHABLE` plus the age of the last good check
rather than leaving a stale board looking current.

## SIM — bidirectional cockpit control

### The finding that decides the architecture

**SimConnect is Windows-only.** Both common wrappers (Python-SimConnect,
node-simconnect) require a Windows host with SimConnect installed. The Pi
cannot talk to MSFS directly, so a Windows-side agent is required — which
this repo already has a pattern for in `windows/boot-agent.ps1` on :9107.

```
RASPBERRY PI                                  WINDOWS WORKSTATION
┌──────────────────────────┐                 ┌──────────────────────────┐
│ Flight Deck UI (XENEON)  │                 │ flightdeck-sim-agent     │
│ deck-api                 │      LAN        │        ↕                 │
│                          │◄───────────────►│ SimConnect               │
│ Existing Grafana         │                 │        ↕                 │
│ Existing Prometheus      │                 │ MSFS 2024                │
│ Existing kiosk playlist  │                 └──────────────────────────┘
└──────────────────────────┘
```

Commands flow XENEON → deck-api → sim agent → SimConnect → MSFS.
State flows MSFS → SimConnect → sim agent → deck-api → SSE → XENEON.

**The Pi must never attempt to load or speak SimConnect.** The Windows agent
owns every SimConnect interaction and exposes *normalized Flight Deck state*
— raw SimConnect concepts do not leak onto the Pi or into the UI. deck-api
carries an envelope like `{control:"gear", action:"set", value:"up"}` and
knows nothing about SimVars, event ids or units.

### The agent's contract, deliberately tiny

| Endpoint | Purpose |
|---|---|
| `GET /health` | agent alive, SimConnect session state, sim version, current aircraft |
| `GET /state` | the normalized state block |
| `POST /command` | `{cmd_id, control, action, value}` → `{cmd_id, accepted, seq}` |
| push or poll | WebSocket/SSE from Windows → Pi, or short polling, for rapidly changing values |

Initial normalized state covers only the proof controls — gear, flaps
position/detent, parking brake, landing lights, autopilot master — plus
altitude, airspeed and heading if they come for free.

### The agent is expected to disappear

It starts with Windows and MSFS and vanishes on every reboot into Linux.
That is normal operation, not an error: **deck-api treats loss of the sim
agent as `SIM OFFLINE`, never as a Flight Deck failure.** The SIM surface
shows *no link* in the navigation strip, and the sim episode closes so the
display returns to the playlist.

`node-simconnect` could in principle let the Pi connect over TCP once
`SimConnect.xml` is opened to the network. Rejected for v1: unofficial
protocol implementation whose MSFS 2024 compatibility must be re-proven each
sim update; a Node runtime on a 4 GB Pi already carrying k3s, Grafana and a
browser; and it moves failure onto the node that must stay up. The adapter
boundary keeps it available later.

### Command acknowledgement

**The UI never renders the commanded state.** Only the observed one.

1. UI sends the command with a `cmd_id`.
2. sim-agent transmits the event, returns `{cmd_id, accepted, seq}` —
   *accepted* means received, never "it worked".
3. UI enters `PENDING`, an overlay on the last **observed** state.
4. The state stream moves: `GEAR TOTAL PCT EXTENDED` leaves 100 → `TRANSIT`;
   reaches 0 → `UP`.
5. No state change within the control's timeout → `NO RESPONSE`, falling back
   to the last observed state, never the commanded one.

That is what makes a command the sim ignored — wrong aircraft, paused sim,
gear already up — show as a failed command rather than a lie.

### The v1 five, plus three readouts

| Control | State (SimVar) | Command (Event) |
|---|---|---|
| Gear | `GEAR HANDLE POSITION`, `GEAR TOTAL PCT EXTENDED` | `GEAR_UP` / `GEAR_DOWN` / `GEAR_TOGGLE` |
| Flaps | `FLAPS HANDLE INDEX`, `FLAPS NUM HANDLE POSITIONS`, `TRAILING EDGE FLAPS LEFT ANGLE` | `FLAPS_INCR` / `FLAPS_DECR` / `FLAPS_SET` |
| Parking brake | `BRAKE PARKING POSITION` | `PARKING_BRAKES` |
| Landing lights | `LIGHT LANDING` | `LANDING_LIGHTS_ON` / `_OFF` (prefer over toggle) |
| AP master | `AUTOPILOT MASTER` | `AP_MASTER` |
| Readouts | `AIRSPEED INDICATED`, `INDICATED ALTITUDE`, `PLANE HEADING DEGREES MAGNETIC` | — |
| Capabilities | `IS GEAR RETRACTABLE`, `FLAPS AVAILABLE`, `FLAPS NUM HANDLE POSITIONS`, `SPOILER AVAILABLE`, `AUTOPILOT AVAILABLE` | — |

Gear is first because `GEAR TOTAL PCT EXTENDED` gives a real `TRANSIT` state
— it is the control that actually proves the loop.

`BRAKE PARKING POSITION`, `GEAR HANDLE POSITION` and `FLAPS HANDLE INDEX` are
confirmed against the published SDK reference. **Every name above must still
be verified with the SDK's SimvarWatcher against the target aircraft before
code is written** — MSFS DevSupport carries live threads about
`GEAR POSITION` not matching its own documentation.

Heading arrives in radians; conversion happens in the adapter, not the UI.

### Development target: Beechcraft Baron G58

A default piston twin with retractable gear whose transit is slow enough to
watch, three flap detents, an autopilot, and no autothrottle — so it
exercises the capability model in both directions on day one. Unsupported
controls grey out with a reason; they never sit there silently inert.

### Keyboard emulation

Fallback only. A keystroke cannot be acknowledged, cannot be read back, and
cannot report whether the sim was even focused. Permitted only where no
SimConnect path exists, must be labelled *open-loop — state not confirmed*,
and **none of the v1 five need it**.

### Control pages

`FLIGHT` (v1) · `LIGHTS` · `AUTOPILOT` · `GROUND / START` · `COM / NAV`.
All five appear in the SIM tab bar from day one, greyed and visibly
unavailable, so the surface's shape is honest. Only `FLIGHT` is live.

## MEDIA — reserved

First-class in the navigation from day one, showing *reserved* rather than
pretending to work. Eventually artwork, title, artist, transport, position,
volume.

One constraint fixed now because it decides the integration: **the Pi is a
remote control, not the playback device.** Audio comes out of the machine
being used — the workstation or the Mac mini — and MEDIA commands that
machine. Making the Pi an AirPlay endpoint would put the music on the wrong
speakers and add an audio daemon to the node whose job is staying out of the
way.

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

## Who owns what

| Concern | Owner | Flight Deck's role |
|---|---|---|
| Eval results, certification, judge, provenance | **jobContext** | Displays its existing Grafana boards. Stores nothing, re-derives nothing |
| Metric history, Prometheus, Grafana, k3s | **jobContext**, already on this Pi | Adds none of its own; EVALS is a viewport onto software jobContext installed |
| Aircraft state | **MSFS 2024** | Reads it live via the agent; never caches it as truth, never assumes a command took effect |
| Flight Deck's own telemetry | Flight Deck | Latest value in deck-api, 60-minute window in the browser, dies with the page |
| Boot orchestration | `flightsim-boot.sh` | Still unmodified; deck-api wraps it and follows its journald output |

## Deliberately absent — and the difference that matters

The rule is about what Flight Deck **introduces**, never about removing what
is already there. The Pi's k3s / Prometheus / Grafana stack is sunk
infrastructure serving the original wallboard use case: it stays, untouched.
Scope creep would be *rebuilding or expanding* it to support Flight Deck.

Flight Deck itself adds none of:

- **Its own Prometheus, Grafana, Loki, retention, or historical dashboards.**
  A Flight-Deck-owned TSDB on this thumb drive would corrupt before it dies,
  and the existing stack already answers the historical questions.
- **New dashboards in the existing Grafana**, beyond at most one `kiosk-edge`
  board *provisioned by jobContext* if the 5:1 aspect demands it.
- **Grafana panels embedded in Flight Deck's own DECK/SIM surfaces.** On a 4B
  that costs roughly 700 MB more than serving numbers from deck-api and
  drawing them inline. EVALS is the deliberate exception, because there it
  *is* the wallboard rather than a decoration on top of one.
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

## Build order for pass 3

0. **Survey the live Pi.** `./pi/inspect-wallboard.sh`, commit the survey and
   the recovered `wallboard-kiosk.sh`. Nothing else starts until the five
   questions at the end of that survey are answered from real output.
1. **Console shell.** The 72 px strip, four surfaces, episode-scoped
   switching, DECK moved under it. UI change plus a little surface state in
   deck-api — no new backends.
2. **EVALS.** Host the existing Grafana playlist on localhost, preserving its
   OS-aware selection, and add the reachability overlay. The existing kiosk's
   playlist logic is adopted, not rewritten; its dashboards are not touched.
   Then a `kiosk-edge` 5:1 board *in jobContext* if the existing layout proves
   too tall.
3. **Sim link, before any control.** The Windows sim-agent, a SimConnect
   session, one read-only value on screen. This is where the SimVar names get
   verified with SimvarWatcher against the Baron.
4. **One control, all the way.** Gear only — command, acknowledgement,
   TRANSIT, settled state, no-response timeout. Everything after is
   repetition.
5. **The other four**, then capabilities, then the later pages.

MEDIA stays reserved until the SIM interaction model is proven.

### Open before step 2

Is the Pi running the wallboard the same `pi-node1` the boot orchestrator
installs onto? Everything above assumes yes — the k3s naming, Grafana on
:3000, and the OS-swap probe all point that way — but `wallboard-kiosk.sh`
is not in jobContextMCP and could not be inspected. If they are two different
Pis, EVALS becomes a remote URL instead of localhost and nothing else
changes.

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
