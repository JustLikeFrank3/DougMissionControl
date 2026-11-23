# DougMissionControl

The panel says DOUG MISSION CONTROL; so does the repo now.

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
                ├─ Linux up     → ssh forced-command key → firmware BootNext
                │                 → reboot
                └─ no answer    → WOL magic packet; the firmware boot order
                                  is pointed at Windows, so it lands there
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
windows/efi-entry.ps1         finds the GRUB firmware entry, so /reboot can
                         hand back to Linux without writing to ext4
windows/set-primary-display.ps1  makes one monitor primary, so a profile can
                         say which screen its game opens on (`-List` to look)
linux/setup.sh           GRUB saved-default, boot-to-windows helper, WOL, greeting, media-agent
linux/set-boot-order.sh  points the UEFI boot order at Windows, so a COLD
                         power-on lands in the sim in one boot, not two
linux/boot-agent.py      token-guarded :9108 endpoint (the mirror of the Windows one)
linux/grub_utils.sh      finds the Windows menuentry (single- or double-quoted)
linux/efi_utils.sh       finds the Windows and GRUB entries in `efibootmgr`
linux/ssh_utils.sh       installs the Pi's forced-command authorized_keys line
linux/scarlett-reset.py  USB-replug for the Focusrite after a warm dual-boot
linux/seamless-displays.sh  pins EDID + forces connectors on, so a SCREENS input
                         switch does not tear the desktop down (--revert undoes it)
linux/media-agent.py     :9110 — now-playing (MPRIS), monitor input switching
                         (DDC) and the output spectrum (parec + numpy), the
                         Linux twin of the sim-agent's /media, /monitor, /audio
                         plus /speak: Douglas's voice on this machine's speakers
douglas/douglas-agent.py :9111 on Douglas — metrics, voice state, transcript and
                         push-to-talk relay for the DOUGLAS surface (douglas/setup.sh)
douglas/provision.sh     fresh Ubuntu 24.04 → Douglas: tailnet, GTT limit, llama.cpp
                         (Vulkan) :8081, Open WebUI :8080, Wyoming STT/TTS, voice
douglas/douglas-voice.py :10800 — DougVoice's pipeline: satellite → STT → Douglas → TTS
dougvoice/setup.sh       Pi Zero 2 W + Whisplay HAT → DougVoice, Douglas's satellite
dougvoice/dougvoice.py   :10700 Wyoming satellite launcher, and the LCD/LED/button face
tests/                   shell tests for the above — `bash tests/test_*.sh`

Flight Deck — the touchscreen front-end (v0.1):
pi/display-check.sh      read-only: is the XENEON Edge's video + touch sound?
pi/setup-display.sh      cage + chromium kiosk on the Edge, mode pinning
pi/setup-deck.sh         installs deck-api, repoints fauxmo at it
pi/pin-fleet.sh          pins Douglas and DougVoice in the Pi's hosts file, so the
                         deck finds them without DNS
pi/deck-api/deck_api.py  one entry point for every trigger; state + SSE
pi/deck-api/test_phases.py   phase mapping vs the orchestrator's real log lines
pi/deck-ui/              the panel itself — index.html, deck.css, deck.js, selftest.html
pi/deck-ui/js/           the panel as ES modules — deck.js is now just wiring:
                           format, geo, series   pure logic, unit-tested in node
                           ui                    $, rows, hold-to-arm, POST
                           track, spark, gauges  drawing, given the state to draw
                           nav, sim, screens, media   a surface each, owning its
                                                 own state and its own listeners
tools/screenshots.mjs    renders every surface at 2560x720 for the images above
                         (node + any installed Chrome + internet for map tiles;
                          `npm install playwright-core` first, `npm.cmd` on
                          Windows if PowerShell blocks the npm wrapper)

The Windows sim agent (docs/SIM-AGENT-BRIEF.md):
windows/sim-agent/       flightdeck-sim-agent — SimConnect, media, DDC, GPU
                         telemetry and the output spectrum, all on :9109
windows/setup-sim-agent.ps1   build, token, firewall, logon task
windows/scarlett-power.ps1    keep the Scarlett on the bus (power mgmt + ghosts)
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
   — GRUB_DEFAULT=saved, a UEFI boot order with Windows first (see
   [Which OS a cold boot lands in](#which-os-a-cold-boot-lands-in)), the
   `boot-to-windows` helper, the forced-command
   authorized_keys entry, persistent `ethtool wol g`, the `:9108` boot
   agent, and the Linux logon greeting + VS Code autostart. It prints a
   `LINUX_AGENT_TOKEN=` line; add that to `boot.env` on the Pi too. The
   public key is optional — omit it to install everything except the ssh
   path, and the Pi will use the token-guarded agent instead.
4. **BIOS**: enable "Resume by PCI-E/PME" (Wake on LAN) and **disable**
   ErP/EuP deep-off, or the NIC loses standby power at S5 and cold-boot
   wake silently fails.
5. **Pin the workstation's IP** — DHCP reservation at the router, or a
   static address in both OSes. A machine that is powered off cannot renew
   or defend its DHCP lease, so the first genuinely cold night is when some
   other gadget takes its address — and every probe, token callback and
   agent call in this system dials a number a stranger now answers. Found
   the hard way: an Echo took `.50` overnight and the panel called a
   running workstation "off" while WOL woke it perfectly. Two subnet traps
   while you are in there: mesh routers commonly hand out a **/22**, so a
   `.255` broadcast address copied from a /24 example is just a
   nonexistent host (the WOL sender now also unicasts for this reason),
   and a hand-typed static mask of `255.255.255.0` on a /22 LAN recreates
   the same bug one layer down.
6. **Auto sign-in** (needed for a truly hands-free cold boot — the
   greeting and launches are logon-triggered): `netplwiz` → uncheck
   "Users must enter a user name and password". On Windows 11 that
   checkbox is hidden until you set
   `HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device`
   → `DevicePasswordLessBuildVersion` = 0. Sysinternals Autologon also
   works and ignores the whole dance.
7. **Alexa app**: "Alexa, discover devices" finds the virtual plugs
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
`jarvis-greeting.ps1` gains a callback — nothing on the Windows side is
touched, so a run completes at **OS UP**.

### The surfaces

Five surfaces behind a persistent 72 px strip, all of them live at once —
nothing is unmounted when you look away, because reloading the Grafana frame
would restart the playlist every time you glance at DECK.

These are rendered from the real `deck-ui` at true panel pixels by
`tools/screenshots.mjs`, against a scripted state frame. They are what the
panel looks like, not a mock-up, and they can be regenerated after any change.

**DECK** — the boot you just asked for, while it happens. Phase track,
rolling GPU telemetry, the fleet, and an ABORT that only appears with a boot
in flight.

![DECK](docs/img/deck.png)

**NAV** — a moving map from the sim agent's observed position. Waypoint
search hits real OpenStreetMap data, plans are saved on the Pi, and the trail
is where the aircraft has actually been. Pinch to zoom; FIT hands framing back
to the auto-fit.

![NAV](docs/img/nav.png)

**SIM** — observed aircraft state and the controls that command it. The panel
never draws the commanded state: a tap shows PENDING over the last *observed*
value, and only the simulator moving it changes what you see. That is what
makes a command MSFS ignored look like a failed command instead of a lie.

Each autopilot bug carries the mode that reads it. A bug on its own is only a
target — set AP SPD to 165 with no mode engaged and the throttle never moves —
so the tile says *FLYING THIS BUG* or *BUG ONLY · MODE OFF* rather than leaving
a number sitting there looking authoritative.

![SIM](docs/img/sim.png)

**SCREENS** — one card per monitor, named by its position on the desk, with a
button per input the panel declares. DDC/CI over whichever OS is booted:
Windows answers through the sim agent, Linux through `media-agent.py`.

Panels that won't declare their inputs — the HP 32f refuses the capabilities
request outright — fall back to `MON_DEFAULT_INPUTS`, an operator-stated fact
about this desk rather than a guess from a model table. Whatever that list
says, the input a monitor is *observed on* is always offered too: a default
written for two identical HPs mentions no DisplayPort, and without that rule
the panel could move a third monitor to HDMI with no button anywhere to bring
it back.

![SCREENS](docs/img/screens.png)

**AUDIO** — what is playing, and what it actually sounds like. The spectrum is
measured on the workstation by the sim agent's WASAPI loopback capture, because
the panel runs on the Pi and has no audio of its own to look at. Reachable from
the strip or by tapping the now-playing widget on DECK — the transport buttons
inside it still skip rather than navigate.

The bands are real. There is no fallback pattern and no idle animation: silence
draws flat bars, and no capture session says so in words. A visualiser driven by
track position would be the same class of lie as drawing a commanded gear
position, which this panel refuses to do anywhere else.

![AUDIO](docs/img/audio.png)

**DOUGLAS** — the AI, on its own box. Push-to-talk on the left, the
conversation in the middle, and on the right what Douglas is spending: GPU,
unified model memory, tok/s, CPU, RAM, temperature and power. The panel has no
microphone, so holding PUSH TO TALK asks Douglas to have DougVoice listen —
the same as DougVoice's own button — and the button's colour is the voice
pipeline's observed state, not the press. Everything comes from
`douglas-agent` via `/api/douglas`, polled only while the surface shows; with
the agent unreachable every meter reads "—" rather than an idle-looking 0.
Reachable from the strip or by tapping the DOUGLAS line under DECK's boot
tiles.

![DOUGLAS](docs/img/douglas.png)

**On a phone** — `http://node1:8088/m`, over the tailnet. The same deck in one
column: the workstation with its hold-to-arm boot tiles, Douglas (status,
meters, the last few turns, a box to type to him, push-to-talk, and a link
to its own chat page), now playing, the fleet and the event log. When Douglas
has a vision model the ask box grows a camera button: take or pick a photo,
add a question or not, and he answers about what is in it. The photo is
scaled to 1280 px on the phone before it is sent. Ask him to change it
instead (snow on the roof, the whole thing as a watercolour) and the edited
picture arrives a minute or two later as a line of its own. A picture he
draws or edits shows under his line, here and on the panel; a tap fills the
screen with it. Same SSE frame, same endpoints, same rules as the panel.

A phone's link to node1 is not a wire: on the tailnet it can drop to a relay,
or go quiet for a minute or two at a time. So every request from the page has
a time limit, and SEND is never greyed out: a question that cannot go yet
waits, and goes by itself on the first poll that shows node1 is back and
Douglas is free. The line under the ask box says what it is waiting for and
counts the time, and the button reads STOP while a question is on its way.
Each question carries an id, so a copy that arrived with only its reply lost
is not asked twice. The page has to stay open for a waiting question to go.
On node1, `journalctl -u deck-api` has one line for each question (its size
and outcome, never its words) and one for a request that did not arrive
whole; `journalctl -u tailscaled` shows whether the phone was on a direct
path or a relay at the time, and `tailscale debug peer-endpoint-changes
<device>` has the detail.

A phone is not loopback, so it needs `DECK_TOKEN`: run
`sudo ./pi/mobile-link.sh` on node1 and open the link it prints (a QR code too
when `qrencode` is installed). The page keeps the token in that browser and
strips it from the address bar; "Add to Home Screen" makes it a full-screen app.
On an iPhone that app is a browser of its own and shares nothing with Safari,
so it asks for the token once: in Safari, tap COPY THIS PHONE'S TOKEN at the
bottom of the page, then paste it into the app's token screen (which also
takes the whole link).

HOLD TO TALK on the plain page borrows DougVoice's ears, exactly as the
panel's button does: a plain-HTTP page is not allowed the phone's own
microphone. The button says so, and when a press picks up nothing the line
under the ask box says that too ("DougVoice heard nothing in 3.5 s"), from the
length, level and word count the pipeline keeps of each listen (never the
words).

**Talking through the phone.** A browser lends a page its microphone only on
HTTPS. `sudo ./pi/mobile-https.sh`, run on node1, puts the phone page on
`https://<node1>.<tailnet>.ts.net/m` with `tailscale serve`: tailnet only, as
before. It needs HTTPS enabled for the tailnet, and issuing the certificate
records the machine's tailnet name in the public certificate log. Requests
that arrive that way reach deck-api from loopback, the address the panel is
trusted on, so deck-api asks anything a proxy forwarded for the token, and the
script takes the HTTPS front down again if a request without one gets in.
The plain page then offers TALK THROUGH THIS PHONE, a link that takes the
phone's token along. On the HTTPS page HOLD TO TALK records through the
phone's own microphone (16 kHz, up to 20 s), sends the recording through the
same waiting queue as a typed question, and plays Douglas's reply through the
phone; nothing is played in the house. An iPhone that has just lent a page
its microphone treats the page's sound as a telephone call, out of the
earpiece at call volume, so the recording has an audio context of its own
that is closed when the button is let go, and the reply is played by an
ordinary media element with the page's audio session set to playback. `sudo ./pi/mobile-https.sh off` undoes
it.

**IDLE** — the resting state, and the default surface: a clock, the boot
tiles, and the fleet's status cards. The panel spends most of its life with nothing
happening, and its previous default was the densest surface it has. Nothing
here updates faster than once a second, nothing glows, and the only colour is
the green of a machine that is actually up. The strip has no IDLE button on
purpose — you do not navigate to rest, the panel returns there by itself.
`DEFAULT_SURFACE=evals` in `boot.env` restores the old behaviour.

A SHUT DOWN control lives on DECK beside ABORT — same hold-to-arm, amber
rather than red, powering the workstation off through whichever OS is up
(`/shutdown` on either boot agent). It hides during a boot, ABORT hides
outside one, so the two never appear together and each keeps the full width.

![IDLE](docs/img/idle.png)

**jobContext** — the pre-existing Grafana wallboard, framed as-is. Flight Deck
provisions no dashboards, stores no eval data, and contributes only the strip,
the sub-nav built from Grafana's own dashboard list, and AUTO pinning.

*Not shown: the frame's contents are third-party software that is not part of
this repo, and a screenshot of a stand-in would document nothing real.*

### Live telemetry, and only live

GPU temperature, utilisation and VRAM come from the exporters that already
answer on `:9105` / `:9106` — this project installs none of its own. deck-api
parses what they emit and keeps **only the latest value**; the rolling
60-minute window behind the sparklines lives in the browser's memory and dies
with the page.

There is deliberately no Prometheus, Grafana, Loki, or long-term retention.
This targets the existing Pi 4B with 4 GB and a USB thumb drive, and a TSDB
on flash with no real wear levelling corrupts before it dies. deck-api state
lives in tmpfs and `/tmp` is moved there too, so nothing writes in a loop.

When neither OS answers, the trace breaks rather than reading zero — on a
dual-boot machine that gap *is* the reboot.

Both `pi/deck-api/test_phases.py` and `test_telemetry.py` run anywhere, with
no Pi and no exporter needed. See [docs/DESIGN.md](docs/DESIGN.md) for the
full picture.

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
| `LINUX_AGENT_TOKEN` | must match `/etc/flightsim/boot-agent.token` on the Linux boot. Unset means every reboot-to-Windows request goes over the ssh key instead |
| `LINUX_AGENT_PORT` | the Linux boot agent's port (default 9108) |
| `LINUX_MEDIA_TOKEN` | must match `/etc/dualboot/media-agent.token` on the Linux boot. Unset means MEDIA and SCREENS stay dark under Linux — deck-api skips the agent entirely rather than guessing |
| `LINUX_MEDIA_PORT` | the Linux media/monitor agent's port (default 9110) |
| `SIM_AGENT_TOKEN` | must match `C:\ProgramData\dualboot\sim-agent.token`. The same token guards SIM, MEDIA and SCREENS under Windows — one agent serves all three |
| `SIM_AGENT_PORT` | the Windows sim agent's port (default 9109) |
| `POLL_SECS` | how long to keep watching a boot (default 300) |
| `DOUGLAS_HOST` | Douglas's address (default `douglas`, its MagicDNS name) |
| `DOUGLAS_AGENT_TOKEN` | must match `/etc/douglas/agent.env` on Douglas. Unset means the DOUGLAS surface says so instead of drawing meters |
| `DOUGLAS_LLM_PORT` / `DOUGLAS_CHAT_PORT` / `DOUGLAS_STT_PORT` / `DOUGLAS_TTS_PORT` / `DOUGLAS_AGENT_PORT` | Douglas's service ports (defaults 8081 / 8080 / 10300 / 10200 / 9111) |
| `VOICE_HOST` / `VOICE_SAT_PORT` | DougVoice's address (default `dougvoice`) and Wyoming satellite port (default 10700) |

Hardcoded elsewhere and worth knowing about: the Pi's address in
`jarvis-greeting.ps1` (for the intent read-back), the device names and
ports in `pi/setup.sh`, and the launch commands in `$LaunchProfiles`.

### Adding a game

Three edits: a new device block in `pi/setup.sh` with
`FLIGHT_INTENT=<key>`, a matching entry in `$LaunchProfiles` in
`windows/jarvis-greeting.ps1` (command plus the Jarvis closing line), then
redeploy both sides, re-run Alexa discovery, and add a routine.

### Choosing which screen a game opens on

A launch profile can also say what the desk should look like before the game
starts. Both fields are optional, and a profile with neither leaves everything
exactly as it found it:

```powershell
sim = @{
    cmd     = { explorer.exe shell:AppsFolder\Microsoft.Limitless_8wekyb3d8bbwe!App }
    display = '3840x1080'      # make this monitor primary first
    input   = 'dp1'            # and put every monitor on this DDC input
    closing = 'The flight deck is ready when you are.'
}
```

`display` exists because both of these games take the **primary display and
nothing else** — Squadrons is Frostbite and has no monitor picker at all — so
"launch on the ultrawide" really means "make the ultrawide primary first".
Windows has no cmdlet for that; `set-primary-display.ps1` does it through
`ChangeDisplaySettingsEx`, moving the new primary to the origin and carrying
every other monitor with it so the desktop does not end up with a hole in it.
Run it with `-List` to find a string that identifies the panel you mean:

```powershell
C:\ProgramData\dualboot\set-primary-display.ps1 -List
```

Matching is deliberately strict about ambiguity — two identical 1920×1080
panels both match `1920x1080`, and it will refuse rather than pick one, because
making the wrong monitor primary rearranges the whole desktop.

`input` switches DDC through the sim agent already running on the box, the same
endpoint the SCREENS surface drives, so there is exactly one implementation of
DDC on this machine. Leave it unset unless you know which input your PC is on:
sending a monitor to an input nothing is plugged into blanks it until you find
the OSD.

Neither can stop a launch. A monitor that will not move is a worse evening than
a game on the wrong screen, and both failures are warnings that the greeting
carries on past.

## Which OS a cold boot lands in

A powered-off machine has nobody left to ask. Whatever the last running OS
wrote down is what a Wake-on-LAN packet boots into, so the only real
question is where that setting lives.

It cannot live in GRUB, because only one of the two boots can write it.
Linux runs `grub-reboot`; Windows has no ext4 writer, so it can neither do
that nor edit `grubenv`. That asymmetry used to show up as a two-boot cold
start: GRUB's saved default is Linux, so "boot Windows" from cold **booted
Linux**, waited for it to answer, and only then rebooted into Windows —
a GRUB countdown showing the wrong OS, several minutes, and a real chance
of exhausting `POLL_SECS`.

The UEFI boot variables are the one switch both sides can throw, so the
cold default lives there instead, pointed at Windows:

| From | To | How |
| --- | --- | --- |
| off | Windows | firmware `BootOrder[0]` = Windows Boot Manager |
| Linux | Windows | `efibootmgr -n <windows>` (`boot-to-windows`) |
| Windows | Linux | `bcdedit /set {fwbootmgr} bootsequence <ubuntu>` → GRUB |
| off | Linux | WOL into Windows, then the row above |

`linux/setup.sh` applies this via `linux/set-boot-order.sh`; run that alone
(`sudo ./linux/set-boot-order.sh`, or `--print` to look first) after
reinstalling either bootloader, because the entry numbers change.
GRUB's own configuration is untouched — its saved default stays Linux,
which is exactly what the Windows → Linux leg needs once the firmware has
handed over.

Two things follow from this. Powering the machine on **by hand** now goes
straight to Windows without showing the GRUB menu; use the firmware's
one-time boot menu (F8/F11/F12, board-dependent) to reach Linux that way.
And a cold boot to *Linux* is now the two-boot direction — deliberately,
since Linux is the rare target here.

Neither step is required. On a legacy-BIOS install, or a firmware whose
boot list has no Windows entry, `set-boot-order.sh` refuses and says why,
and both agents fall back to the old GRUB-default behaviour.

### Local inference lives on Douglas

The workstation no longer runs a model. Douglas — a headless GEEKOM A9 Mega
(Ryzen AI Max+ 395, 128 GB unified memory) on the tailnet — is the home of
the assistants, so the workstation's GPU belongs to the games and the old
"a game's boot keeps its VRAM" juggling is gone. Re-running `setup.ps1`
removes the `LlamaServer` logon task and its firewall rule. On the Linux
boot, disable the old unit by hand (it was never installed by this repo):

    sudo systemctl disable --now 'llama-server@*'

Douglas serves llama.cpp on `:8081` (start it with `--metrics` for the
tok/s meters), Open WebUI on `:8080`, and Wyoming speech-to-text/text-to-speech
on `:10300`/`:10200` for DougVoice, with his own voice (`douglas-tts`) on
`:10210`. `douglas/setup.sh`, run on Douglas,
installs `douglas-agent` (`:9111`) — the panel's one window into it — and
mints the token that goes into the Pi's `boot.env` as `DOUGLAS_AGENT_TOKEN`.
The Pi reaches Douglas and DougVoice by MagicDNS name (`douglas`,
`dougvoice`), so the Pi must be on the tailnet with `--accept-dns`, or
`DOUGLAS_HOST` / `VOICE_HOST` must be set to full names.

Those names resolve only while the Pi's `/etc/resolv.conf` is the one
Tailscale wrote, and on a Pi that is on Wi-Fi it is not always (see
Troubleshooting). `sudo ./pi/pin-fleet.sh`, run on the Pi, pins both names in
its hosts file at the addresses Tailscale gives, so the deck goes on finding
them whatever has happened to DNS. Nothing needs restarting, and `off` undoes
it. A machine that is removed from the tailnet and added again gets a new
address: run the script again and the pin follows it.

#### Provisioning Douglas

`douglas/provision.sh` takes a fresh Ubuntu Server 24.04 to all of the above
and runs `douglas/setup.sh` at the end. In order:

1. **BIOS:** set the iGPU's UMA frame buffer to its minimum (512 MB). The
   model lives in GTT, which is ordinary RAM the GPU maps; RAM carved out as
   VRAM is RAM GTT can never have.
2. **`sudo TS_AUTHKEY=tskey-… ./douglas/provision.sh`** — hostname `douglas`,
   the HWE kernel and Mesa (Strix Halo's gfx1151 needs both newer than 24.04
   GA), Tailscale, the GTT/TTM limit on the kernel command line, llama.cpp
   built for Vulkan/RADV, Open WebUI, faster-whisper, Piper, the Kokoro
   voice (its 337 MB model is fetched and checked against a pinned hash),
   `douglas-voice` and `douglas-agent`. Without `TS_AUTHKEY` it prints a login URL and waits.
3. **`sudo reboot`** — the GTT limit and the new kernel only exist after one.
4. **Put a model in `/srv/models`** and point `LLAMA_MODEL` at it in
   `/etc/douglas/llama.env` (nothing multi-GB is downloaded for you).
   What lets him be shown a picture is `EYES_URL` in `voice.env`: his own
   model's address if it can see (give `llama-server` its `--mmproj` in
   `LLAMA_ARGS`), or else a small vision model and its projector in
   `/srv/models/eyes` (see `/etc/douglas/eyes.env`). What lets him draw one
   is an image model in `/srv/models/draw` (`/etc/douglas/draw.env` says
   which files, and where each goes) and `IMAGE_URL` in `voice.env`; a
   second model beside it and `IMAGE_EDIT` let him change one he is given.
5. **Run `provision.sh` again.** It is idempotent: config files are written
   only when missing, so this run just rebuilds what moved and ends with a
   port-by-port up/down list that means something.
6. Copy `DOUGLAS_AGENT_TOKEN` from `/etc/douglas/agent.env` into node1's
   `boot.env`, and give the same token to `dougvoice/setup.sh`: DougVoice's
   button posts to `douglas-agent`'s `/ptt` exactly as the deck does, so
   `VOICE_PTT_TOKEN` never leaves Douglas.

Vulkan rather than ROCm: on gfx1151 RADV matches or beats ROCm at token
generation, comes from Ubuntu's own archive, and has no toolkit to keep in
step with the kernel. Rebuild with `-DGGML_HIP=ON` if prompt processing on
very long contexts ever matters more.

Script knobs (environment): `TS_AUTHKEY`, `GTT_GB` (default 110 of the
128 GB), `LLAMA_REF` (default `master`), `REBUILD_LLAMA=1`, `MESA_PPA=1` (adds
kisak-mesa if RADV still cannot see the GPU after the reboot),
`OPEN_WEBUI_HTTPS=0` (skip `tailscale serve`; phones need HTTPS for the
microphone, which needs HTTPS certificates enabled for the tailnet),
`SKIP_APT=1`.

Everything else lives in `/etc/douglas/*.env`, read by the units; edit and
`systemctl restart` the unit named in each file's header:

| File | Knobs |
|---|---|
| `llama.env` | `LLAMA_MODEL` (the GGUF), `LLAMA_ARGS` (default `-ngl 999 --load-mode none --jinja -c 32768 --parallel 2 --alias douglas`), `LLAMA_HOST`/`LLAMA_PORT`. `--metrics` is always on — the deck's tok/s meters need it |
| `eyes.env` | `llama-eyes`, the vision model on `:8082` (loopback): `EYES_MODEL_FILE`, `EYES_MMPROJ` (its projector), `EYES_ARGS` (default `-ngl 999 --load-mode none --jinja -c 8192 --parallel 1 --image-min-tokens 1024 --image-max-tokens 2048 --alias eyes`), `EYES_HOST`/`EYES_PORT`. The unit is enabled by `provision.sh` only once the model file exists and `voice.env`'s `EYES_URL` points at it: a model that sees for itself needs no second one |
| `draw.env` | `douglas-draw`, the image server on `:8083` (loopback): `DRAW_MODEL_DIR` (default `/srv/models/draw`, holding `diffusion_models/`, `text_encoders/` and `vae/`: the files themselves, since the server refuses links that lead out of the directory), `DRAW_ARGS` (empty by default), `DRAW_HOST`/`DRAW_PORT`. It starts with no model in memory: the pipeline tells it which to hold before each picture. `provision.sh` builds stable-diffusion.cpp and enables the unit only once a model is in `diffusion_models/` |
| `chatgpt.env` | `douglas-chatgpt`, ChatGPT's chat-only line to Douglas: `CHATGPT_SECRET` (required, at least 24 URL-safe characters: it is the path of the connector's address, and changing it revokes the connector), `CHATGPT_PORT` (default `9120`, loopback), `CHATGPT_PER_HOUR` (default `60`), `LLM_URL`. Not created by `provision.sh`: the connector is set up by hand, see `douglas-chatgpt.py` |
| `open-webui.env` | `OPENAI_API_BASE_URL` (llama.cpp's `/v1`), `WEBUI_NAME`, `WEBUI_SECRET_KEY`. The first account made in the browser is the admin |
| `tts.env` | `douglas-tts`, his speaking voice on `:10210` (loopback): `KOKORO_MODEL` and `KOKORO_VOICES` (the two model files, in `/srv/models/kokoro`), `KOKORO_VOICE` (default `bm_lewis`), `KOKORO_SPEED` (default `1.0`), `TTS_BIND`/`TTS_LISTEN_PORT`. The unit is enabled by `provision.sh` only once the model files exist |
| `speech.env` | `WHISPER_MODEL` (default `small`, on the CPU), `WHISPER_LANGUAGE`, `WHISPER_COMPUTE`, `WHISPER_ARGS`; `PIPER_VOICE` (default `en_GB-alan-medium`, fetched on first use), `PIPER_ARGS` |
| `voice.env` | `VOICE_SAT_HOST`/`VOICE_SAT_PORT` (DougVoice's satellite, `dougvoice:10700`), `VOICE_PTT_PORT`/`VOICE_PTT_TOKEN`, `STT_*`/`TTS_*`/`LLM_URL`, `TTS_VOICE` (unset by default: the voice is chosen in `tts.env`, and a name here overrides it for this pipeline only), `TTS_FALLBACK_PORT` (a second voice, Piper on `10200`, used when the first does not answer; `0` for none), `LLM_MAX_TOKENS`, `DOUGLAS_SYSTEM_PROMPT` (who he is and what he must not invent), `DOUGLAS_FACTS` (one line of true things about the household - the only local knowledge he has), `DOUGLAS_INSTRUCTIONS_DIR` (default `/etc/douglas/instructions.d`: every `.md`/`.txt` there is read in name order on every turn, so who the owner is, their rules and notes live in files on Douglas, not in this repo), `DOUGLAS_OWNER`, `SPEAKER_URL` (a machine with real speakers, e.g. the workstation's media agent at `http://<host>:9110/speak?token=...`: each reply plays there when that machine is up and unmuted, and through DougVoice otherwise; a new press or a cancel cuts it short there too), `MCP_URL`/`MCP_TOKEN` (the owner's MCP server: the pipeline reads the `stories` tone profile from it at start and every `TONE_REFRESH_S`, caches a few samples in `/var/lib/douglas/tone.md` and gives them to the model as the owner's writing voice; it makes no other call), `JOBCONTEXT_URL`/`JOBCONTEXT_TOKEN` (the owner's records on their jobContext server, read when asked and changed only after a read-back and a yes; `https` only), `JOBCONTEXT_AREAS` (the parts of it he may reach, e.g. `stories` for stories and writing samples alone; unset, all of it), `JOBCONTEXT_VIA` (where they may be asked about from, e.g. `phone`; unset, anywhere), `JOBCONTEXT_CONFIRM_S` (how long a change waits for its yes, default `180`), `SEARCH_KEY` (a Brave Search API key: with it he can search the web; `SEARCH_URL` if the service is somewhere else), `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET`/`REDDIT_USER` (an app registered with Reddit and the Reddit name it is registered to: with them he can search Reddit; `REDDIT_USER_AGENT` to say who he is in other words), `DOUGLAS_TURN_LOOKUP_S` (how long one turn may go on looking things up before he must answer, default `20`), `TZ` (so the time he is given is local), `VOICE_MAX_LISTEN_S` (a held button stops itself after this), `VOICE_QUIET_DB` (default `-45`: Whisper turns a quiet room into "you" or "thank you", so a transcript that is only such a stock word, from audio quieter than this, counts as nothing heard), `VOICE_HISTORY_TURNS`/`VOICE_HISTORY_IDLE_S` (how much of the conversation Douglas remembers, and for how long), `DOUGLAS_TOOLS` (`0` turns his lookups off), `DOUGLAS_HOME` (the place "the weather" means when none is named, e.g. `Springfield, Illinois`), `DOUGLAS_UNITS` (`us` for °F and mph), `DOUGLAS_NEWS_FEEDS` (RSS/Atom feeds for the top stories), `DOUGLAS_NEWS_SEARCH` (a news search feed, `{q}` replaced by the topic), `EYES_URL` (what he sees with: his own model at `http://127.0.0.1:8081` if it can see, else `llama-eyes` at `:8082`; unset, he cannot be shown a picture and says so), `IMAGE_URL` (the image server, `http://127.0.0.1:8083`; unset, he cannot draw and says so), `IMAGE_DRAW` and `IMAGE_EDIT` (the model that draws and the model that edits, each a name and a file under `DRAW_MODEL_DIR`: `flux1-schnell diffusion_models/flux1-schnell-q8_0.gguf` for drawing by default and nothing for editing, so without `IMAGE_EDIT` he draws and does not edit), `IMAGE_PARTS` (the text encoders and VAE the two share), `IMAGE_EDIT_STEPS` (default `8`: an edit's time is its steps, about 13 seconds each at a megapixel), `IMAGE_EDIT_SIDE` (default `1024`, the long side an edit comes out at), `PICTURES_DIR`/`PICTURES_KEEP` (where the pictures he makes are kept, and how many: `/var/lib/douglas/pictures`, the newest 60), `EYES_MAX_TOKENS`, `VOICE_MAX_IMAGE_BYTES`, `DOUGLAS_USER_AGENT` (how lookups introduce themselves; generic by default, and some sites, Wikimedia's image servers among them, turn an anonymous client away until it carries a contact) |
| `agent.env` | `douglas-agent`'s; `provision.sh` fills in `VOICE_PTT_URL` with the pipeline's token |

**The voice pipeline.** `douglas-voice` connects *to* DougVoice's
`wyoming-satellite` (run in always-streaming mode: no wake word, no VAD) and
sends `run-satellite`; the satellite then streams its microphone for good.
That audio is dropped unless push-to-talk is held, so nothing reaches the
model without a press — DougVoice's own button or the deck's, both of which
`POST /ptt` (`{"action":"start|stop|cancel","via":"voice|deck"}`; DougVoice
needs `?token=`). On stop the kept audio goes to faster-whisper, the text to
llama.cpp as Douglas, the reply to Piper, and the speech back to the
satellite's speaker, resampled to the format its `run-pipeline` asked for.
`/run/douglas/voice.json` follows idle → listening → thinking → speaking →
idle and is rewritten at least every 5 s; each side of the exchange is one
line in `/var/lib/douglas/transcript.jsonl`. Pressing again while Douglas is
speaking interrupts the reply; cancel drops the turn wherever it is.

**What he can look up.** A model on a box with an Ethernet cable still has no
internet: it can only use what the program around it hands over. So
`douglas-voice` gives it four read-only tools and runs them when the model
asks: `weather` (Open-Meteo: current conditions and seven named days, for
`DOUGLAS_HOME` when no place is given), `news` (the top stories from
`DOUGLAS_NEWS_FEEDS`, or a news search for a topic, each headline dated),
`wikipedia` (the best article's introduction, its address and the other
matches) and `read_page` (the text of a public web page, optionally from a
phrase onward). He is told to look up anything about the present every time
rather than recall it, to add nothing the lookup did not return, and to say so
when a lookup fails. Two more need a key and are offered only with one: a
search of the web and a search of Reddit (below). Without them there is no
general web search, only the headlines of a news search standing in for one:
search engines answer an automated client with a bot challenge, which this
does not try to get past. `read_page` fetches public addresses
only (loopback, LAN and tailnet addresses are refused, redirects included), and
whatever a lookup returns is handed to the model marked as information, not
instructions. Lookups are the one thing that leaves the house: the words of
the question go to the service asked. `DOUGLAS_TOOLS=0` turns them off, and he
then says he has no internet.

**The weather and the news are looked up before he speaks.** Told to look
things up, he mostly does. On the first message of a conversation he often
did not: asked whether it would rain today, he gave a forecast with numbers
in it that he had made up, or said he had no way to check, as often as four
times in ten. No wording cured it, a lower temperature did not, and the model
server cannot be made to call a tool (it accepts `tool_choice` and ignores
it). So the program looks. A message that uses a weather or news word is put
to the model as a question of its own, with a word or two for an answer: is
this asking for the weather, and where? for the news, and about what? That
it answers reliably, in about half a second. On anything but a no, the
program calls the tool and hands him the result as a call he made, and he
answers from it. It is also the quicker way round, one pass of the model
where his own lookup takes two: in a dry run every one of 30 weather
questions and 12 about the news was answered from a lookup, in about two
seconds where it had been four to seven. A message that only uses the word
("do you like rainy days?") gets a no and goes to him as it is, and one that
points at a place named earlier ("is it raining there?") is left to him,
since he knows where "there" is.

**A search of the web, and of Reddit.** Asked what the reviews say of a
thing, he had nothing to look with: he said he had "not seen any reviews", or
named a model from memory. Both searches go through the service's own API,
each with a key the owner gets from that service, and neither is offered
without one. `search` (`SEARCH_KEY`, a Brave Search API key; the service
charges by the search, about half a cent each at the time of writing) asks for
passages from the best few pages, the form of answer that service offers to
assistants, and falls back to its ordinary result list on a plan that has
none; he gets each page's title, site, date and address and what it says, in
plain words, and is told to say which site a claim comes from. `reddit` (`REDDIT_CLIENT_ID`,
`REDDIT_CLIENT_SECRET`, `REDDIT_USER`) reads Reddit as an app with no user
behind it: the best threads for a query from the last year, with the top
comments of the first two, or more of one thread's comments. Nobody's name
comes back with a comment, nothing marked as adult, nothing deleted, and
what he gets is labelled as the opinion of anonymous users. Reddit approves
each use of its API before it gives access (its Responsible Builder Policy,
November 2025), and wants the app and its owner's Reddit name in every
request, which is what `REDDIT_USER` is for.

As with the weather, the program does the looking. In a dry run he reached
for the web search on his own in 13 of 24 questions about reviews, prices
and what to buy; with the question put to the model first ("does this need a
web search, and for what words?") and the program searching before he
answers, 24 of 24, and 12 of 12 for Reddit, with plain questions left alone.
With one search switched on and not the other, the one that is there stands
in: a question about Reddit becomes a web search with the word reddit in it,
and a question about reviews goes to Reddit. A message in the words of the
owner's own records ("which of my stories is best?") is never taken to a
search: the records come first.

With a real web search switched on, a question need not use a word like
"review" or "price" to want it. Left to himself he answered seven of eight
such questions from what he knew two years ago ("Apple hasn't announced an
iPhone 17 yet"), and once added "I looked it up" in a turn that had looked
nothing up. So whatever asks (a question mark, or a sentence that opens like
a question) is put to a wider question: does answering this take a web
search, for an assistant whose knowledge ends two years ago? A named product
or service, who holds an office, anything latest or current: yes. Small talk,
the time, the person's own things, knowledge that does not change: no. The
answer is the words to search for, taken from the message with no year or
name added from memory; asked without that it searched for "iPhone 16" and
for "Emmanuel Macron". Tried on 143 messages, twice each, it was right on all
of them. It is put to the model as instructions, with the message as the
message: the model server keeps what it has read only up to where a message
starts, so the question in one piece was read again for every message (1.4
seconds for a no) and this way is read once and kept (half a second).

A question that points back ("And how much is the regular one?") names
nothing and got a no; he then said he had not checked, or gave a two-year-old
price as "according to the same lookup". Such a message is first said again
so that it stands alone, with what the person said just before to go by ("And
how much is the regular PlayStation 5?"), and that is what is asked about: 27
of 30 pairs came out right, "What time is it?" after a question about a
console among them. Only the person's own words go into this, never his
replies or what a lookup returned: a rewriting with a word in it that the
person did not say is thrown away, and in a conversation that has read the
owner's records, or after a question about the weather or the news, nothing
said earlier is used at all. It is a step of its own, a second on a
follow-up, because the one question would not take the job on: three
wordings that gave it the earlier message each broke something else.

After a search the program made, that search is all the looking up the turn
has, and he is told so with the results: answer from them in two or three
sentences, the few things that bear on the question with the site that says
each, nothing that is not there, and if they do not answer it, say what is
missing, so that the person can have him search again. Left his allowance, in
one such turn in six he read pages, searched again or looked into the owner's
records, and with this model the server reads the whole turn again for every
further round of lookups, twenty seconds and more, and once again in the turn
after: one spoken answer took 54 seconds. He is still shown his tools in that
turn (left out, the server would read his instructions over again), and a
call he makes all the same is not run. And his standing instructions now say
only what the search is for: "search first, then answer from what the pages
say" is what he had recited as done.
In a dry run of whole turns on the real model and the real search, the program
searched for all 24 questions that wanted it (reviews, prices, what to buy,
"who runs Intel these days", "what is new in the latest iPhone", three that
pointed back, two that named Reddit), one lookup each, and they were answered
in 8 to 16 seconds, most of that the writing; 12 that wanted none were left
alone, with the question costing them about half a second, a question in the
records' words went to the records, and in none of 39 turns did he say he had
looked when he had not.

With neither search, the news stands in. The news search needs no key, and
searched for a product it finds what news and review sites have run about it:
reviews, round-ups and deals, each headline with its outlet and its date. So a
question about reviews, a price or what to buy is taken there with the words
it comes to, and he is told in that turn what he has been handed: headlines,
not the reviews under them, to be passed on in a few sentences with the outlet
named for each, nothing added, and a plain word that headlines are all he can
see without a search key. Often the headline carries the verdict, the score or
the price; a round-up's headline does not name its winner, and he says that
too. In a dry run on the real model and the real news search, 23 such
questions (reviews, prices, what to buy, comparisons, "search the web for"),
each asked twice, were all taken to the news, and read against the headlines
they were handed the answers kept to them; they take about ten seconds, most
of it the writing. A question that names Reddit is not taken to the news,
since no headline says what people there think, and one the news has nothing
on is not answered from the day's top stories: for both he is told to say that
he cannot look it up, where before he might answer "I can't find that model"
as if he had tried. A headline older than half a year carries its year, so
that last year's review does not read as this month's. What to do with
headlines is said to him only in the turn that has them: told in his standing
instructions to call the news tool first, he took to saying "I looked this up"
in turns that had looked nothing up. With a search key the news steps aside by
itself.

A turn has an allowance of looking: four lookups, nine thousand characters
of results, and twenty seconds (`DOUGLAS_TURN_LOOKUP_S`), after which he has
to answer from what he has and is told so. A search that comes back poor is
otherwise searched again in other words for most of a minute. A tool call he
writes out as words when he has no lookups left is not part of his reply.
What was searched for is never written to the journal, only that a search
was made and how much came back.

The web search has met the real service (November 2026); the Reddit search
has not, and is still as written to Reddit's documentation and tested against
a stand-in. The field names were as documented and an answer takes half a
second to a second. Four things were not as the code had them, and the
stand-in now answers in the service's own words for each. The passages are
Markdown, a heading to a line, the page's own headline first, then lists,
bold and tables, and a video arrives as JSON (its name, and the comments under
it); cut to size as they came, up to a third of a page's share went on the
headline said a second time and on the marks. He now gets the words: the
headline is dropped where the title has it, a heading ends in a colon, a
table's row is its cells, data is read out a part at a time, and on a Reddit
page the lesser headings, which are the names of those who wrote, are dropped
as they are in the Reddit search. The service's status does not say which no
it was: a key it does not know gets 422 (`SUBSCRIPTION_TOKEN_INVALID`), and so
does a request it cannot make sense of (`VALIDATION`), while an address the
key's plan leaves out gets 400 (`OPTION_NOT_IN_PLAN`); the word in the body
does, so he is told which it was, the journal carries that word, and only a
refused key or a spent allowance stops the ordinary results from being asked
for. And in those ordinary results the date comes twice, in words ("August
14, 2026") and as a date: the code took the first and cut it to ten
characters, "August 14,". Last, real titles and addresses are long: six
pages at full length ran past what one lookup may hand over and the sixth was
cut off mid-word, so a page now gets its share while there is room, a smaller
one in what is left, and is left out when that is too little to say anything
in.

**The owner's own records.** With `JOBCONTEXT_URL` and `JOBCONTEXT_TOKEN` set,
he has one more tool: the owner's records on their own jobContext server (an
MCP server: the stories they have told about themselves and samples of how
they write, and, for someone looking for work, a whole job search:
applications, interviews, contacts, documents). `JOBCONTEXT_AREAS` names the
parts he may reach, `stories` for the stories and writing samples alone:
that is context about the person he talks to, and a job search is not
something everyone wants a house assistant bringing up. The pipeline asks the
server what it offers at start and puts the areas he may reach into his
instructions; the others he is not told about and cannot call. The key it is
given can do anything its owner can, so what he may do with it is decided by
the pipeline, not by the model:

- An action on the pipeline's own list of reads (`JC_READS`) is carried out
  when he asks for it.
- Every other action is taken to change something, a new one on the server
  included until someone lists it as a read. It is not sent. The turn ends
  there, and the reply is the pipeline reading the change back, field by
  field, from the very arguments it would send. It goes to the server only
  if the person's next message, from the same device and within three
  minutes, is a plain yes. "No" drops it, and so does anything else; either
  way he says so. Out of a microphone an "okay" is not a yes, nor is a yes
  from a recording with no sound in it: a speech recogniser handed room noise
  writes those by itself. What the server answered is passed on as it came:
  saved, turned down, or not known because it did not answer.
- Deleting is never offered (nor is anything a later version of the server
  names as a removal), and a web address is never put forward by voice,
  where it cannot be checked.
- Some of the server's answers run to a hundred pages (the list of every
  application, or every contact), and the model reads a few. Handed the first
  of them unmarked, it counted what it could see and called that the total.
  So a long answer comes back either as the entries that mention what he
  asked to `find` (a name, a word), or as its beginning under a notice that
  it is only the beginning and is not to be counted or summed up as the
  whole.
- He is told to look before he answers anything about the records, and the
  pipeline checks, because told is not enough: in testing he answered a
  second question about an application from thin air, interviewer's name and
  all. A question that uses the words of an area he can reach, or any
  question once the records are part of the conversation, that he answers
  without a single lookup is put to him again with an instruction to look.
  If he still does not, and the answer names names or numbers, it goes out
  ending "I did not look that up in your records."
- Once the records have been read in a conversation, `read_page` is off until
  the conversation ends (five quiet minutes, or "new conversation"). The
  records are private, and some of what is in them, a job advert or a
  recruiter's message, was written by strangers; text like that can ask a
  model to carry what it has read elsewhere, and fetching an address is how
  it would. The weather, news and Wikipedia go to fixed services and stay on.

The journal names each action and its arguments, never their values. What he
reads out of the records does end up in his replies, so in the transcript,
on the panel and out of the speaker: `JOBCONTEXT_VIA=phone` keeps the records
to the phone page.

**Why his instructions never change.** A turn used to begin with six seconds
of nothing. The model server keeps what it has already read and starts again
at the first word that differs, and the instructions ended with the time of
day; in the model's chat template the tools follow the instructions, so every
turn in a new minute read the tools and the whole conversation again, about a
thousand tokens. Now the instructions are the same text on every turn, for
every device, spoken or typed, and what belongs to one turn (the time, the
device, how the reply will be delivered) closes the person's own message in
square brackets. The same pair of questions a minute apart went from 6.8
seconds each to 2.6.

**A line for ChatGPT.** `douglas-chatgpt` lets ChatGPT talk to Douglas as one
AI to another, and nothing more. It is a small MCP server with one tool,
`ask_douglas`, added to ChatGPT as a custom connector. ChatGPT's servers are
on the public internet, so this is the one part of Douglas that is published
there: through Tailscale Funnel on port 443 (the only port ChatGPT will
use), which is why Open WebUI moves to tailnet-only `:8443` when it is set
up. The address carries a secret in its path and every other path is a bare
404. What arrives that way gets his built-in persona and no more: no
lookups, no eyes, no drawing, no records, none of the owner's instruction
files, facts or tone samples, no view of the house transcript. One question
at a time, a cap an hour and a cap on length; every exchange is in the
journal (`journalctl -u douglas-chatgpt -o cat`).

**How he sounds.** Piper, the voice he started with, is quick and clear and
sounds like a machine reading. He speaks with Kokoro instead: a small neural
voice (82M parameters) that sounds like a person and still runs on the CPU,
about ten times faster than real time, so a fifteen-second reply is ready in
a second and a half and none of the graphics memory is spent on it.
`douglas-tts` serves it on `:10210` (loopback) over the same Wyoming
`synthesize` Piper answers, so the pipeline only needed a different port.
`KOKORO_VOICE` in `tts.env` picks the voice (`bm_lewis` by default; the
model has dozens, British and American among them) and `KOKORO_SPEED` the
pace. Piper stays installed: if `douglas-tts` is down or fails, the pipeline
says so in its journal and speaks the reply with Piper (`TTS_FALLBACK_PORT`).
Kokoro reads naturally but does not act: it will not sigh or lean on a word.

**From a phone's own microphone.** `POST /hear` takes a turn recorded
somewhere else (base64 16-bit mono PCM, with an id) and treats it as a spoken
turn, except that nothing is played in the house: the reply is synthesised
and kept, and `GET /speech?id=` hands it back as a WAV to whoever sent the
recording (202 while he is still thinking; `nothing` if no words were heard in
it). `douglas-agent` and deck-api relay both, and the phone page on HTTPS uses
them for HOLD TO TALK.

**What he can see.** A picture is put into words by a model that can see:
his own, if it is one (`EYES_URL` pointing at `:8081`), or a second, small
vision model beside one that reads only text (`llama-eyes`, `:8082`). `POST /say` takes an
optional `"image"` (base64 JPEG, PNG, GIF or BMP); the pipeline has the vision
model describe it with the question in mind, hands that to Douglas as what he
saw, and marks the line `[picture]` in the transcript. The picture is kept for
the rest of the conversation so that he can take a second look (the `look`
tool) when a follow-up asks about a detail, and `read_page` on a picture's
address looks at it the same way. Pictures never leave Douglas. With no
vision model configured he refuses a picture and says why, and the phone page
does not offer its camera button.

**What he can draw.** With `IMAGE_URL` set he has a `draw` tool: an image
model on the same machine (`douglas-draw`: FLUX.1 schnell behind
stable-diffusion.cpp's server, on the same Vulkan backend as his own model)
turns a description into a 1024x1024 picture in about 25 seconds, one a
turn. The picture is kept on Douglas (`PICTURES_DIR`, readable by the
pipeline alone, the newest sixty), and its id goes out with his line in the
transcript: `GET /picture?id=` on the pipeline, relayed by `douglas-agent`
and by deck-api (`/api/douglas/picture`), is how the phone page and the
panel show it under that line. DougVoice has no screen, and he says where to
look. He has not seen what he made unless he looks (`look` takes the picture
he drew as readily as one he was sent), so he is told to hand it over
without describing it. What he was asked to draw is not written to the
journal, and the image server answers only this machine: it is cut off from
the network by systemd, since making a picture needs nothing from anywhere.

**What he can change.** With `IMAGE_EDIT` set as well he has an `edit` tool,
which works on the last picture in the conversation, a photo he was sent or
one he drew: "make it snow", "take the car out", "turn it into a
watercolour". A second model does it (FLUX.1 Kontext [dev], under Black
Forest Labs' non-commercial licence), and it is given the picture itself, so
what is in it stays: the same dog on the same porch. The image server holds
one model at a time, and the pipeline tells it which before each picture.
An edit is slow, about thirteen seconds a step at a megapixel, so it runs at
eight steps and at no more than 1024 pixels on its long side: a photo from
the phone takes about a minute and a quarter. That is too long to hold a
turn open. He answers at once that it is on its way, the edit runs behind
the conversation, and the picture arrives as a line of his own ("Here is the
edited picture.") on the same screens; he is told it has arrived with the
person's next message. While one picture is being made another is not
started, drawn or edited: he says the image model is busy. A request that
comes with a photo is `edit`'s work, and a `draw` in such a turn is turned
back once with the reason: `draw` never sees the photo, and would make a
different picture from a description of it.

**A photo he is sure he never got.** A picture reaches him as words, what
was seen in it, and he took the missing picture for a failure. Sent a photo
of a dog and asked to put the dog on a bed of clouds, he answered that he
could not pull the dog out of the photo and offered to draw one; in a probe
of fourteen kinds of request he also said the photo "did not come through",
that he had no way to edit photos, and that the edit was on its way: 22
first moves right in 42. Three things changed. The words that come with a
picture now say that it has arrived, that it is kept, and that `edit` works
on it. His instructions say what an edit can do (a new place or background,
things added or taken out, colours, clothes, weather, a style) and no longer
hand him a sentence to say: told to "say it is on its way", he said it in
place of making the call, so now the call comes first and the tool's answer
tells him what to say. And the program checks: when a picture comes with a
message and no edit is started, it asks the model one more thing on its own,
with one word for an answer (is a change to the picture being asked for?),
which the model gets right where its choice of tool wavers (24 of 24 sample
messages, half a second). On a yes he is told to call `edit`; if he still
does not, the program starts the edit with the person's own words. In a dry
run of whole turns, every one that asked for a change ended in an edit (30
of 30) and none that only asked about a photo did (9 of 9).

**Saying is not doing.** The first edits never started. He answered "It's on
its way" and called nothing, most of the time. The cause was in what he was
shown of his own past: the conversation kept his replies and not the tool
calls behind them, so every earlier picture looked like something he had
produced by announcing it. Now each exchange is remembered with its calls
and a short form of what came back (the first 300 characters), between the
person's line and his. In a probe of the same request the call was made 3
times in 12 before, 11 in 12 with the calls remembered, and 12 in 12 once
the note that closes every message also said that only the tool starts a
picture. A reply that still promises a picture when none was started is
caught by the program: he is asked again, told that nothing has started, and
if he promises a second time the person reads "I said a picture was on its
way, but I had not started one. Ask me again."

### DougVoice — Douglas's ears

DougVoice is a Raspberry Pi Zero 2 W wearing a PiSugar Whisplay HAT (WM8960
codec with two mics and a speaker, a 1.69" 240×280 LCD, an RGB LED, one
button). It does no inference: it is a [Wyoming
satellite](https://github.com/rhasspy/wyoming-satellite) on `:10700` that
Douglas's voice pipeline connects to, sending the mic one way and Douglas's
text-to-speech the other. Flash Raspberry Pi OS Lite (64-bit), then on the Pi:

    sudo DOUGLAS_AGENT_TOKEN=<from Douglas's /etc/douglas/agent.env> ./dougvoice/setup.sh
    sudo reboot          # first run only: loads the Whisplay sound card

`setup.sh` names the Pi `dougvoice`, joins the tailnet (set `TS_AUTHKEY` to skip
the login URL), installs the Whisplay driver, puts `wyoming-satellite` in a venv,
and installs three units, configured from `/etc/dougvoice/dougvoice.env`:

| Unit | Does |
|---|---|
| `dougvoice-satellite` | `wyoming-satellite` on `:10700`, mic and speaker through the `whisplaysound` ALSA card |
| `dougvoice-face` | the LCD and LED show **offline / idle / listening / thinking / speaking**; the button is push-to-talk |
| `dougvoice-wake` | local openWakeWord, only when `WAKE_WORD` is set |

**The driver is PiSugar's own.** It comes from
[PiSugar/Whisplay](https://github.com/PiSugar/Whisplay): its `install_driver.sh`
enables SPI and installs PiSugar's `whisplay-soundcard` overlay and WM8960
driver. PiSugar ships this driver themselves; it is not the Seeed or Waveshare
one. Their `runtime/whisplay.py` drives the LCD, LED and button, and the face
imports it from the same checkout. `WHISPLAY_REF` pins a commit. Check the card
with `aplay -l | grep whisplay`, and the levels with
`alsamixer -c whisplaysound`.

**What the face shows** comes from whichever source changed most recently:
the satellite's own events (`--event-uri`), Douglas's voice state polled from
`douglas-agent` once a second, and the button, which lights "listening" the
moment it is pressed. Douglas unreachable, no pipeline on Douglas, or the
pipeline not connected to the satellite all show **offline**, a red ring.
"Thinking" breathes amber on the LED, so a long answer still looks alive.

**Push-to-talk, two ways:**

- *Stream mode* (default, `WAKE_WORD` empty). The satellite streams the mic to
  Douglas whenever Douglas is connected, about 32 KB/s, and Douglas's pipeline
  gates it. The button posts `{"action": "start|stop|cancel", "via": "button"}`
  to `douglas-agent`'s `/ptt`, the same path the deck's on-screen button
  uses. Hold to talk. A tap shorter than 0.25 s cancels, and a press while
  Douglas is thinking or speaking cancels the turn. If a wake word is wanted
  in this mode, it runs on Douglas, against the stream.
- *Wake mode* (`WAKE_WORD=ok_nabu`, then re-run `setup.sh`). openWakeWord runs
  on the Pi, and the satellite streams only after a detection. The face sits
  between the two as the satellite's wake service, and the button injects a
  detection, so it still opens a turn. The turn then ends on Douglas's
  voice-activity detection. One trade-off: the deck's on-screen push-to-talk
  cannot open the mic in this mode, because nothing reaches Douglas until the
  Pi has woken. openWakeWord takes roughly 150 MB of the Zero's 512 MB, and
  its unit is capped at 200 MB so it cannot starve the satellite.

**Hardware caveats.**

- **Use standoffs.** The Whisplay sits on a shallow GPIO header. Without
  standoffs between the HAT and the Zero, pressing the button rocks the board
  on its pins, and the audio and LCD drop out intermittently.
- **Do not pair it with a PiSugar S battery board.** That combination has been
  reported to destroy both the Whisplay and the Zero under it. Power DougVoice
  from USB, or use a battery that PiSugar documents as Whisplay-compatible.

### Docker at boot

Also two halves, and the Linux one is already done — it just doesn't look
like it.

**Linux** runs two independent Docker daemons and it matters which one you
are talking to. The system `docker.service` is enabled and comes up at boot,
and containers with a `unless-stopped` restart policy come back with it (the
k3d cluster does). Docker Desktop is a *separate* daemon behind a *separate*
socket, shipped as a **user** service that is disabled by default:

    systemctl --user enable docker-desktop     # start it at logon

The trap is that `docker context ls` decides which daemon the CLI talks to.
With the `desktop-linux` context selected while Desktop is not running, a
plain `docker ps` fails with a socket error that reads like "Docker is not
installed" — while the system daemon is up the whole time, quietly running
your containers. `docker context use default` points the CLI at the system
daemon; containers started under one daemon are invisible to the other.

**Windows** is Docker Desktop's own setting — Settings → General → *Start
Docker Desktop when you sign in*. Deliberately not scripted here: Docker
Desktop manages its own autostart, and a second mechanism fighting it is
worse than the toggle.

## How "is it up?" is decided

The orchestrator probes two Prometheus exporters that belong to a
separate project (a Grafana wallboard): `:9105` answering means Linux,
`:9106` means Windows. This repo does not install them — if you run
this without that stack, repoint `WIN_PORT`/`LINUX_PORT` at
anything that answers HTTP only under the OS in question.

The two boot agents are the better answer, and need no code change to use:
the probe hits the root path, and both agents answer `/` as well as
`/status`, so `WIN_PORT=9107` and `LINUX_PORT=9108` in `boot.env` are all
it takes. Both ship with this repo, both run from startup rather than at
logon, and both are more reliable than the exporters — which belong to
another project and have died on their own more than once.

### GPU telemetry

The DECK gauges used to come only from that same third-party exporter, so each
time it died the panel reported "no data" about a GPU sitting there at 47°.
The sim agent now serves its own `/metrics` from `nvidia-smi`, and deck-api
prefers it under Windows, falling back to `WIN_METRICS_URL` when it is absent —
so a deployment without the agent behaves exactly as it always did.

This introduces no metrics stack: no Prometheus, no retention, no second
scrape history. It is the current reading, in exposition format only because
deck-api already parses that shape, which made the Pi-side change a URL
preference rather than a new parser. The rolling window the sparklines draw
still lives in the browser and still dies with the page.

### The visualiser

`GET /api/audio` returns 64 spectrum bands measured on the workstation, and
both the AUDIO surface and the DECK rail draw them. Loopback on the default
*render* endpoint either way, so it needs no virtual cable and changes nothing
about what reaches the Scarlett — WASAPI under Windows, `parec` on
`@DEFAULT_MONITOR@` under Linux, and deck-api picks by whichever OS is booted
exactly as `/media` and `/monitor` do.

The band mapping exists in both agents and is held to the same properties by
both test suites, because two implementations of one contract is how a contract
drifts. The Linux side needs `numpy` (`pip install numpy`); without it `/audio`
answers with that as its reason rather than pinning a core on a pure-Python
4096-point FFT.

The bands are real. There is no fallback pattern and no idle animation: when
nothing is playing the bars are flat, and when there is no capture session the
widget is hidden entirely. A visualiser driven by track position or a canned
waveform would be the same class of lie as drawing a commanded gear position,
which is the one thing this panel refuses to do anywhere else.

Note what a failed probe does *not* distinguish: a machine that is off,
a machine asleep ignoring magic packets, and a machine that is up with a
dead exporter all look identical from the Pi. All three log
`no response — sending WOL`. If a trigger reaches the Pi and nothing
boots, check whether the machine was merely asleep before suspecting
fauxmo — see the WOL note in Troubleshooting.

## Tests

```
bash tests/test_*.sh          # all of them; each is standalone
python3 pi/deck-api/test_phases.py
python3 pi/deck-api/test_douglas_routes.py   # deck-api's Douglas routes, with stand-ins
python3 douglas/test_douglas_agent.py   # fake /proc, /sys and llama.cpp
python3 douglas/test_douglas_voice.py   # fake satellite, STT, llama.cpp and TTS
python3 douglas/test_douglas_tts.py     # the Kokoro voice service, with a stand-in model
python3 douglas/test_douglas_chatgpt.py # ChatGPT's line to Douglas: the secret path, the exchange, the persona
python3 dougvoice/test_dougvoice.py     # no Pi, HAT or Douglas needed
```

| Suite | Covers |
|---|---|
| `tests/test_boot_agent.sh` | the Linux boot agent's auth matrix, against a real listener on a throwaway port with a stubbed reboot |
| `tests/test_grub_entry.sh` | finding the Windows menuentry in single- and double-quoted `grub.cfg` |
| `tests/test_efi_utils.sh` | finding the Windows and GRUB entries in `efibootmgr` output — by description, by loader path, and via the generic `UEFI OS` label — and that reordering the boot list keeps every other entry |
| `tests/test_media_agent.sh` | `ddcutil` parsing against the layouts it really emits, that an empty monitor list explains itself, and that each SCREENS card commands its own panel rather than its neighbour |
| `tests/test_deck_ui.sh` | the panel's pure logic in node — formatting, map arithmetic, the teleport threshold, and the rolling telemetry window |
| `tests/test_sim_agent.sh` | which sim event each Flight Deck command resolves to and with what parameter, and that every autopilot bug publishes the mode that reads it |
| `tests/test_pin_fleet.sh` | `pi/pin-fleet.sh` against throwaway files and a stand-in `tailscale`: both names pinned once, at the address Tailscale gives; a second run and `off` leaving the files as they should be, to the byte; cloud-init's template written only where it is in use; and nothing changed when a machine is unknown, its address is not a tailnet one, or half a pin is already there |
| `pi/deck-api/test_phases.py` | phase mapping against the orchestrator's real log lines |
| `douglas/test_douglas_voice.py` | the voice pipeline's state file and transcript, read back through `douglas-agent`'s own readers; a whole turn, cancel, barge-in, failures returning to idle; the Wyoming framing and clients against fake servers; the PTT endpoint as the agent relays to it; the owner's records against a stand-in server: which areas he may reach, what counts as a read, that a change is read back and sent only on a yes (and what counts as one), that deletes and redirects go nowhere, that a long answer is marked as partial or searched, that an answer given without looking is asked for again, and that the journal holds no values; drawing against a stand-in image server: the picture kept and fetched by its id, one a turn, and nothing of the request in the journal; editing against the same stand-in: the reply at once and the picture later as its own line, one picture at a time, a failed edit owned up to, a photo sent with the request going to `edit` and not `draw`, the tool calls kept in the conversation, a picture promised without a call asked for again, and a change to a photo that he turns down put to the one-word question, asked for again, and at last started by the program; the weather and the news looked up by the program before he answers, for the place or the topic asked about, and left alone when the message only uses the word; the web search and the Reddit search against stand-in services: offered only with a key, what is sent and what comes back, a refused or spent key said to be so, each standing in for the other, the news standing in for both when there is neither (its headlines handed over as headlines and no more, a question about Reddit or in the words of the owner's records left out of it, and nothing handed over when the news has nothing), a second asking that keeps what the program looked up, and neither the words nor the keys in the journal; the web search's answers in the shape the real service gives them (Markdown passages, a video as data, a forum's names, titles and addresses as long as real ones, the ordinary results' two dates and repeated description) and its refusals in its own words (an unknown key, a plan that leaves an address out, a request it cannot read); with a web search to make, a question with no searching word in it put to the wider question and searched for, what asks nothing left alone, and the note that goes with a search the program made; a turn's allowance of lookups |
| `douglas/test_douglas_tts.py` | the Kokoro voice service against a stand-in model: a reply as audio-start, chunks and audio-stop at the model's rate; the default voice, a named one, and a name the model lacks; silence for nothing to say; a model failure as an error event with the service still up; nonsense and half an event on the wire; three replies at once spoken one at a time; `describe`; and a journal with sizes and timings but never the words |
| `dougvoice/test_dougvoice.py` | the satellite's command line in both modes; which state the face shows as satellite events, Douglas's polls and the button race each other; push-to-talk semantics; the LCD frames and LED colours; Wyoming framing; and the wake shim end to end on real sockets |

`test_deck_ui.sh` skips cleanly where node is absent; the Pi runs the panel in
Chromium and has no reason to have node installed. `test_sim_agent.sh` skips
the same way without the .NET SDK, and needs neither Windows nor the MSFS SDK
where it does run — it builds the agent against its SimConnect stub and
reflects into the result.

The sim-agent suite is aimed at one failure mode in particular: a command that
compiles, transmits, is reported accepted, and does nothing, because the number
in it was right-looking and wrong. `FLAPS_SET` taking a 0–16383 handle position
rather than the detent index it is invariably handed is exactly that, and it
sent every flap command on the panel to UP.

The UI suite is deliberately weighted toward the failures this panel has
actually had, rather than toward coverage: a trail drawn straight across a
relocate, a sparkline carrying a value through an outage instead of showing a
hole, an ETE invented from a ground speed too small to divide by, and a
missing reading rendered as a zero.

## Test matrix

| Starting state | Expect |
|---|---|
| Target OS already up | greeting + launch fire immediately, no reboot |
| Other OS up | reboot into the target in ~30 s, greeting on logon |
| Powered off, target Windows | WOL powers on straight into Windows (~1 min) |
| Powered off, target Linux | WOL powers on into Windows, then one chained reboot (~2 min) |

WOL cannot pick a boot target, so from full-off the machine lands wherever
the firmware boot order points — Windows, once `set-boot-order.sh` has run.
See [Which OS a cold boot lands in](#which-os-a-cold-boot-lands-in).

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
- **WOL does nothing from sleep**, though it works from full off. A
  different problem with the same symptom, and the one that silently
  breaks scheduled boots. On Windows check `powercfg /a`: "Standby (S0 Low
  Power Idle)" means Modern Standby, where wake-on-magic-packet is
  unreliable by design — switch the BIOS to S3 if it offers the choice, or
  set sleep to Never on a machine whose whole job is being summonable.
  `powercfg -devicequery wake_armed` must list the NIC; if it does not,
  the `powercfg /deviceenablewake` grant did not stick. Under Linux,
  `ethtool <iface>` should report `Wake-on: g`.
- **A cold "boot Windows" shows GRUB counting down to Ubuntu.** The
  firmware boot order still puts GRUB first. `sudo ./linux/set-boot-order.sh`
  from the Linux boot; `--print` first if you want to see the entry numbers
  before anything changes.
- **…and it comes back after a power cycle**, `efibootmgr` having reported
  success. Many consumer boards keep their own boot priority list in NVRAM
  and re-sync `BootOrder` from it at POST; this one does. Nothing in
  software can win that argument — set **Boot Option #1 = Windows Boot
  Manager** in the BIOS itself. `BootNext` is honoured regardless, so the
  Linux→Windows and Windows→Linux legs keep working while the cold default
  is wrong, which is what makes this look like a partial fix rather than a
  reverted one.
- **A cold "boot Windows" now works, but "boot Linux" ends up in
  Windows.** The Windows leg could not find the GRUB firmware entry, so it
  fell back to a plain reboot. `C:\ProgramData\dualboot\boot-agent.log`
  records exactly that; `bcdedit /enum firmware` shows what the firmware
  actually offers. Re-running `windows\setup.ps1` re-derives and caches it.
- **Alexa acknowledges, the Pi logs the trigger, and nothing boots.** Read
  past the generic `WARN: reboot request failed` — it covers ssh auth
  failure, an unreachable host, and a token rejection alike. Run the
  reboot leg by hand from the Pi and read the real error. A `Permission
  denied (publickey)` here usually means `LINUX_SSH` names the wrong
  account: it must be the *workstation's* username, which is easy to
  confuse with the Pi's own.
- **Machine wakes but nothing is spoken.** Nobody logged on — the greeting
  is a logon task. See auto sign-in above.
- **Douglas and DougVoice both read OFFLINE, and both are up.** The Pi has
  lost their names, not the machines. On the Pi, `getent hosts douglas`
  finds nothing while `tailscale ping douglas` answers, and
  `/etc/resolv.conf` begins `# Generated by NetworkManager`. The Wi-Fi
  dropped and rejoined; NetworkManager rewrites that file on every rejoin and
  Tailscale writes its own lines back, but after several rejoins in a row it
  can lose the last round (`journalctl -u tailscaled | grep trample` shows
  each one). `sudo tailscale set --accept-dns=false`, then
  `--accept-dns=true`, has Tailscale write the file again, and the deck shows
  both within seconds. `sudo ./pi/pin-fleet.sh` takes DNS out of it: the two
  names go into `/etc/hosts`, and into cloud-init's template where cloud-init
  rebuilds that file at every boot. With the pins in place, a machine that
  reads OFFLINE after it was added to the tailnet again has a stale pin: run
  the script once more.
- Logs: `journalctl -t flightsim-boot` on the Pi (plus
  `~/flightsim-boot.log` for detached runs).

## Security notes

- There are two ways to reboot the workstation into Windows, and both are
  credential-guarded. The Pi's ssh key is bound in `authorized_keys` to
  `command="sudo /usr/local/bin/boot-to-windows"` with `restrict`, and the
  sudoers rule covers exactly that one script — the key can reboot the box
  into Windows and nothing else. The Linux boot agent on `:9108` is the
  faster path and needs `LINUX_AGENT_TOKEN`; the orchestrator tries it
  first and falls back to the ssh key when no token is configured or the
  agent does not answer.
- Both boot agents fail closed. A wrong token gets a 403, and a missing or
  empty token file refuses `/reboot` outright rather than degrading to "no
  token required" — so a half-finished install cannot leave an
  unauthenticated reboot endpoint listening on the LAN.
- The Pi does hold both agent tokens, in `/etc/flightsim/boot.env`. They
  are reboot-only credentials: neither agent exposes a shell, a file
  transfer, or any command beyond the reboot it was built for. `/status`
  and `/` are unauthenticated on both, being reads.
- The Windows agent runs as SYSTEM (it must reboot before anyone logs on).
  Both are hand-rolled HTTP listeners on LAN ports — fine behind a home
  router, not something to expose to the internet.
- fauxmo is LAN-only, "off" is a deliberate no-op, and the state probe is
  a read.
- The speech services run on onnxruntime. The version installed here
  (1.32.0) uploads usage telemetry to Microsoft on Linux unless
  `ORT_DISABLE_TELEMETRY=1` is set (watched with `strace`: one HTTPS
  connection and a device-id file without it, nothing with it). The units for Whisper, Piper, Open WebUI and
  the Kokoro voice all set it, and `douglas-tts`, which needs no network at
  all, is also limited to loopback by systemd (`IPAddressDeny=any`). So is
  `douglas-draw`, the image server.
- The web search key and Reddit's id and secret live in
  `/etc/douglas/voice.env` (root:douglas, 640) and go only to the service
  each belongs to, in the header that service asks for. What is searched
  for goes to the service searched and nowhere else, the journal included.
  With a web search switched on more goes out than without: any question
  that the wider check says needs a search is searched for, in words taken
  from the message itself.
  What comes back is other people's text, and is handed to the model marked
  as information, not instructions.
- Douglas's key to the owner's jobContext records is a whole one: the server
  has no narrower kind, so which parts he reaches (`JOBCONTEXT_AREAS`) is the
  pipeline's doing. It lives in `/etc/douglas/voice.env` (root:douglas,
  640), is sent only over HTTPS and never to a redirect, and is used under
  the pipeline's rules, not the model's: reads on request, changes only after
  a read-back and a yes, no deletes. Revoking the key on the server ends all
  of it.
- One thing on Douglas is published to the internet, and only if it is set
  up: `douglas-chatgpt`, through Tailscale Funnel on port 443. Its key is a
  secret in the path, it runs as a throwaway user with the filesystem
  read-only, and it can do one thing, pass a message to the model under a
  persona that knows nothing about the household. Funnel publishes a whole
  port, so nothing else may share that one: `provision.sh` puts Open WebUI on
  `:8443` once the connector's `chatgpt.env` exists.
- No secrets in the repo. Both agent tokens are generated at install time,
  and `.gitignore` covers `*.token`.

## Origin

Extracted from a job-search project's `scripts/flightsim/` directory,
where it was living for no better reason than that's where it got
written. History preserved via `git subtree split`. Internal identifiers
keep the `flightsim` prefix (`/etc/flightsim`, `flightsim-boot.sh`, the
`FlightSimBootAgent` task) because renaming them would mean reinstalling
every deployed piece.

## License

MIT — see [LICENSE](LICENSE).
