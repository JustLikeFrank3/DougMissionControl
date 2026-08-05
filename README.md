# dualbootautomationproject

Voice-controlled boot orchestration for a dual-boot workstation. Say
*"Alexa, flight sim bootup"* and the machine wakes from off, or reboots
out of Linux, lands in Windows, greets you in a neural British voice, and
launches the sim — no keyboard.

The Alexa side needs no cloud account, no skill, and no subscription: a
Raspberry Pi pretends to be a handful of Wemo smart plugs, and Alexa
routines "turn them on."

```
"Alexa, flight sim bootup"
        │
        ├─ Alexa Speaks (routine): "Right away, sir. Bringing the flight deck online."
        └─ turns on virtual device "flight sim"
                │  (fauxmo Wemo emulator on the Pi)
                ▼
        flightsim-boot.sh on the Pi probes the workstation:
                │
                ├─ Windows up   → fire greeting + launch via boot-agent /launch
                ├─ Linux up     → ssh forced-command key → grub-reboot + reboot
                └─ no answer    → WOL magic packet; if GRUB lands in Linux,
                                  chains through the ssh path
                ▼
        Windows logs on → JarvisGreeting task reads the recorded intent,
        speaks the welcome, launches the matching game.
```

## The voice commands

| Phrase (yours to choose) | Virtual device | Port | Result |
|---|---|---|---|
| "flight sim bootup" | flight sim | 49915 | Windows + MSFS 2024 |
| "boot into Windows" | pc | 49917 | Windows, greeting only |
| "squadron bootup" | squadrons | 49918 | Windows + STAR WARS Squadrons |
| "workstation bootup" | workstation | 49916 | Linux + VS Code |

Each Windows device records *why* it booted (`FLIGHT_INTENT`) to
`/tmp/flightsim-intent` on the Pi. At logon, `jarvis-greeting.ps1` reads
that back over ssh and runs the matching entry in its `$LaunchProfiles`
table — so one greeting script serves every launch profile, and a
power-button boot (no intent recorded, or one older than 30 minutes)
launches nothing.

## Layout

```
pi/setup.sh              installs fauxmo + the orchestrator on the Pi, mints the ssh key
pi/flightsim-boot.sh     the orchestrator itself (→ /usr/local/bin on the Pi)
windows/setup.ps1        WOL, Fast Startup, logon task, boot agent, firewall, token
windows/jarvis-greeting.ps1   the spoken greeting + launch profiles (logon task)
windows/boot-agent.ps1        token-guarded :9107 endpoint (SYSTEM startup task)
linux/setup.sh           GRUB saved-default, boot-to-windows helper, WOL, greeting

Flight Deck — the touchscreen front-end (v0.1):
pi/display-check.sh      read-only: is the XENEON Edge's video + touch sound?
pi/setup-display.sh      cage + chromium kiosk on the Edge, mode pinning
pi/setup-deck.sh         installs deck-api, repoints fauxmo at it
pi/deck-api/deck_api.py  one entry point for every trigger; state + SSE
pi/deck-api/test_phases.py   phase mapping vs the orchestrator's real log lines
pi/deck-ui/              the panel itself (index/deck.css/deck.js) + selftest.html
```

## One-time setup (in this order)

Every address in this repo is an example — `192.168.1.50` is the
workstation, `192.168.1.51` the Pi, `192.168.100.1` a point-to-point link
to the workstation's Linux boot. Substitute your own throughout.

1. **Windows boot** (elevated PowerShell):
   ```
   powershell -ExecutionPolicy Bypass -File windows\setup.ps1 `
       -PiHost user@192.168.1.51 -AdapterName Ethernet
   ```
   — arms NIC WOL, disables Fast Startup, installs the JarvisGreeting
   logon task and the boot agent, prints the agent token and a BIOS +
   auto-logon checklist. `-PiHost` is baked into the installed greeting so
   it can read back which trigger caused the boot; a re-run without it
   keeps the value already installed.
2. **Pi** (from either boot):
   ```
   PI_HOST=user@192.168.1.51 PI_LAN_IP=192.168.1.51 \
   WS_LAN=192.168.1.50 WS_MAC=AA:BB:CC:DD:EE:FF \
   WS_BROADCAST=192.168.1.255 LINUX_SSH=user@192.168.100.1 ./pi/setup.sh
   ```
   — installs fauxmo and the orchestrator, writes those values to
   `/etc/flightsim/boot.env`, prints the Pi's public key. Add the token
   from step 1 to that file as `WIN_AGENT_TOKEN=`. The env file is the
   one place to correct an address later; re-runs keep your edits.
3. **Linux boot** (sudo, with the key from step 2):
   `sudo ./linux/setup.sh 'ssh-ed25519 AAAA… flightsim-boot@pi'`
   — GRUB_DEFAULT=saved, the `boot-to-windows` helper, the forced-command
   authorized_keys entry, persistent `ethtool wol g`, and the Linux logon
   greeting + VS Code autostart.
4. **BIOS**: enable "Resume by PCI-E/PME" (Wake on LAN) and **disable**
   ErP/EuP deep-off, or the NIC loses standby power at S5 and cold-boot
   wake silently fails.
5. **Auto sign-in** (needed for a truly hands-free cold boot — the
   greeting and launches are logon-triggered): `netplwiz` → uncheck
   "Users must enter a user name and password". On Windows 11 that
   checkbox is hidden until you set
   `HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device`
   → `DevicePasswordLessBuildVersion` = 0. Sysinternals Autologon also
   works and ignores the whole dance.
6. **Alexa app**: "Alexa, discover devices" finds the virtual plugs
   (they appear under Plugs). Then More → Routines → **+** → When:
   *Voice* → your phrase → Action 1 *Alexa Says* (your Jarvis line) →
   Action 2 *Smart Home* → the device → **On**.

## Flight Deck — the touchscreen (v0.1)

A Corsair XENEON Edge (2560 × 720, HDMI + USB HID touch) on the Pi, giving
the same four triggers a physical panel — plus the thing voice cannot: a
live picture of the ninety seconds after you ask. It hangs off the Pi rather
than the workstation, because the workstation is the subject of every
control on it.

```
./pi/display-check.sh          # prove video + touch; fix every ✗ before continuing
sudo ./pi/setup-display.sh     # cage + chromium kiosk, opens the touch self-test
sudo ./pi/setup-deck.sh        # deck-api on :8088, repoints fauxmo, real UI
```

`flightsim-boot.sh` is **not modified**. `deck-api` triggers it exactly as
fauxmo always has and derives boot phases by following the script's own
`journalctl -t flightsim-boot` output, so a boot started by voice, by touch,
or by hand all show up the same way.

| | |
|---|---|
| `GET /api/state` | everything the panel renders |
| `GET /api/events` | the same, streamed over SSE |
| `POST /api/boot` | `{"intent":"sim\|squadrons\|plain\|code"}` |
| `POST /api/launch` | target already up — fire greeting + profile |
| `POST /api/abort` | kill an in-flight run |
| `POST /api/hook/greeting` | for the Windows callback; not wired in v0.1 |

Loopback needs no token; anything else needs `DECK_TOKEN` from
`/etc/flightsim/boot.env`. Phases 6–7 (LOGON, LAUNCHED) stay dark until
`jarvis-greeting.ps1` gains a callback — v0.1 touches no Windows file, so a
run completes at **OS UP**.

There is deliberately no Prometheus, Grafana, Loki, or new exporter: this
targets a Pi 4B with 4 GB and a USB thumb drive, and a TSDB on flash with no
real wear levelling corrupts before it dies. State lives in tmpfs. See
[docs/DESIGN.md](docs/DESIGN.md) for the full picture and what a later
metrics plane would look like.

## Configuration

Most values live in `/etc/flightsim/boot.env` on the Pi (created by
`pi/setup.sh`, never overwritten):

| Variable | Meaning |
|---|---|
| `WS_LAN` | workstation's LAN IP — must be the same under both OSes (DHCP reservation by MAC) |
| `WS_MAC` | that NIC's MAC, the WOL target |
| `WS_BROADCAST` | subnet broadcast address for the magic packet |
| `LINUX_SSH` | user@host for the Linux boot (a direct-link IP is fine) |
| `WIN_AGENT_TOKEN` | must match `C:\ProgramData\dualboot\boot-agent.token` |
| `POLL_SECS` | how long to keep watching a boot (default 300) |

Hardcoded elsewhere and worth knowing about: the Pi's address in
`jarvis-greeting.ps1` (for the intent read-back), the device names and
ports in `pi/setup.sh`, and the launch commands in `$LaunchProfiles`.

### Adding a game

Three edits: a new device block in `pi/setup.sh` with
`FLIGHT_INTENT=<key>`, a matching entry in `$LaunchProfiles` in
`windows/jarvis-greeting.ps1` (command plus the Jarvis closing line), then
redeploy both sides, re-run Alexa discovery, and add a routine.

## How "is it up?" is decided

The orchestrator probes two Prometheus exporters that belong to a
separate project (a Grafana wallboard): `:9105` answering means Linux,
`:9106` means Windows. This repo does not install them — if you run
this without that stack, repoint `WIN_PORT`/`LINUX_PORT` at
anything that answers HTTP only under the OS in question. The boot
agent's own `:9107/status` is a natural Windows-up signal and is more
reliable than the exporter (which has died on its own more than once).

## Test matrix

| Starting state | Expect |
|---|---|
| Target OS already up | greeting + launch fire immediately, no reboot |
| Other OS up | reboot into the target in ~30 s, greeting on logon |
| Powered off | WOL powers on; straight to the GRUB default, else one chained reboot (~2 min) |

WOL cannot pick a GRUB menu entry, so from full-off the machine always
lands in the saved default first. Keep Linux as the default (needed for
the Windows→Linux leg to work at all) or flip it to Windows for faster
cold sim starts — not both.

## Troubleshooting

- **Alexa: "device isn't responding" / nothing reaches the Pi.** Check the
  Echo is on the same subnet as the Pi — a roaming Echo that rejoined a
  different SSID is the usual cause. `journalctl -u fauxmo` shows whether
  any probe arrived at all.
- **Alexa: "encountered a hardware malfunction".** Alexa polls device
  state right after turning it on; a boot takes minutes. The `state_cmd`
  therefore also reports ON while the boot lock is held.
- **Echo won't discover the devices.** fauxmo must answer on the Echo's
  subnet; retry discovery, and note newer Echo firmware can be picky about
  Wemo emulation. There is no "Wemo" entry under *Add device → Other* on
  current app versions — plain "discover devices" is the reliable path.
- **`cannot execute: required file not found` on the Pi.** A CRLF checkout
  broke the shebang. `.gitattributes` pins `*.sh` to LF and `pi/setup.sh`
  strips CR on install; if you hit it anyway, `sed -i 's/\r$//'`.
- **The Pi can't reach `:9107`.** The firewall rule must cover the profile
  Windows assigns your LAN — it is often *Public*, not *Private*, and a
  Private-only rule fails silently.
- **WOL does nothing from full off.** BIOS PME disabled, ErP deep-off
  enabled, or the last shutdown was from Linux without
  `flightsim-wol.service` armed. A lit link LED while off means standby
  power is present.
- **Machine wakes but nothing is spoken.** Nobody logged on — the greeting
  is a logon task. See auto sign-in above.
- Logs: `journalctl -t flightsim-boot` on the Pi (plus
  `~/flightsim-boot.log` for detached runs).

## Security notes

- The Pi holds no workstation credentials. Its ssh key is bound in
  `authorized_keys` to `command="sudo /usr/local/bin/boot-to-windows"`
  with `restrict`, and the sudoers rule covers exactly that one script —
  the key can reboot the box into Windows and nothing else.
- The boot agent runs as SYSTEM (it must reboot before anyone logs on) and
  is guarded by a shared token; a wrong token gets a 403. It is a
  hand-rolled HTTP listener on a LAN port — fine behind a home router,
  not something to expose to the internet.
- fauxmo is LAN-only, "off" is a deliberate no-op, and the state probe is
  a read.
- No secrets in the repo. The agent token is generated at install time.

## Origin

Extracted from a job-search project's `scripts/flightsim/` directory,
where it was living for no better reason than that's where it got
written. History preserved via `git subtree split`. Internal identifiers
keep the `flightsim` prefix (`/etc/flightsim`, `flightsim-boot.sh`, the
`FlightSimBootAgent` task) because renaming them would mean reinstalling
every deployed piece.

## License

MIT — see [LICENSE](LICENSE).
