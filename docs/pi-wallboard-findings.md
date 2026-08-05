# Pi wallboard — survey findings

From `pi/inspect-wallboard.sh` run on `Node1` (Raspberry Pi 4B, 4 GB),
2026-08-05. The raw survey lives at `docs/pi-wallboard-survey.md` on the Pi
and is regenerable by re-running the script; this file is the analysis and
the decisions that follow from it.

**Headline: there is no cage-shaped hole to fill.** The Pi already runs a
Wayland compositor with a kiosk browser as its client. Flight Deck slots in
as a client of that compositor, not as a replacement for it.

## Answers to the five questions

### 1. What launches the browser

Not a systemd unit. The chain is:

```
lightdm  (autologin-user=fvm3, autologin-session=rpd-labwc)
   └─ labwc                                    ← the Wayland compositor
        └─ ~/.config/autostart/grafana.desktop
             └─ /usr/local/bin/wallboard-kiosk.sh
                  └─ chromium --kiosk --ozone-platform=wayland
                       http://localhost:3000/playlists/play/cftmf9c1waosga?kiosk
```

`~/.config/autostart/grafana.desktop` is the seam. Everything else —
lightdm, labwc, the session — stays untouched.

### 2. Does the kiosk own tty1 / DRM?

**No — `labwc` does.** Chromium is an ordinary Wayland client
(`--ozone-platform=wayland`), one of several possible clients.

This kills the earlier plan. `pi/setup-display.sh` installs **cage** and
starts it on tty1 with `PAMName=login`; on this host that fights lightdm and
labwc for seat0 and would take the wallboard down. Cage is the wrong tool
here and is dropped for this Pi.

`/tmp` is **already tmpfs** (1.9 G), so the `tmp.mount` step is redundant too.

### 3. Anonymous Grafana access

**Yes.** Grafana 11.1.0, `GET /` → 200, and `wallboard-kiosk.sh` already
queries `/api/datasources/proxy/uid/prometheus/...` with no credentials —
its own comment says "no credentials on disk". The EVALS surface needs no
token and no service account.

### 4. Playlists and the OS mapping

| Name | UID | Chosen when |
|---|---|---|
| `jcmcp-wallboard-linux` | `cftmf9c1waosga` | `max(up{job="gpu-windows"})` is absent/0 |
| `jcmcp-wallboard-windows` | `bftmf9cl1io74e` | that query returns `1` |

Both at a 60 s interval. Ten dashboards exist: `kiosk-app`, `kiosk-cloud`,
`kiosk-cluster`, `kiosk-evals`, `kiosk-gaming`, `kiosk-ollama`,
`kiosk-provenance`, `jobcontext-evals`, `jobcontext-overview`,
`temp-nightly`.

Three behaviours in that script are load-bearing and must survive:

- **UIDs are resolved by name at runtime.** The comment records why: Grafana
  re-mints UIDs, and baking one in "broke every time the playlist was
  recreated". Flight Deck must look playlists up by name too.
- **`unknown` is not `linux`.** A failed probe returns `unknown` and the
  kiosk deliberately does not swap, so a network blip cannot flap the
  display. Only a definite answer that differs triggers a change.
- **A single-instance `flock`** on `/run/user/1000/wallboard-kiosk.lock`,
  after an incident where relaunching bred "4 loops × 68 chromium processes
  and starved the Pi" (2026-07-20).

Plus `wlrctl pointer move 9999 9999` every 30 s to park the cursor offscreen,
and a three-strike health check that restarts chromium.

### 5. Headroom

```
Mem: 3795 total · 1868 used · 1927 available   Swap: 2047 total · 150 used
```

Largest resident: `k3s-server` 552 MB, **chromium ~762 MB across processes**,
`grafana` 184 MB, `containerd` 161 MB, `prometheus` 136 MB, `labwc` 104 MB.
Load 0.49, SoC 56 °C, `throttled=0x0` — the fan and heatsinks are doing their
job.

**One browser, confirmed.** A second chromium would cost ~700 MB against
1.9 GB available with swap already in use. deck-api is Python stdlib at
~50 MB and is not the problem; a second browser would be.

## Two things the survey corrected

**The Pi does not boot from the thumb drive.** Root is `/dev/mmcblk0p2` —
a 28 GB **microSD**, 64 % used. The 64 GB USB drive is `Flash_Drive
jcmcp-backup`, a backup volume. So Prometheus (7-day retention,
`--storage.tsdb.path=/prometheus` on local-path storage) has been writing to
the SD card for 21 days.

That is pre-existing and not Flight Deck's to change. It does make the
"Flight Deck adds no TSDB of its own" decision more clearly right, not less —
and the 7-day retention is what keeps it survivable.

**Two HDMI connectors are present** (`card1-HDMI-A-1`, `card1-HDMI-A-2`).
Whether a display is attached to each was not established. labwc handles
multiple outputs, so the Edge could join alongside the TV during transition —
at the cost of rendering both, which the memory budget does not favour.

## The integration that follows

Replace `~/.config/autostart/grafana.desktop` with a Flight Deck entry that
launches a wrapper derived from `wallboard-kiosk.sh` — same flock, same
Grafana health wait, same `wlrctl` cursor parking, same three-strike
restart — pointing chromium at Flight Deck's UI instead of directly at a
playlist URL.

The EVALS surface then shows the playlist inside Flight Deck. Two ways:

| | Cost | Result |
|---|---|---|
| **`<iframe>` the playlist URL** *(preferred)* | one line in Grafana's config: `[security] allow_embedding = true` | Instant surface switching, the 72 px strip stays visible over the wallboard |
| **Swap the chromium URL** | none | ~2–3 s blank on every switch, no strip during EVALS — but it is exactly what the existing script already does, so it is proven on this box |

Grafana defaults to `allow_embedding = false`, which sends
`X-Frame-Options: deny` and blocks the iframe. Enabling it is a small,
reversible change to jobContext's Grafana deployment that *enables* its
dashboards to be displayed rather than altering them. If that is unwelcome,
the URL-swap fallback needs no change at all.

The OS probe can come from deck-api directly — it already determines the
booted OS from `:9106` / `:9105` / `:9107` — but the existing semantics
(`unknown` does not flap, definite-and-different triggers a swap) must be
carried over rather than reinvented.
