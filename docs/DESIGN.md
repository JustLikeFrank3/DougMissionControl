# Flight Deck — design notes

Proposal for a Corsair Xeneon Edge touch panel that fronts the boot
orchestration in this repo, plus a Prometheus/Grafana metrics plane
covering the workstation (both OSes), a Mac mini, and the Pi itself.

Nothing here is built. The rendered sketch, with the panel mockup and the
architecture diagrams, is [`dashboard-sketch.html`](dashboard-sketch.html).

## Why the panel hangs off the Pi

The workstation is the subject of every control on the screen. A panel
driven by the workstation goes black exactly when you reboot it, which is
the one thing it cannot do. The Pi already holds the boot logic, the ssh
key, and the lock file — it is the only always-on node in the system.

The Edge accepts HDMI 2.0 alongside USB-C DP-alt, and its touch panel is a
standard USB HID digitizer, so a Pi 5 drives it with micro-HDMI for video
and USB-A for touch. No vendor software in the path.

## The one refactor everything else rests on

Today `fauxmo` calls `flightsim-boot.sh` directly, so a voice-triggered
boot is invisible to anything that didn't start it. Insert **`deck-api`**
on the Pi as the single entry point; `fauxmo`'s `on_cmd` becomes a curl to
it. The orchestrator script itself is unchanged.

| Endpoint | Does |
|---|---|
| `GET /api/state` | Workstation OS, uptime, boot-in-flight phase, per-host rollups |
| `GET /api/events` | SSE stream of state deltas — phase changes push, no polling lag |
| `POST /api/boot` | `{target: windows\|linux, intent: sim\|squadrons\|plain\|code}` — takes the flock, records intent, execs the orchestrator |
| `POST /api/launch` | Target already up: fire greeting + profile via the boot agent's `/launch` |
| `POST /api/abort` | Release the flock, mark the run abandoned (cannot un-send a WOL packet) |
| `POST /api/power` | Shut down / sleep a host — needs a `/shutdown` verb in `boot-agent.ps1`, `pmset` over ssh for the Mac |
| `POST /api/hook/greeting` | Windows reports the greeting spoke and the profile launched |

Same trust model as `boot-agent.ps1`: LAN-bound, shared-token guarded, no
stored credentials. The kiosk browser reaches it over loopback and needs
no token.

## Boot phases

The panel renders a seven-phase track. Each phase has something that
actually reports it:

| # | Phase | Signal |
|---|---|---|
| 1 | TRIGGER | `deck-api` takes the flock |
| 2 | PROBE | `:9105` / `:9106` — which OS answers |
| 3 | KICK | WOL sent · ssh forced-command · agent reboot |
| 4 | PING | ICMP — the NIC has power |
| 5 | OS UP | target exporter answers |
| 6 | LOGON | greeting task fires |
| 7 | LAUNCHED | hook POST from Windows |

Phases 1–5 are already observable from the existing script. 6 and 7 are
why `jarvis-greeting.ps1` gains a two-line callback — without it the panel
claims "Windows is up" a good minute before the sim is flyable.

Warm reboot ≈ 30 s · cold boot ≈ 2 min · chained double boot ≈ 3 min.

**Unchanged dead end:** WOL cannot select a GRUB entry, so from full off
the machine always lands in the saved default. The tile should show
"+1 reboot" rather than implying both targets are equally fast.

## UI notes

2560 × 720 at ~180 ppi means a 44 px target is 6 mm. Every touch target in
the sketch is ≥ 158 px.

- Three fixed columns — **act** (600 px) / **context** (1100 px) / **fleet**
  (860 px). The boot controls are never behind a tab; reaching them is what
  you do while the machine is unusable.
- Destructive verbs arm on a 1.5 s hold with a fill sweep, not a confirm
  dialog — a modal on a 720 px-tall ribbon eats the whole screen.
- State is carried by word and shape as well as hue, so it survives
  colorblindness and a dim panel.
- One hue (cyan) for every telemetry trace; identity comes from the card
  title. Green/amber/red stay reserved for status. Magenta is the boot
  affordance and nothing else.
- Run under `cage` (Wayland kiosk compositor), not a desktop. Force the
  mode with `video=HDMI-A-1:2560x720@60` in `cmdline.txt` if EDID doesn't
  offer it. Brightness over DDC/CI with `ddcutil` for an evening dim,
  wake-on-touch.

## Metrics plane

Prometheus, Loki, and Grafana all run **on the Pi**. Hosting the collector
on the workstation would delete the metrics for every event worth
measuring — reboots, crashes, thermal shutdowns.

This requires an NVMe HAT or USB SSD on the Pi before anything else in this
section gets built. A 15 s scrape across six targets kills a microSD card
in months.

### Exporters

| Host | Port | Exporter | Gives |
|---|---|---|---|
| Windows | `:9182` | `windows_exporter` | CPU, memory, logical_disk, net, service, thermalzone, process, os |
| Windows | `:9835` | `nvidia_gpu_exporter` | Clocks, utilisation, VRAM, power, temp, fan |
| Windows | `:9106` | *existing* `gpu-exporter.ps1` | **Leave in place** — the "Windows is up" probe |
| Windows | `:9110` | PresentMon shim (phase 4) | Frametime, 1 % lows during a sim session |
| Linux | `:9100` | `node_exporter` | CPU, load, memory, filesystem, net, hwmon |
| Linux | `:9835` | `nvidia_gpu_exporter` or DCGM | Same GPU series as Windows, so one dashboard spans both |
| Linux | `:9105` | *existing* `ollama-exporter.py` | **Leave in place** — the "Linux is up" probe |
| Mac mini | `:9100` | `node_exporter` (darwin) | CPU, memory, disk, net — nothing Apple-Silicon-specific |
| Mac mini | `:9101` | `macmon` → textfile shim | P/E cluster freq, GPU & ANE, package power, thermal pressure |
| Pi | `:9100` | `node_exporter` + textfile | SoC temp, `vcgencmd` throttle flags, NVMe wear |
| Pi | `:9115` | `blackbox_exporter` | ICMP + HTTP probes of every host and the WAN |
| All | `:9633` | `smartctl_exporter` | Drive health, reallocated sectors, SSD life |

The two existing exporters on `:9105`/`:9106` are load-bearing for boot
detection. Add alongside them; do not migrate the probes.

### Two things dual-boot breaks

**Label so the timeline survives.** Scrape Windows and Linux with the same
`instance="workstation"` and a distinguishing `os` label, not as two
unrelated hosts. One CPU panel then reads continuously across a reboot.
Set *Connect null values: never* so the gap where neither OS was up stays
visible — that gap is real information.

**Don't alert on a normal reboot.** `up{job="windows"} == 0` fires every
time you boot into Linux. The meaningful condition is *neither* OS
answering for longer than a boot takes: alert on `absent()` over the union
of both jobs with a 10 minute `for:`, and let `deck-api` publish a
`flightdeck_boot_in_flight` gauge to suppress it outright.

### Dashboards

Provisioned as JSON under `observability/grafana/dashboards/` via file
provisioning, so rebuilding the Pi restores them. `deck-api` feeds Grafana
annotations — every boot, launch and shutdown lands as a vertical line,
which is what makes the sim-session graphs readable.

| Dashboard | Question | Phase |
|---|---|---|
| Flight Deck Overview | Is everything alive, what is the workstation doing? Laid out for 2560 × 720 as a kiosk panel | 3 |
| Workstation — Windows | CPU/GPU/VRAM/thermals/disk/net under load | 3 |
| Workstation — Linux | Same shape, plus containers / Ollama / builds | 3 |
| Mac mini | P vs E cluster residency, GPU & ANE, package watts, thermal pressure | 4 |
| Boot Orchestration | Boot durations, which trigger fired, success rate, where failures stall | 4 |
| Network & Reachability | Who is up, inter-host RTT, WAN latency and loss | 3 |
| Storage & SMART | Fullness and drive health everywhere, including the Pi's NVMe | 4 |
| Sim Session | Per-flight frametime, 1 % lows, GPU curve, session length | 5 |
| Pi Health | SoC temp, throttle flags, NVMe wear, service uptime | 3 |

## Proposed layout

```
pi/deck-api/                    boot triggers, state, SSE
pi/deck-ui/                     the kiosk web app (no build step)
pi/setup-display.sh             Edge mode, touch, cage + kiosk
pi/setup-observability.sh       prometheus + grafana + loki on the Pi
observability/prometheus/       prometheus.yml, alert rules
observability/grafana/          provisioning + dashboards/*.json
observability/exporters/        per-OS installers
windows/, linux/                existing, plus the greeting callback
```

## Build order

1. **Bring the panel up.** Edge on the Pi at native resolution, touch
   calibrated, `cage` + browser on a placeholder. De-risks the only real
   unknown and depends on nothing else.
2. **`deck-api` and UI v1.** Service, state file, SSE, four boot tiles,
   live phase tracking. Repoint `fauxmo`. The screen now fully replaces
   the voice trigger.
3. **Prometheus, Grafana, core dashboards.** Pi on NVMe, exporters on
   Windows/Linux/Pi, blackbox probes, five dashboards.
4. **Mac mini, boot telemetry, greeting hook.** Apple-Silicon exporters,
   boot events as metrics and annotations, phase 7 made real, Loki.
5. **Polish.** Frametime capture and the sim-session dashboard, phone
   alerts, scheduled dimming, hold-to-confirm, shutdown path.

## Open questions

- **Mac mini — Apple Silicon or Intel?** Changes the exporter set
  completely.
- **Pi model and storage.** A Pi 4 on microSD drives the screen but should
  not host Prometheus.
- **Where does the Edge physically live** — magnet-mounted in the case, or
  on the desk? Decides whether the Pi moves into the case and how far the
  two cables run.
- **Keep GRUB's default as Linux?** Keeping it preserves the
  Windows → Linux leg; flipping it makes cold sim starts faster and breaks
  that leg. Only one can be true.
- **Alerts to the phone?** Worth it mainly for "the Pi itself is unhappy",
  since it becomes the single point of failure.

## Verify on arrival

- Edge power draw — give it its own USB-C PD supply rather than pulling
  from the Pi, which is already budgeting for NVMe.
- That the Pi's EDID read offers 2560 × 720 @ 60 without a forced mode.
- That the digitizer's coordinate mapping is correct without calibration
  when the panel is the only output.
