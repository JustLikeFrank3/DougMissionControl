#!/usr/bin/env bash
# Pins linux/media-agent.py's ddcutil parsing against real `ddcutil detect
# --brief` layouts, with ddcutil stubbed — the parser is the half that failed
# silently in the field, because every error path collapses to an empty
# monitor list and SCREENS renders that identically to "no monitors here".
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 - "$repo_dir" <<'PY'
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location(
    "media_agent", f"{sys.argv[1]}/linux/media-agent.py")
ma = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ma)

failures = []


def detect(text):
    """Run _detect() with ddcutil stubbed to return `text`."""
    ma._buses = None
    ma._last_error = None
    ma._ddcutil = lambda args, timeout=10: text
    return ma._detect()


def check(name, got, want):
    if got != want:
        failures.append(f"{name}\n     got:  {got}\n     want: {want}")


# The layout ddcutil actually emits: the bus path sits mid-line after
# "I2C bus:", not at the start of it. Anchoring to the start of the line found
# nothing and returned [] on a machine where DDC worked perfectly.
check("brief layout", detect("""Display 1
   I2C bus:  /dev/i2c-4
   DRM connector: card1-DP-1
   Monitor:  DEL:DELL U2415:7MT018AK0DPL

Display 2
   I2C bus:  /dev/i2c-5
   Monitor:  ACM:VG270U:0x12345678
"""), [{"bus": "4", "model": "DELL U2415", "connector": "card1-DP-1"},
       {"bus": "5", "model": "VG270U", "connector": ""}])

# An EDID synopsis block interleaves "Model:" lines that must not be mistaken
# for the "Monitor:" line the model is read from.
check("edid synopsis", detect("""Display 1
   I2C bus:  /dev/i2c-7
   EDID synopsis:
      Mfg id:    GSM
      Model:     WRONG VALUE
   Monitor:  GSM:LG ULTRAWIDE:0001
"""), [{"bus": "7", "model": "LG ULTRAWIDE", "connector": ""}])

# A bare path is still accepted, so the fix is a widening and not a swap.
check("bare path", detect("""Display 1
   /dev/i2c-4
   Monitor:  DEL:DELL U2415:7MT
"""), [{"bus": "4", "model": "DELL U2415", "connector": ""}])

check("no displays", detect(""), [])

# An empty result must not be cached: the unit starts at boot under linger,
# possibly before the i2c group or the GPU driver is ready, and caching []
# there would keep SCREENS dark until someone restarted the service.
ma._buses = None
ma._last_error = None
ma._ddcutil = lambda args, timeout=10: ""
ma._detect()
ma._ddcutil = lambda args, timeout=10: """Display 1
   I2C bus:  /dev/i2c-3
   Monitor:  DEL:DELL:X
"""
if ma._detect() != [{"bus": "3", "model": "DELL", "connector": ""}]:
    failures.append("empty detection was cached; a later probe could not recover")

# An empty monitor list has to say why, or a blank SCREENS is undiagnosable.
ma._buses = None
ma._last_error = "ddcutil: permission denied opening /dev/i2c-4"
ma._ddcutil = lambda args, timeout=10: ""
payload = ma.monitors()
if payload.get("monitors") != []:
    failures.append(f"expected no monitors, got {payload}")
if "permission denied" not in (payload.get("reason") or ""):
    failures.append(f"empty monitor list lost its reason: {payload}")

# --- geometry: resolution, desktop order, and what happens without either ---

# DRM spells a connector "card1-HDMI-A-1" where xrandr says "HDMI-1", and
# amdgpu says "DisplayPort-1" where DRM says "DP-1". Same port either way.
for drm, server in [("card1-HDMI-A-1", "HDMI-1"), ("card1-DP-1", "DP-1"),
                    ("card1-DP-1", "DisplayPort-1"), ("card1-HDMI-A-1", "HDMI-A-1")]:
    if ma._conn_key(drm) != ma._conn_key(server):
        failures.append(f"connector {drm} did not match {server}")


class _Ran:
    def __init__(self, stdout):
        self.returncode, self.stdout, self.stderr = 0, stdout, ""


def monitors_with(xrandr, buses):
    ma._buses = [dict(b) for b in buses]
    ma._last_error = None
    ma._ddcutil = lambda args, timeout=10: "VCP 60 SNC x12"
    if xrandr is None:
        def boom(*a, **k):
            raise FileNotFoundError("xrandr")
        ma.subprocess.run = boom
    else:
        ma.subprocess.run = lambda *a, **k: _Ran(xrandr)
    try:
        return ma.monitors()["monitors"]
    finally:
        ma.subprocess.run = subprocess_run_real


import subprocess as _sp
subprocess_run_real = _sp.run

BUSES = [{"bus": "5", "model": "HP M32f FHD", "connector": "card1-HDMI-A-1"},
         {"bus": "6", "model": "HP M32f FHD", "connector": "card1-DP-1"}]

# ddcutil enumerates by I2C bus, which says nothing about how the desks are
# arranged. With DP physically on the left, the cards must come out DP first —
# while each card keeps the ddcutil ordinal, because POST /monitor selects by
# it and reordering the list must not redirect the buttons.
got = monitors_with("""DP-1 connected primary 1920x1080+0+0 (normal) 698mm x 392mm
HDMI-1 connected 1920x1080+1920+0 (normal) 698mm x 392mm
""", BUSES)
check("desktop order", [(m["index"], m["position"], m["x"]) for m in got],
      [(1, "LEFT", 0), (0, "RIGHT", 1920)])
if not got[0]["primary"] or got[1]["primary"]:
    failures.append(f"primary flag followed the wrong panel: {got}")

# Without a display server there is no honest answer for desktop position, so
# it must be omitted rather than invented — the panel renders what arrives.
got = monitors_with(None, BUSES)
for m in got:
    if "x" in m or m["position"] != "":
        failures.append(f"invented a desktop position with no display server: {m}")
check("order preserved without geometry", [m["index"] for m in got], [0, 1])


# The wlroots and sway paths, for compositors with no xrandr view. Same desk
# either way: DP on the left, HDMI on the right, so the ddcutil order reverses.
WLR = """HDMI-A-1 "HP Inc. HP M32f FHD 3CM30811V7"
  Enabled: yes
  Modes:
    1920x1080 px, 60.000000 Hz (preferred, current)
  Position: 1920,0
DP-1 "HP Inc. HP M32f FHD 3CM1171SHX"
  Enabled: yes
  Modes:
    1920x1080 px, 60.000000 Hz (preferred, current)
  Position: 0,0
"""
SWAY = json.dumps([
    {"name": "HDMI-A-1", "active": True,
     "rect": {"x": 1920, "y": 0, "width": 1920, "height": 1080}},
    {"name": "DP-1", "active": True, "primary": True,
     "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}}])


def monitors_from(tool, text, env=None):
    """Only `tool` answers; every other display-server query fails."""
    ma._buses = [dict(b) for b in BUSES]
    ma._last_error = None
    ma._ddcutil = lambda args, timeout=10: "VCP 60 SNC x12"
    ma.os.environ.pop("MON_ORDER", None)
    if env:
        ma.os.environ.update(env)

    def fake(cmd, **kw):
        class R:
            returncode, stdout, stderr = 1, "", ""
        if cmd[0] == tool:
            R.returncode, R.stdout = 0, text
        return R

    ma.subprocess.run = fake
    try:
        return ma.monitors()["monitors"]
    finally:
        ma.subprocess.run = subprocess_run_real
        ma.os.environ.pop("MON_ORDER", None)


for tool, text in [("wlr-randr", WLR), ("swaymsg", SWAY)]:
    got = monitors_from(tool, text)
    check(f"{tool} layout",
          [(m["index"], m["position"], m["x"]) for m in got],
          [(1, "LEFT", 0), (0, "RIGHT", 1920)])

# MON_ORDER is the last resort for compositors that will not say — GNOME on
# Wayland has no CLI for this. It must order the cards WITHOUT inventing an x:
# an ordinal is not a pixel offset, and the panel prints x as one.
got = monitors_from("nothing-answers", "", env={"MON_ORDER": "dp1,hdmi1"})
check("MON_ORDER labels", [(m["index"], m["position"]) for m in got],
      [(1, "LEFT"), (0, "RIGHT")])
for m in got:
    if "x" in m:
        failures.append(f"MON_ORDER leaked an ordinal as a pixel offset: {m}")

if failures:
    print("FAIL: media-agent ddcutil parsing", file=sys.stderr)
    for f in failures:
        print(f"  - {f}", file=sys.stderr)
    raise SystemExit(1)
print("media-agent ddcutil parsing tests passed")
PY
