#!/usr/bin/env bash
# Pins linux/media-agent.py's ddcutil parsing against real `ddcutil detect
# --brief` layouts, with ddcutil stubbed — the parser is the half that failed
# silently in the field, because every error path collapses to an empty
# monitor list and SCREENS renders that identically to "no monitors here".
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 - "$repo_dir" <<'PY'
import importlib.util
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
"""), [{"bus": "4", "model": "DELL U2415"},
       {"bus": "5", "model": "VG270U"}])

# An EDID synopsis block interleaves "Model:" lines that must not be mistaken
# for the "Monitor:" line the model is read from.
check("edid synopsis", detect("""Display 1
   I2C bus:  /dev/i2c-7
   EDID synopsis:
      Mfg id:    GSM
      Model:     WRONG VALUE
   Monitor:  GSM:LG ULTRAWIDE:0001
"""), [{"bus": "7", "model": "LG ULTRAWIDE"}])

# A bare path is still accepted, so the fix is a widening and not a swap.
check("bare path", detect("""Display 1
   /dev/i2c-4
   Monitor:  DEL:DELL U2415:7MT
"""), [{"bus": "4", "model": "DELL U2415"}])

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
if ma._detect() != [{"bus": "3", "model": "DELL"}]:
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

if failures:
    print("FAIL: media-agent ddcutil parsing", file=sys.stderr)
    for f in failures:
        print(f"  - {f}", file=sys.stderr)
    raise SystemExit(1)
print("media-agent ddcutil parsing tests passed")
PY
