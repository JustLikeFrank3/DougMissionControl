# Train Sim World 6 — integration investigation

Research findings and architecture for adding TSW6 as a first-class
simulator behind a RAIL surface, alongside MSFS behind SIM/NAV. This is
the investigation the work was asked to start with; nothing here is
implemented yet except `scripts/tsw_probe.py`, which exists precisely so
the first implementation step is verification rather than faith.

Researched 2026-08-06. Sources and licenses in §19. Facts marked
**[verify]** could not be confirmed against a live copy of TSW6 from this
environment and are what the probe run must settle.

---

## 1. Executive recommendation

**GO — integrate directly against the official TSW6 External Interface
API. No SimHub, no CobraOne, no RailDriver emulation in the path.**

TSW5 introduced, and TSW6 carries, an official Dovetail external API:
launch the game with `-HTTPAPI` and it serves plain HTTP JSON on
`http://127.0.0.1:31270`, authenticated by a `DTGCommKey` header whose
key the game writes to
`Documents\My Games\TrainSimWorld6\Saved\Config\CommAPIKey.txt`. It
provides:

- **discovery** — `GET /list/<node>` walks the object tree of the
  currently driven vehicle, so a loco's controls are enumerable at
  runtime, with per-endpoint writability flags;
- **telemetry** — speed, brake gauges, ammeter, wheel slip, next
  signal/speed-limit/gradient data, geo position, consist identity, sim
  time, weather;
- **writable analog controls** — `PATCH /set/<path>?Value=0.25` sets a
  lever to a continuous 0–1 position (snapping to real notches), which is
  a *real* control write, not a synthesized keypress;
- **batched polling** — server-side subscriptions read N values in one
  HTTP GET.

That is everything the MSFS integration gets from SimConnect, over a
transport (local HTTP + JSON) that our existing C# agent can speak with
zero new native dependencies. The preferred end state the task named —

    TSW6  ⟷  Doug Windows agent  ⟷  Doug  →  Xeneon Edge
                    ↑
                  HOTAS

— is technically available today. CobraOne's interface and PieHid64.dll
emulation solve a problem we no longer have (see §7); SimHub has no TSW
support at all and would add a hop for nothing (§13).

The one honest caveat: the official API documentation PDF lives behind
Dovetail's forum and was unreachable from this environment. Everything
above is cross-confirmed from **five or more independent working
open-source implementations** (§19), which is strong evidence — but the
first implementation step is still to run `scripts/tsw_probe.py` on the
real workstation against the real game (§16), not to write product code.

## 2. Existing Doug architecture relevant to this integration

The stack, as built:

```
RASPBERRY PI (always on)                      WINDOWS WORKSTATION (comes and goes)
┌───────────────────────────────┐             ┌──────────────────────────────────┐
│ deck-ui   kiosk on the Edge   │             │ boot-agent.ps1        :9107      │
│ deck-api  :8088  state + SSE  │◄───LAN─────►│ flightdeck-sim-agent  :9109      │
│ fauxmo    Alexa triggers      │             │   ├ SimBridge → SimConnect → MSFS│
│ flightsim-boot.sh orchestrator│             │   └ MediaBridge                  │
└───────────────────────────────┘             │ jarvis-greeting.ps1 (logon task) │
                                              └──────────────────────────────────┘
```

What matters for RAIL, pattern by pattern:

- **The membrane rule.** `SimVars.cs` is the only file that holds MSFS
  vocabulary; `Normalize.cs` converts raw values into Flight Deck shapes;
  `HttpApi.cs` serves only normalized state. No SimVar name, unit or
  event id crosses the LAN. deck-api's `sim_state()` is a verbatim
  pass-through and reshapes nothing.
- **Liveness is layered.** `GET /` and `/status` are unauthenticated
  process liveness; `/health` reports `sim.connected`, which means *a
  live SimConnect session*, never "the process is running". deck-api's
  `fetch_sim_link()` distinguishes `link` (agent answering) from
  `session` (simulator connected).
- **Commands are honest.** `POST /command` → `accepted` means *the event
  was transmitted*, never "it worked". The UI renders only observed
  state; commanded state appears as a `PENDING` overlay that resolves by
  watching the state stream, or decays to `NO RESPONSE`.
- **Absence is not zero.** A control the aircraft lacks is absent from
  `controls`; the panel greys it with `NOT FITTED`. Telemetry gaps break
  the sparkline; stat tiles dim past 15 s and blank past 120 s.
- **Two clocks.** `workstation.since` (state last changed) is kept apart
  from `last_alive` (last successful contact), after the "last seen 0 s
  on a dark host" bug.
- **Surfaces and episodes.** `deck / evals / nav / sim`, with
  episode-scoped auto-switching (a boot opens a DECK episode; a sim
  session opens a SIM episode; manual tap suppresses autos for that
  episode). Per-surface pollers run only while the surface is on screen —
  SIM polls `/api/sim` at 250 ms, NAV at 500 ms; the strip's coarse link
  rides the ordinary 2–5 s poll.
- **Boot orchestration.** `flightsim-boot.sh` takes only a *target OS*;
  the *game* is chosen by `FLIGHT_INTENT`, recorded on the Pi and read
  back at Windows logon by `jarvis-greeting.ps1`, which launches the
  matching `$LaunchProfiles` entry. Adding a game is a documented
  three-edit pattern and touches the orchestrator not at all.
- **The panel.** 2560 × 720 Xeneon Edge on a Pi 4B. EFIS palette (cyan
  reference data, magenta boot affordance, green/amber/red reserved for
  state), mono type, ≥158 px touch targets, hold-to-arm for anything
  destructive, placeholder pattern (`ph-pill / ph-t / ph-s / ph-n`) for
  every offline surface. The browser is the most expensive process on
  the box; nothing redraws without new data.

RAIL slots into every one of those patterns without bending any of them.

## 3. TSW6 API findings

Cross-confirmed facts (≥3 independent implementations unless noted):

| Fact | Value |
|---|---|
| Enable | `-HTTPAPI` in Steam launch options (persisted by Steam, so a `steam://rungameid` launch keeps it) |
| Transport | plain HTTP over TCP, JSON bodies; **no WebSocket, no SSE, no push** |
| Address | `http://127.0.0.1:31270`, localhost-only by default; `engine.ini` `[HTTPServer.Listeners] DefaultBindAddress=0.0.0.0` opens it to the LAN (not needed — our agent is co-resident) |
| Auth | `DTGCommKey: <key>` header on every request; key auto-minted at first `-HTTPAPI` launch into `Documents\My Games\TrainSimWorld6\Saved\Config\CommAPIKey.txt`; can change, so read it dynamically; wrong key → 403 `dtg.comm.InvalidKey` |
| Versions | same API in TSW5 and TSW6 (key file directory differs); TSW6 Build 493 reports API 1.5; `GET /info` returns `GameName`, `GameBuildNumber`, `APIVersion` for defensive checks |
| Quirk | one client enforces HTTP/1.1 explicitly for TSW6 **[verify]** |

Routes:

| Method | Path | Purpose |
|---|---|---|
| GET | `/info` | game + API metadata, route list |
| GET | `/list[/<node-path>]` | children (`Nodes`) + endpoints (`Endpoints[{Name, Writable}]`) of a node |
| GET | `/get/<node-path>.<endpoint>` | read → `{"Result":"Success","Values":{...}}` |
| PATCH | `/set/<node-path>.<endpoint>?Value=X` | write (number/string/bool) |
| POST | `/subscription/<path>?Subscription=<id>` | add a path to a server-side batch |
| GET | `/subscription?Subscription=<id>` | read the whole batch in one request |
| DELETE | `/subscription?Subscription=<id>` | drop the batch |
| GET | `/listsubscriptions` | enumerate batches |

Node grammar: nodes separated by `/`, endpoint after `.`
(`CurrentDrivableActor/Throttle(Lever).InputValue`). Endpoints come in
three flavors: bare (`InputValue`, `ObjectClass`), `Property.*`, and
`Function.*`. Units: m/s for speed, Pascals for pressure, meters for
distance, 0–1 normalized for controls.

Key root nodes: `CurrentDrivableActor` (the driven vehicle: controls +
HUD functions + deep `Simulation/` subtree), `DriverInput` (per-cab
control surface), `VirtualRailDriver` (simplified normalized
throttle/brake/reverser abstraction), `DriverAid` (route data ahead),
`CurrentFormation` (consist by index), `Timetable` (all vehicles in the
scenario, by GUID), `TimeOfDay`, `WeatherManager` (writable).

**Standardization verdict: the *meta-model* is standardized; the *names*
are not.** Every control on every train exposes the same endpoint set
(`InputValue`, `Interacting`, `Function.GetNotchCount`,
`GetMinimum/MaximumInputValue`, `Property.bDefaultToPressed`, …), and
the global nodes (`DriverAid`, `TimeOfDay`, HUD functions) are uniform.
But control node names are vehicle-specific: power is `Throttle`,
`MasterController`, `Throttle_F`, or `Throttle(Lever)` depending on
region and stock; safety acknowledge is `AWS_ResetButton` (UK),
`PZB_Acknowledge` (DE), or `AlerterReset(Button)` (US); German dual-cab
locos suffix `_F`/`_B`. Every community tool therefore re-runs
`/list/CurrentDrivableActor` after boarding and classifies controls:
has `Function.GetNotchCount` → lever; has `Property.bDefaultToPressed`
→ button. So: **partially standardized — plan for per-locomotive role
mapping over a standardized discovery mechanism.** This is exactly the
`capabilities` model Doug already has, one level deeper.

## 4. Telemetry actually available

Against the task's checklist. ✔ = confirmed in community implementations,
✔? = confirmed to exist but **[verify]** exact path/coverage per loco,
✗ = not found in any surveyed client.

| Field | Verdict | Where |
|---|---|---|
| speed | ✔ | `CurrentDrivableActor.Function.HUD_GetSpeed` (m/s) |
| throttle position | ✔ | discovered lever node `.InputValue` / `.Function.GetCurrentNotchIndex` |
| reverser | ✔ | discovered lever node |
| train brake | ✔ | discovered lever node |
| independent/loco brake | ✔ | discovered lever node (where fitted) |
| dynamic brake | ✔ | discovered lever node (where fitted) |
| brake pipe / cylinder / main res pressures | ✔? | `HUD_GetBrakeGauge_1/_2` return needle values in Pa (e.g. `{"WhiteNeedle (Pa)", "RedNeedle (Pa)"}`); which physical gauge each needle is varies per loco; deeper `Simulation/` nodes exist |
| RPM | ✔? | `Simulation/` subtree, per-loco **[verify]** |
| amps / current | ✔ | `HUD_GetAmmeter` |
| tractive effort | ✔? | `Simulation/` subtree **[verify]** |
| wheel slip | ✔ | `HUD_GetIsSlipping` |
| horn, bell, headlights, wipers | ✔ | discovered control nodes (`Horn`, `Bell` (US), `Headlights`, `Wiper*`), readable and writable |
| safety-system state (AWS/PZB/LZB/SIFA/alerter) | ✔ | per-loco nodes, e.g. `CurrentFormation/0/PZB_Service_V2.Property.ActiveMode`, `MFA_Indicators.Property.B_IsActive` — the Zusi-bridge project drives a physical PZB/SIFA/LZB panel from them |
| locomotive identity | ✔ | `CurrentFormation/0.ObjectClass` → `RVM_<ROUTE>_<CLASS>_<VARIANT>_C` (note: index 0 is not always the driven cab) |
| consist | ✔ | `CurrentFormation/<i>` per vehicle (`ObjectClass`, `LatLon`) |
| route | ✔? | route prefix is parseable from `ObjectClass`; no dedicated route node found |
| scenario / service | ✔ | `DriverAid.PlayerInfo.currentServiceName` |
| current objective | ✗ | not found exposed |
| distance/location | ✔ | `DriverAid.PlayerInfo.geoLocation` (lat/lon), `TrackData.lastPlayerPosition` (height, tunnel) |
| world coordinates | ✔ | as above + `CurrentFormation/<i>.LatLon` |
| gradient | ✔ | `DriverAid.Data.gradient` |
| speed limit | ✔ | `DriverAid.Data.speedLimit`, `nextSpeedLimit`, `distanceToNextSpeedLimit`, plus track/service/formation max speeds |
| signal state | ✔ | `DriverAid.Data`: `signalSeen`, `distanceToSignal`, `signalAspectClass`, `bSignalIsPermissive` — **next signal only**, no full signalling model |
| upcoming signals (plural) | ✗ | only the next one |
| next station / objective | ✗ | not found in any surveyed client |
| timetable (times) | ✗ | `Timetable` node lists vehicles, not schedules; no stop times found |
| weather | ✔ | `WeatherManager.*` (also writable) |
| game/session state | ✔ (derived) | `/info` answers ⇒ process up; vehicle nodes `NodeValid` ⇒ session; see §10 |

The three ✗ rows are the real gaps: **no next-station, no schedule
times, no objective text.** The RAIL tab design (§11) treats those as
NOT AVAILABLE from day one rather than promising them. The probe should
still sweep `/list` for them on the real build — the tree is explicitly
non-exhaustive and the surveyed clients may simply not use them
**[verify]**.

## 5. Writable controls actually available

Writes are `PATCH /set/<path>?Value=X` and are **real continuous
control-position writes**, not keypresses:

- Levers take normalized 0–1 (`InputValue`); values between notches snap
  to the nearest notch; `notch_value = notch_index / (notch_count − 1)`;
  `GetNotchCount` / `GetMinimum/MaximumInputValue` give the mapping
  programmatically. Some signed controls use −1–1.
- Confirmed writable in working projects: throttle, train brake, dynamic
  brake, reverser, horn, bell, headlights, wipers, sander, PZB/SIFA
  acknowledge, doors, couplers, MFD buttons — anything `/list` flags
  `Writable`, which is per-loco but comprehensive (the MIDI bridge and
  trenino drive whole cabs this way).
- `VirtualRailDriver` is the simplified alternative: `Enabled`,
  `Throttle`, `Brake`, `Reverser` — normalized, loco-independent, but
  coarse (RailDriver vocabulary only) and it disables a physical
  RailDriver while on.
- Caveats: keyboard/in-game input can override an API-set position at
  any time (re-assert or re-read periodically); writes take effect
  immediately; there is no server-side rate limiting, so *we* are the
  rate limiter.

This satisfies section B of the brief in full, pending live
verification. Notably the API's notch metadata solves programmatically
what CobraOne solves with hand-built per-loco spreadsheets.

## 6. HOTAS integration options

Goal: existing flight HOTAS (DirectInput HID) drives the locomotive.

**Direct approach (recommended):**

    HOTAS → Doug Windows agent (HID read) → PATCH /set/... → TSW6

The agent gains a small input layer — on .NET, `Windows.Gaming.Input`
(UWP-style, no native deps) or SharpDX.DirectInput — samples axes at
~30–60 Hz, applies the per-loco role map (axis → control node), converts
axis position → notch or continuous value using the *API's own*
`GetNotchCount`/min/max metadata, and PATCHes only on change beyond a
dead-band. Buttons map to momentary writes (`InputValue=1` then `0` for
`bDefaultToPressed`-style controls) or toggles. Everything stays inside
the agent; Doug's Pi side never sees a HID concept — it only ever sees
the same observed telemetry.

The concept mapping from the brief (throttle axis → power, secondary
axis → train brake, stick Y → dynamic brake, stick X → independent
brake, hat → camera*, trigger → horn, thumb → bell, buttons →
lights/wipers/acknowledge) is expressible as a per-loco *role map* JSON
— the same shape every community tool converged on. (*Camera is the one
row with no API path found; camera switching may need keyboard
emulation, which Doug policy labels open-loop or omits. **[verify]**)

**CobraOne approach (studied, not chosen):**

    HOTAS → CobraOne GUI → fake PieHid64.dll → TSW's RailDriver code

Works TSW2→TSW6 with the game's own per-loco RailDriver mappings for
free — that was its killer feature before the API existed. Costs today:
closed-source freeware from a forum Dropbox link; overwrites a game DLL
(re-patched after updates); limited to the RailDriver control vocabulary
(5 levers + rotaries + 44 buttons); inherits the native RailDriver
code's TSW5/6 twitchiness bugs; an extra always-running third-party app
— exactly the dependency the brief said to avoid.

**What CobraOne teaches regardless** (§7 has the comparison): per-loco
lever maps are unavoidable (the API's naming variance is the same
problem his spreadsheet solves); calibration matters (axis min/max/
center/dead-band per physical device); notch handling must be explicit
(snap vs. continuous per control); and users need a mapping UI
eventually. Phase 5 (§15) sequences all of that after telemetry is
proven.

## 7. Direct API vs CobraOne vs RailDriver emulation

| | Direct TSW6 API | CobraOne RD&JI | Raw PieHid64 emulation (skaako/problemo57) |
|---|---|---|---|
| Mechanism | official HTTP API | closed-source GUI + fake PieHid64.dll | fake PieHid64.dll, DIY feeder |
| Reads telemetry | **yes — full tree** | no (input only) | no (input only; LED speedo out) |
| Writes controls | yes, continuous, any writable control | yes, RailDriver vocabulary only | yes, RailDriver vocabulary only |
| Control fidelity | per-control notches via API metadata | game's native RD per-loco maps | game's native RD per-loco maps |
| Loco coverage | any loco, discovered at runtime | locos with RD mappings (+ his spreadsheet) | same |
| Survives game updates | API versioned via `/info` **[verify stability]** | DLL re-copied per update | same |
| Third-party runtime dep | none | CobraOne app must run | none (but DIY everything) |
| Source / license | n/a (we write the client) | closed, no license | skaako no license; problemo57 MIT |
| Doug fit | agent-native, one process | parallel architecture, fragile UI coupling | wrong direction: input-only |
| Verdict | **chosen** | studied for lessons | reference only |

The deciding asymmetry: RailDriver-emulation paths are *input-only* —
they could never feed the RAIL tab. We would still need the HTTP API for
telemetry, at which point the API might as well carry control too.

## 8. Recommended provider architecture

**One workstation agent process, multiple providers.** Evolve
`flightdeck-sim-agent` rather than spawning a sibling: same :9109, same
token, same logon-task deployment, same "expected to disappear with
Windows" lifecycle. The TSW provider is a pure-HTTP client — no new
native dependencies — so co-hosting costs nothing, and `/health` becomes
the single place deck-api learns what the workstation can currently do.

```
flightdeck-sim-agent (:9109)
├── MsfsProvider   — SimBridge → SimConnect → MSFS        (exists)
├── TswProvider    — HTTP client → localhost:31270 → TSW6  (new)
├── MediaBridge                                            (exists)
└── HttpApi
    ├── GET /            GET /status       liveness (unchanged)
    ├── GET /health      + providers block (see below)
    ├── GET /state  /events  POST /command  MSFS, unchanged — no deck-api break
    └── GET /rail/state  /rail/events  POST /rail/command   new, same shapes
```

Provider contract (in-process interface, and the `/health` shape):

```json
"providers": {
  "msfs": { "application": "msfs2024",  "connected": true,
            "last_seen": 1785956301.2,
            "capabilities": ["aircraft","flight","position","navigation"],
            "metadata": { "aircraft": "Beechcraft Baron G58" } },
  "tsw":  { "application": "train_sim_world_6", "connected": false,
            "last_seen": null, "status": "no_api",
            "capabilities": [], "metadata": {} }
}
```

Rules carried over from the MSFS design, now stated as provider law:

- **Game vocabulary stays inside the provider.** `TswVars`-equivalent
  (node paths, role heuristics, per-loco profiles) is the only place TSW
  path strings live; `TswNormalize` emits RailTelemetry; nothing TSW-
  shaped crosses the LAN.
- **Capabilities are reported, not assumed.** A loco with no dynamic
  brake yields a RailTelemetry with `dynamic_brake` absent.
- **Loss is normal.** Provider down ⇒ `connected:false` with a reason;
  no retry alarms.
- **Domain schemas are separate.** RailTelemetry is not FlightTelemetry
  with train words; they share the envelope (ts, seq, freshness,
  capabilities) and the transport, nothing else.

On the Pi, deck-api adds the mirror image (~60 lines, all existing
patterns): `fetch_rail_link()` on the ordinary poll for the strip +
episode logic; `/api/rail` fast-poll pass-through for the surface;
`/api/rail/command` forwarder (phase 4). `SURFACES` gains `"rail"`.

## 9. Proposed normalized RailTelemetry schema

Served by `GET /rail/state`, streamed on `/rail/events`. Field absent =
not fitted / not exposed by this loco — never zero, never null-as-zero.

```json
{
  "ts_source": 1785956301.05,      // when TSW answered the poll
  "ts_agent":  1785956301.12,      // when the agent published this
  "seq": 4821,
  "provider": "tsw",
  "status": "driving",             // §10 state machine

  "vehicle": {
    "class": "ES44C4",             // parsed from ObjectClass
    "object_class": "RVM_CJP_BNSF_ES44C4_C",
    "consist_length": 12,
    "service": "Barstow Yard Transfer"
  },

  "capabilities": ["throttle","reverser","train_brake","independent_brake",
                   "dynamic_brake","horn","bell","headlights","wipers",
                   "safety_alerter","ammeter","wheel_slip"],

  "drive": {
    "speed_ms": 21.4,
    "speed_limit_ms": 26.8,
    "next_speed_limit": { "limit_ms": 17.9, "distance_m": 812 },
    "gradient_pct": -0.4,
    "throttle":          { "input": 0.500, "notch": 4, "notches": 9 },
    "reverser":          { "input": 1.0,   "position": "forward" },
    "train_brake":       { "input": 0.0,   "notch": 0, "notches": 7 },
    "independent_brake": { "input": 0.0 },
    "dynamic_brake":     { "input": 0.0 }
  },

  "power": { "ammeter_a": 480, "wheel_slip": false },

  "brakes": { "gauge_1": { "white_kpa": 720, "red_kpa": 0 },
              "gauge_2": { "white_kpa": 393, "red_kpa": 386 } },

  "safety": { "system": "alerter", "state": "idle" },

  "aux": { "headlights": "on", "bell": false, "horn": false, "wipers": "off" },

  "route": {
    "position": { "lat": 34.8958, "lon": -117.0173 },
    "next_signal": { "seen": true, "distance_m": 1240,
                     "aspect": "Clear", "permissive": false },
    "in_tunnel": false
  },

  "world": { "sim_time": "2026-08-06T14:32:05", "weather": {
    "precipitation": 0.0, "cloudiness": 0.3, "temperature_c": 28.1 } }
}
```

Notes: everything is SI at this boundary (m/s, kPa, meters); the UI
formats mph vs km/h — ideally by the loco's region parsed from
`ObjectClass`. `ts_source` vs `ts_agent` is the two-clock rule from
deck-api applied per hop: the Pi adds its own receive time, and the UI
computes staleness from *its* clock against the freshest hop it can
trust. `safety` starts minimal (system name + coarse state) — PZB/LZB
modes are per-loco and phase-later.

## 10. Connection/state machine

Section H of the brief demanded distinct states; here they are, with
their observable evidence. The agent's TswProvider derives them; nobody
downstream re-derives:

| # | State | Evidence | Panel copy |
|---|---|---|---|
| 0 | `no_windows` | (Pi-side: workstation not in Windows) | RAIL TELEMETRY OFFLINE · Requires Windows + Train Sim World |
| 1 | `no_api` | `GET /info` refused/unreachable | WINDOWS ONLINE · TRAIN SIM WORLD NOT RUNNING |
| 2 | `no_key` | key file missing/rejected (403) | TSW RUNNING · API KEY NOT ACCEPTED (a *fault*, shown as one) |
| 3 | `api_up` | `/info` answers; vehicle nodes `NodeValid:false` | TRAIN SIM WORLD · WAITING FOR TELEMETRY (menus / loading / on foot) |
| 4 | `driving` | subscription entries valid and refreshing | TRAIN SIM WORLD 6 · TELEMETRY ACTIVE |
| 5 | `stale` | last good sample age > threshold while 1–4 claim otherwise | TELEMETRY LOST · last contact: [age] |

Transitions worth naming: 4→3 on leaving the cab (values go
`NodeValid:false`, not absent-process); any→1 on game exit (connection
refused); 2 is the only state that is an error rather than an absence —
matching the SIM surface's rule that a rejected token is a fault worth
reading while an absent agent is ordinary life. "TSW online" is
*never* defined as "process exists": the process is not even probed;
the API answering **is** the definition, and gameplay is a separate
state above it.

Freshness bands (UI-side, from `ts_agent` + Pi receive time):
**FRESH** < 2 s · **AGING** 2–10 s (dim + age stamp) · **STALE** > 10 s
(values withdrawn to `—`, banner shows last-contact age) ·
**UNAVAILABLE** (state ≤ 3: `—`, no numerals at all) ·
**NOT SUPPORTED** (capability absent: tile omitted or `NOT FITTED`) ·
**NOT APPLICABLE** (capability meaningless for this vehicle class).
A stopped train is state 4 with `speed_ms: 0.0` and FRESH; a dead
source is state 5 with no number on screen. The component knows the
difference because the two never share a rendering path.

Reconnect discipline (from the community consensus, §19): exponential
backoff capped ~30 s; re-read `CommAPIKey.txt` on every reconnect;
DELETE-then-recreate subscriptions on session start; fall back to
individual GETs if a subscription goes wedged; expect the API to answer
nothing useful until an in-game session exists.

## 11. RAIL tab UX / state proposal

Nav strip gains `RAIL` beside `SIM` with the same sub-label grammar:
`no link` → `no game` → `menus` → `<loco class>` when driving. The
surface reuses the placeholder pattern for states 0–3 and renders the
live panel only in state 4, with the `.stale` treatment past 2 s exactly
as SIM does.

Layout, in Doug's visual language (simt tiles, cased type, EFIS palette
— cyan for reference data, green/amber/red only for state):

```
RAIL // TRAIN SIM WORLD 6                                    ES44C4 · BNSF · Barstow Yard Transfer
┌──────────rail (left, ~600px)──────────┐┌──────────primary (~1100px)─────────┐┌──ops (~860px)──┐
│ SPEED        (big numeral, mph/km/h)  ││ THROTTLE  N4/8   ████████░░ bar    ││ NEXT SIGNAL    │
│ 47                                    ││ REVERSER  FWD                      ││ CLEAR · 0.8 mi │
│ LIMIT 60 · next 40 in 0.5 mi          ││ TRAIN BRK REL    ░░░░░░░░░░        ││ permissive: no │
│ (limit exceedance = amber numeral)    ││ INDEP     REL                      ││ GRADIENT −0.4% │
│                                       ││ DYN BRK   OFF                      ││ POSITION       │
│ BRAKE PIPE  720 kPa   MAIN RES 858    ││ (levers are read-only gauges in    ││ 34.896,-117.017│
│ AMPS 480    SLIP —                    ││  phase 2; hold-to-arm buttons      ││ SIM TIME 14:32 │
│ SAFETY alerter · idle                 ││  arrive with phase 4)              ││ WX light cloud │
└───────────────────────────────────────┘└────────────────────────────────────┘└────────────────┘
```

Rules, restated as build constraints:

- **No SaaS cards.** Levers render as labeled position bars with notch
  pips (the flap-pips pattern already exists); speed is one big numeral
  with the limit beside it; over-limit turns the numeral amber — state
  color used for state.
- **Unsupported ≠ zero.** A loco without a dynamic brake omits the DYN
  row (capability absent). A fitted-but-unreadable value shows `—`.
- **No next-station / timetable block** until §4's ✗ rows are disproven
  on the real build — the surface's shape stays honest about what the
  API gives.
- Speed unit follows the loco's region (UK/US mph, DE km/h), stated on
  the tile (`MPH` / `KM/H`) so the numeral is never ambiguous.
- Touch targets ≥158 px; future control buttons use hold-to-arm; no
  modal dialogs on a 720 px panel.

## 12. Boot orchestration integration

"train bootup" is the documented three-edit game addition plus one
deck-api line — **`flightsim-boot.sh` is untouched** (the target is
still just `windows`):

1. `pi/setup.sh`: new fauxmo device `{"name": "train sim", "port":
   49919, "on_cmd": "FLIGHT_INTENT=train …deck-api /api/boot…"}` (same
   state_cmd as the others). Alexa re-discovery + a routine for the
   phrase.
2. `windows/jarvis-greeting.ps1`: `$LaunchProfiles.train = @{ cmd =
   { Start-Process 'steam://rungameid/<TSW6-appid>' }; closing = 'The
   railway is yours, sir.' }`. Steam launches carry the stored
   `-HTTPAPI` launch option, which is set once by hand in Steam
   properties — that persistence is what makes voice boot → API
   available work with no per-boot flags. **[verify appid + that the
   URL path honors launch options]**
3. `pi/deck-api/deck_api.py`: `PROFILES["train"] = {"target":
   "windows", "label": "TRAIN SIM", "sub": "TSW6"}` — the DECK tile
   comes free from the existing tile rendering; `index.html` gains the
   button.

Works from all four starting states by construction: powered off → WOL →
Windows → logon → greeting reads intent `train` → launches TSW6; Linux
up → reboot leg; Windows up → orchestrator's existing "already up —
launching" leg fires `/launch`; TSW already running → launch is
idempotent-ish (Steam focuses the running game) and the agent's
TswProvider is already in state ≥ 3. **[verify Steam relaunch behavior
with the game already running]**

Episode wiring mirrors SIM: deck-api opens a `rail` episode when the
provider reaches `driving` (state 4) and closes it when the provider
drops below `api_up`, restoring the pre-episode surface. Phases 6–7 of
the boot track remain exactly as dark as they are today.

## 13. SimHub relationship / future provider strategy

Finding: **SimHub has no TSW support, native or plugin** — the only
rail-adjacent SimHub work reads Train Simulator *Classic* via
`RailDriver64.dll` shared memory. Routing TSW through SimHub was never
on the table technically, which settles section J's instinct: SimHub is
a *peer future provider* for racing/driving titles it actually supports,
not rail plumbing. The provider contract in §8 is what makes that future
cheap: a `SimHubProvider` would sit beside `MsfsProvider`/`TswProvider`
in the same agent, publish its own domain schema (DriveTelemetry, say)
behind its own capabilities, and reuse the envelope, freshness law,
health block and transport wholesale. Nothing needs designing today
beyond not hardcoding "two providers" anywhere.

## 14. Risks and unknowns

1. **The official PDF is unread.** Mitigated by five+ independent
   working implementations and by the probe run — but the probe run is
   mandatory before product code.
2. **API stability across game updates.** No versioning promises found;
   one dashboard ships a re-discovery script for this reason. Defense:
   check `/info` `APIVersion`/`GameBuildNumber` at connect, log
   mismatches, keep all paths in the one vocabulary file.
3. **Per-loco naming variance** is permanent. Defense: discovery +
   role heuristics with per-loco profile overrides (the pattern every
   surveyed tool converged on); un-mapped controls appear as
   capability-absent, not wrong.
4. **Polling costs frame rate.** No server-side rate limit; excessive
   GETs hit game FPS. Defense: subscriptions (one GET per tick), 2–4 Hz
   for the panel, 30–50 ms only ever agent-internal for HOTAS writes.
5. **No push transport.** Staleness detection is ours alone: the
   subscription answering yesterday's values looks identical to fresh
   ones except for our own clocks. Hence `ts_source`/`ts_agent`
   discipline and the change-detection heartbeat (speed or sim-time
   ticking is the liveness signal — sim time advances even when parked).
6. **Menus vs driving ambiguity** (`NodeValid:false` everywhere) —
   handled as first-class state 3, not an error.
7. **Key rotation** mid-session → 403; re-read the file on every
   reconnect and on any 403.
8. **HTTP/1.1 quirk** reported for TSW6 — trivial to honor, cheap to
   verify.
9. **Control contention**: keyboard/in-game input overrides API writes
   silently. The observed-state-only UI rule absorbs this; HOTAS phase
   needs a re-assert-on-diff policy decision.
10. **Gaps are real**: no next-station/timetable/objective data found;
    camera control path unknown. The RAIL tab design assumes their
    absence; finding them is upside, not risk.
11. **Licensing**: trenino is CC BY-NC — read for ideas, copy nothing.
    Several useful repos have no license at all; same rule.

## 15. Implementation phases

Each phase is provable on its own, and nothing in phase N+1 starts
until N's exit test passes.

- **Phase 0 — probe (this branch).** Run `scripts/tsw_probe.py` on the
  workstation: `--controls` dump for 2–3 locos across regions + a live
  telemetry session + a game-restart reconnect. Commit the captured
  output to `docs/tsw-probe-findings.md`. Settles every **[verify]**.
- **Phase 1 — TswProvider, read-only.** C# provider in the agent:
  key discovery, state machine (§10), subscription lifecycle,
  normalization to RailTelemetry, `/health` providers block,
  `/rail/state` + `/rail/events`. Exit: curl from the Pi shows live
  speed with honest `ts_source`, and killing TSW moves `status` through
  3→1 without the agent caring.
- **Phase 2 — RAIL surface, read-only.** deck-api link probe +
  `/api/rail` pass-through + `SURFACES`; deck-ui nav button, placeholder
  states 0–5, live panel per §11. Exit: the Edge shows live speed, and
  yanking TSW produces TELEMETRY LOST with an age, never a frozen 47.
- **Phase 3 — boot orchestration.** The three edits of §12 + rail
  episode. Exit: "train bootup" from powered-off lands in a cab with the
  RAIL tab live, hands never touching anything.
- **Phase 4 — control writes.** `POST /rail/command` with the SIM
  command discipline (accepted ≠ worked, PENDING/NO RESPONSE, hold-to-
  arm buttons for horn/bell/lights/brake notches). Exit: the gear-test
  equivalent — command a throttle notch, watch observed state move,
  command with the game paused, watch NO RESPONSE.
- **Phase 5 — HOTAS.** HID input layer in the agent, role-map profiles,
  calibration, dead-bands, re-assert policy. Exit: throttle axis drives
  the ES44C4 with keyboard override behaving predictably.
- **Phase 6 (someday) — more providers** per §13.

## 16. Proof-of-concept plan

`scripts/tsw_probe.py` (in this branch) is the spike: stdlib-only
Python 3, **read-only by construction** (it contains no PATCH call, so
it cannot manipulate the locomotive — discovery included). It: finds the
key file (highest-numbered `TrainSimWorld*`), connects and prints
`/info` identity, identifies loco + service + geo, walks
`/list/CurrentDrivableActor` classifying levers/buttons and printing
per-endpoint writability (`--controls` for the full dump), builds a
delete-then-create subscription over speed / brake gauges / ammeter /
slip / DriverAid + discovered levers, and live-prints at 2 Hz with
round-trip time and last-good age on every line — the freshness
demonstration — then falls back to the detect phase on connection loss
and reconnects cleanly across a game restart, re-reading the key.
Not wired into Doug in any way, exactly as required.

## 17. Files/modules in Doug that would change

| Phase | File | Change |
|---|---|---|
| 1 | `windows/sim-agent/TswVars.cs` (new) | the only file holding TSW path strings, role heuristics, per-loco overrides |
| 1 | `windows/sim-agent/TswBridge.cs` (new) | HTTP client, key discovery, state machine, subscriptions |
| 1 | `windows/sim-agent/TswNormalize.cs` (new) | raw JSON → RailTelemetry |
| 1 | `windows/sim-agent/Program.cs` | host both providers; providers block in `/health` |
| 1 | `windows/sim-agent/HttpApi.cs` | `/rail/state`, `/rail/events`, later `/rail/command` |
| 2 | `pi/deck-api/deck_api.py` | `SURFACES += rail`; `fetch_rail_link()`; `/api/rail`; later `/api/rail/command`; `PROFILES["train"]` |
| 2 | `pi/deck-ui/index.html` `deck.js` `deck.css` | RAIL nav button, surface, placeholder, poller, tiles |
| 3 | `pi/setup.sh` | fauxmo device `train sim` :49919 |
| 3 | `windows/jarvis-greeting.ps1` | `$LaunchProfiles.train` |
| 3 | `README.md`, `docs/DESIGN.md` | document the new surface + phrase |
| — | `pi/flightsim-boot.sh`, boot agents | **no change** |

## 18. Tests required

- **Agent (C#):** TswNormalize against canned JSON fixtures captured by
  the probe (per-region locos, a loco missing dynamic brake, a
  `NodeValid:false` menu frame); state-machine transitions 1↔3↔4↔5
  against a scripted fake HTTP server, including 403-then-new-key;
  subscription recreate-on-restart.
- **deck-api (Python, matching the existing self-contained style):**
  `test_rail.py` — link/session/reason envelope for each agent answer
  (down, 503-equivalent, 403, driving); episode open/close on provider
  state moves (extends `test_surfaces.py` patterns); `PROFILES` shape.
- **UI:** the existing approach is manual + selftest.html; minimum bar
  is the placeholder-state matrix exercised by pointing the panel at a
  mock deck-api response set.
- **End-to-end (manual, per phase exit tests):** kill-the-game staleness
  drill; boot-from-off drill; command-while-paused drill.

## 19. External projects/repositories examined

| Project | What it is | License | Used for |
|---|---|---|---|
| [albertorestifo/trenino](https://github.com/albertorestifo/trenino) | Arduino cab controllers via TSW6 API; best community API guide (`docs/TSW_API_GUIDE.md`) | **CC BY-NC 4.0** — ideas only, no code reuse | API reference, control-type heuristics, notch math |
| [gordonwaudio/TSW_Midi_Bridge_V2](https://github.com/gordonwaudio/TSW_Midi_Bridge_V2) | TSW6 API ⇄ MIDI bridge (Python) | none stated — no reuse | key re-read on reconnect, per-loco configs, subscription lifecycle, backoff |
| [Giako888/bridge-trainsimworld-zusi3-arduino](https://github.com/Giako888/bridge-trainsimworld-zusi3-arduino) | physical PZB/SIFA/LZB panel from TSW6 API | none stated — no reuse | safety-system node paths, 30 ms subscription polling, error-halt thresholds |
| [GarethLowe/tsw6-realtime-weather](https://github.com/GarethLowe/tsw6-realtime-weather) | real-weather injection (C#) | unverified | WeatherManager writes, Polly-style retry |
| [waaghals/tsw-inspector](https://github.com/waaghals/tsw-inspector) | API explorer + reverse-engineered OpenAPI spec | MIT | route/response schemas |
| [Steel-Horse-Simulations/Train-Sim-Hud](https://github.com/Steel-Horse-Simulations/Train-Sim-Hud) | desktop HUD (Python) | none stated | DriverAid usage, "API answers only in session" |
| [rolidepo/TSWDash](https://github.com/rolidepo/TSWDash) | LAN dashboards (Python) | none | endpoint-drift warning + re-discovery pattern |
| [TheJAG/tsw_connect](https://github.com/TheJAG/tsw_connect) | GIS extraction via API | MIT | corroboration |
| [fsunt-ut/timtim-tsw](https://github.com/fsunt-ut/timtim-tsw) | NS TIMTIM display replica | LGPL-2.1 | API 1.5 / Build 493 datum |
| [felixlindemann/TSW6Controller](https://github.com/felixlindemann/TSW6Controller) | ESP32 hardware controller | MIT | LAN-exposure proxy pattern, port confirmation |
| [ShaneioCantrai/TrainDeck](https://github.com/ShaneioCantrai/TrainDeck) | Android cab controller + bridge | proprietary (source-visible) | control-snapshot-per-loco pattern |
| [LiahMartens/tsw-controller-app](https://github.com/LiahMartens/tsw-controller-app) | joystick→train bridge, shared per-loco profiles | none stated | HOTAS-profile prior art; TSW path unconfirmed |
| [problemo57/tsw-raildriver-emulator](https://github.com/problemo57/tsw-raildriver-emulator) | fake PieHid64.dll, full C source | MIT | PieHid API surface (`EnumeratePIE`, `SetDataCallback`, VID 0x05F3/PID 0xD2) |
| [skaako/raildriver](https://github.com/skaako/raildriver) | fake PieHid64.dll + Arduino feeder | none (DLL source absent) | RailDriver report format (7 analog + 44 buttons) |
| [travisolbrich/Arduino-Emulated-Raildriver](https://github.com/travisolbrich/Arduino-Emulated-Raildriver) | hardware-level RD impersonation | MIT | alternative-approach context |
| [vanlueckn/TSW5Mods](https://github.com/vanlueckn/TSW5Mods) + [docs](https://vanlueckn.github.io/tsw-websocket-docs) | UE4SS WebSocket mod (:9187), pre-official-API | MIT | why not: injected mod, superseded |
| CobraOne "TS World RailDriver and Joystick Interface" — [DTG forum thread 61440](https://forums.dovetailgames.com/threads/ts-world-raildriver-and-joystick-interface.61440/) | closed-source RD emulation GUI, TSW2→TSW6, v2.0.0.4-ish via Dropbox | closed, none | §6/§7 comparison; lever-map/calibration lessons |
| [perpetualKid/RailDriverSDK.NET](https://github.com/perpetualKid/RailDriverSDK.NET), [piengineering/PIEHidNetCore](https://github.com/piengineering/PIEHidNetCore.dll) | managed PieHid SDKs | (SDK terms) | context |
| [temoi/Simhub-Railworks](https://github.com/temoi/Simhub-Railworks) | TS *Classic* → SimHub | unverified | SimHub verdict (§13) |
| Dovetail official: [forum thread 94488 "Train Sim World API Support"](https://forums.dovetailgames.com/threads/train-sim-world-api-support.94488/) (docs PDF v1.5), [ThirdRails unofficial PDF](https://thirdrails.org/Downloads/TSW_API_Unofficial_Documentation.pdf), [RailDriver support page](https://piengineering.com/pages/rd-train-sim-world) | primary sources, unreachable from this environment | — | flagged **[verify]** |

License rule applied throughout: architectural patterns were studied
everywhere; code is reused from nowhere. Anything we write is original
against the API's own shapes.

## 20. GO / NO-GO

**GO**, on the direct-API path, gated on Phase 0: if the probe run on
the real TSW6 build confirms `/info`, discovery, subscription telemetry
and per-endpoint writability as documented here, proceed to Phase 1
without revisiting this document's architecture. If the probe finds the
API materially different from §3–§5, stop and amend this document first
— the design deliberately concentrates everything TSW-shaped into three
new agent-side files precisely so that a surprise there stays there.
