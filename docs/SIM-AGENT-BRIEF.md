# Brief — build `flightdeck-sim-agent` (Windows side)

Self-contained. Paste into a fresh chat on the Windows workstation; it
assumes no prior context.

---

## What exists already

**Flight Deck** is a 2560 × 720 Corsair XENEON Edge touch panel on a Raspberry
Pi 4B (`Node1`, user `fvm3`), running as a Wayland client under the Pi's
existing labwc session. It has four surfaces — DECK, EVALS, MEDIA, SIM — behind
a persistent 72 px navigation strip. DECK and EVALS work today; SIM is an empty
placeholder reading *no link*. That placeholder is what this brief fills.

Repo: `JustLikeFrank3/dualbootautomationproject`, branch
`claude/multi-os-boot-dashboard-5qgfio`. Design notes in `docs/DESIGN.md`.

| Host | Address | Runs |
|---|---|---|
| Pi (`Node1`) | `192.168.68.51` LAN · `192.168.101.2` direct link | `deck-api` on :8088, the panel, k3s + Prometheus + Grafana (jobContext's, untouched) |
| Workstation, Windows | `192.168.68.50` | `boot-agent.ps1` on :9107, GPU exporter on :9106, MSFS 2024 |
| Workstation, Linux | `192.168.101.1` direct link | exporter on :9105/metrics |

The workstation dual-boots. The Pi is the always-on node.

## The task

Build a **Windows service that owns all SimConnect interaction** and exposes a
small LAN API to `deck-api` on the Pi.

```
XENEON touch → deck-ui → deck-api (Pi) → LAN → flightdeck-sim-agent (Windows)
                                                      ↓ SimConnect
                                                  MSFS 2024
                                                      ↓
aircraft state → SimConnect → agent → deck-api → SSE → XENEON
```

**The Pi must never load or speak SimConnect.** SimConnect is Windows-only;
both common wrappers (Python-SimConnect, node-simconnect) require a Windows
host with it installed. The agent publishes *normalized Flight Deck state* —
no SimVar names, event ids or raw units leak onto the Pi or into the UI.

## Step 0 — verify before writing code

**Do this first and report the results.** Open the MSFS 2024 SDK's
**SimvarWatcher** sample, load the **Beechcraft Baron G58**, and confirm every
name, unit and writability below against the actual aircraft. MSFS DevSupport
carries live threads about `GEAR POSITION` not matching its own documentation,
so treat the table as a starting point, not gospel.

| Control | State (SimVar) | Command (Event) |
|---|---|---|
| Gear | `GEAR HANDLE POSITION` (bool), `GEAR TOTAL PCT EXTENDED` (percent) | `GEAR_UP` · `GEAR_DOWN` · `GEAR_TOGGLE` |
| Flaps | `FLAPS HANDLE INDEX` (number), `FLAPS NUM HANDLE POSITIONS`, `TRAILING EDGE FLAPS LEFT ANGLE` (degrees) | `FLAPS_INCR` · `FLAPS_DECR` · `FLAPS_SET` |
| Parking brake | `BRAKE PARKING POSITION` (bool) | `PARKING_BRAKES` |
| Landing lights | `LIGHT LANDING` (bool) | `LANDING_LIGHTS_ON` / `_OFF` — prefer explicit over toggle, so a dropped frame cannot invert state |
| AP master | `AUTOPILOT MASTER` (bool) | `AP_MASTER` |
| Readouts | `AIRSPEED INDICATED` (knots), `INDICATED ALTITUDE` (feet), `PLANE HEADING DEGREES MAGNETIC` (**radians**) | — |
| Capabilities | `IS GEAR RETRACTABLE`, `FLAPS AVAILABLE`, `FLAPS NUM HANDLE POSITIONS`, `SPOILER AVAILABLE`, `AUTOPILOT AVAILABLE` | — |

Heading arrives in radians. Convert **in the agent**, never in the UI.

Also decide the implementation language and say why. C#/.NET is the natural
fit — `Microsoft.FlightSimulator.SimConnect.dll` ships with the SDK and is the
officially supported path. Python + Python-SimConnect is acceptable if
preferred; it is a ctypes wrapper over the same native DLL. The existing
`boot-agent.ps1` is PowerShell with a hand-rolled `HttpListener`, so mirror its
token model regardless of language.

## The API contract — deliberately tiny

Listen on **:9109**. *(Not :9108 — under Linux the workstation already serves
:9108 as a liveness probe returning the string `linux`.)*

Token-guarded exactly like `boot-agent.ps1`: a shared token in
`C:\ProgramData\dualboot\sim-agent.token`, wrong token gets 403, LAN-only.
Add a firewall rule covering the **Public** profile — Windows usually assigns
that to this LAN, and a Private-only rule fails silently.

### `GET /health`

```json
{ "agent": "flightdeck-sim-agent", "version": "0.1.0",
  "sim": { "connected": true, "name": "MSFS 2024",
           "aircraft": "Beechcraft Baron G58" },
  "uptime_s": 1234 }
```

`connected` means a live SimConnect session, not "the process is running".
deck-api uses this to decide SIM is available.

### `GET /state`

```json
{ "ts": 1785956301.2, "seq": 4821,
  "aircraft": "Beechcraft Baron G58",
  "capabilities": { "gear_retractable": true, "flap_detents": 3,
                    "autopilot": true, "autothrottle": false,
                    "speedbrake": false },
  "controls": {
    "gear":           { "state": "down|transit|up", "pct": 58, "handle": "up" },
    "flaps":          { "index": 0, "detents": 3, "angle_deg": 0.0 },
    "parking_brake":  { "state": "set|off" },
    "landing_lights": { "state": "on|off" },
    "ap_master":      { "state": "engaged|off" }
  },
  "readouts": { "ias_kt": 142, "alt_ft": 4850, "hdg_mag": 271 } }
```

`gear.state` is derived from percent extended: 100 → `down`, 0 → `up`,
anything between → `transit`. That intermediate value is the single most
important thing this agent produces.

A control the aircraft does not have must be **absent** from `controls`, so
the panel can grey it with a reason rather than showing a dead button.

### `POST /command`

```json
{ "cmd_id": "c-183", "control": "gear", "action": "set", "value": "up" }
→ { "cmd_id": "c-183", "accepted": true, "seq": 4822 }
→ { "cmd_id": "c-183", "accepted": false, "reason": "sim not connected" }
```

`accepted` means *the event was transmitted*, *never* "it worked".

### Push

WebSocket `/stream` or SSE `/events`, emitting the `/state` shape on change.
Readouts at ≥ 4 Hz; controls on change. Polling at 4 Hz is an acceptable v1
fallback if push is awkward.

## The rule that matters most

**The UI never renders the commanded state — only the observed one.**

1. Panel sends a command with a `cmd_id`.
2. Agent transmits and returns `accepted`.
3. Panel shows `PENDING` as an overlay on the last **observed** state.
4. The state stream moves it: percent extended leaves 100 → `TRANSIT`, reaches
   0 → `UP`.
5. No movement within the control's timeout → panel shows `NO RESPONSE` and
   falls back to the last observed state, never the commanded one.

That is what makes a command the sim ignored — wrong aircraft, paused sim,
gear already up — show as a failed command instead of a lie.

## Expected to disappear

The agent starts with Windows and MSFS and vanishes on every reboot into
Linux. **That is normal operation, not an error.** deck-api treats its loss as
`SIM OFFLINE`, the strip shows SIM as *no link*, and the sim episode closes so
the panel returns to the wallboard. Do not add retry alarms or failure states
for this.

## Do not

- **No keyboard emulation** for any of the v1 five. A keystroke cannot be
  acknowledged, cannot be read back, and cannot tell you whether the sim was
  focused. It is permitted only where no SimConnect path exists at all, and
  must be labelled *open-loop — state not confirmed*.
- **No SimConnect on the Pi.**
- **No changes** to `flightsim-boot.sh`, `boot-agent.ps1`'s existing behaviour,
  or `jarvis-greeting.ps1`.
- **No Prometheus, Grafana, Loki, or metric retention.** Flight Deck introduces
  none; the Pi's existing stack belongs to jobContext and is untouched.
- **No MSFS-specific names, units or event ids** in anything the Pi sees.

## Done when

1. SimvarWatcher output confirms (or corrects) every name in the table above.
2. `GET /health` reports a live SimConnect session and the loaded aircraft.
3. `GET /state` returns the normalized block with gear percent updating live.
4. Tapping GEAR on the panel produces: command accepted → `TRANSIT` observed →
   settled state observed, with the UI following the *simulator*, not the
   command.
5. A command the sim ignores shows `NO RESPONSE`, not a false state.
6. The other four controls toggle and read back; IAS / ALT / HDG update.
7. Killing the agent puts the panel into `SIM OFFLINE` with no error state.

One correctly bidirectional GEAR control is worth more than twenty blind
macro buttons. Prove the loop on gear before adding anything else.
