# flightdeck-sim-agent

Windows-only service that owns all SimConnect interaction and exposes a small
token-guarded LAN API to `deck-api` on the Pi. Full rationale in
[docs/SIM-AGENT-BRIEF.md](../../docs/SIM-AGENT-BRIEF.md).

```
XENEON touch -> deck-ui -> deck-api (Pi) -> LAN -> flightdeck-sim-agent (Windows)
                                                            | SimConnect
                                                        MSFS 2024
                                                            |
aircraft state -> SimConnect -> agent -> deck-api -> SSE -> XENEON
```

The Pi never loads or speaks SimConnect, and no SimVar name, unit or event id
crosses this boundary.

## Why C#

`Microsoft.FlightSimulator.SimConnect.dll` is the officially supported binding
and ships in the MSFS SDK, which Step 0 requires installing anyway. More to the
point it gives **push** data via `SIMCONNECT_PERIOD.SIM_FRAME` rather than
polling — and catching gear percent *mid-travel* is the whole product.
Python-SimConnect is a polling wrapper with a request cache sitting exactly
where that number needs to be uncached, and bundles an MSFS-2020-era native DLL.

## Layout

| File | Holds |
|---|---|
| `SimVars.cs` | **Every** MSFS name, unit and event id. The only file Step 0 corrects. |
| `SimBridge.cs` | The SimConnect session and its pump thread. |
| `Normalize.cs` | Raw values in, Flight Deck shape out. The membrane. |
| `HttpApi.cs` | TcpListener, token guard, SSE. |
| `Program.cs` | Snapshot ownership, sequence numbers, command mapping. |

## Step 0 — before trusting any of this

Open the SDK's **SimvarWatcher**, load the **Beechcraft Baron G58**, and confirm
every row in `SimVars.cs`. Four rows are flagged `[Q1]`–`[Q4]` as known-doubtful:

- **[Q1]** `GEAR TOTAL PCT EXTENDED` — requested as `Percent`. Parts of the docs
  say `Percent Over 100` (0..1). Cycle the gear and read the range.
- **[Q2]** `FLAPS NUM HANDLE POSITIONS` — should read 3 on the Baron.
- **[Q3]** `PARKING_BRAKES` is toggle-only here; explicit set/off is synthesised
  by comparing against observed state first. Check whether
  `PARKING_BRAKE_SET` takes a 1/0 parameter.
- **[Q4]** `capabilities.autothrottle` is hardcoded `false` — no capability
  SimVar identified.

Unrecognised names surface as `NAME_UNRECOGNIZED` on stderr rather than as a
silently zeroed field, so run the agent with the sim up and watch the console.

## Build and install

Needs the .NET 8 SDK and the MSFS SDK.

```bash
powershell -ExecutionPolicy Bypass -File windows\setup-sim-agent.ps1
```

Registers `FlightDeckSimAgent` as a **logon** task running as the signed-in
user — unlike `FlightSimBootAgent`, which is SYSTEM-at-startup because it must
answer before anyone signs in. This one has nothing to talk to until MSFS is up,
and MSFS runs in the user's session.

## API — :9109

Token: first line of `C:\ProgramData\dualboot\sim-agent.token`, as an
`X-Auth-Token` header or `?token=`. Wrong token or non-RFC1918 source is 403.

| | |
|---|---|
| `GET /` · `GET /status` | `sim-agent` — unauthenticated liveness, mirrors the boot agent |
| `GET /health` | agent version, live-session flag, loaded aircraft, uptime |
| `GET /state` | one normalized snapshot; 503 before the first frame |
| `GET /events` | SSE, same shape, on change and at ≥ 4 Hz |
| `POST /command` | `{cmd_id, control, action, value}` |

`connected` means a live SimConnect session, not that the process is running.
A control the aircraft does not have is **absent** from `controls`, so the panel
greys it with a reason instead of showing a dead button.

`accepted` means the event was transmitted. It never means it worked. When the
requested state already matches the observed one nothing is transmitted and the
reply carries `"noop": true`, so the panel knows not to wait for movement.

## The rule that matters most

The UI never renders the commanded state — only the observed one. Nothing in
this codebase writes a commanded value into the published state; the only thing
that moves `gear.state` is the sim reporting that the gear moved. That is what
makes a command MSFS ignored show up as a failed command rather than a lie.

## Expected to disappear

The agent starts with Windows and vanishes on every reboot into Linux. Normal
operation. Connect failures are quiet and retried forever with no alarm state.
