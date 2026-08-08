#!/usr/bin/env python3
"""media-agent.py — now-playing and monitor input switching under Linux, :9110.

The Linux counterpart to the sim-agent on the Windows boot: deck-api polls
whichever OS is up, and both serve the SAME normalized shapes, so the panel
neither knows nor cares which OS is playing the music or holding the video
cables. Now-playing reads MPRIS via playerctl (Spotify, browsers, VLC, anything
speaking the standard); monitor switching drives DDC/CI via ddcutil.

    GET  /             -> 200 "media-agent"     (liveness, no token)
    GET  /media?token=       -> the now-playing shape
    GET  /media/art?token=   -> jpeg/png bytes, 404 when the track has none
    POST /media/command?token=  {"action": "play_pause"|"next"|"prev"}
    GET  /monitor?token=     -> {"monitors": [...]} — drives SCREENS
    POST /monitor?token=     {"input": "hdmi1", "index": n}  (index < 0 = all)

Token: first line of /etc/dualboot/media-agent.token, owned by the desktop
user at 0600 — this runs as that user, so root-owned would not be readable.
Same guard rules as every other agent in this repo: wrong token 403, RFC1918
only.

Installed by linux/setup.sh as a systemd USER unit
(flightsim-media-agent.service), which is forced rather than chosen: playerctl
needs the user's session bus, which a system unit cannot reach. DDC then needs
/dev/i2c-*, so setup.sh also installs ddcutil, loads i2c-dev, and puts the
desktop user in the i2c group.

Monitor cards are ordered left-to-right by desktop geometry, asked of xrandr,
then wlr-randr, then swaymsg. A compositor that answers none of them (GNOME on
Wayland has no CLI for this) leaves the order to MON_ORDER in
/etc/dualboot/media-agent.env — an operator statement about the desk. Each card
keeps its ddcutil ordinal as "index" through the sort, because POST /monitor
selects by it.

Verify after install — DDC is the half that fails silently, because every
ddcutil error path here collapses to "no monitors found":
    python3 media-agent.py --check      # playerctl parsing, now-playing
    python3 media-agent.py --monitors   # ddcutil parsing, expect "ddc": true
An empty --monitors list means ddcutil reached no bus. Group membership does
not apply to already-open sessions, so log out and back in before believing it.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from hashlib import md5
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 9110
TOKEN_FILE = Path("/etc/dualboot/media-agent.token")


def token() -> str:
    try:
        return TOKEN_FILE.read_text().splitlines()[0].strip()
    except (OSError, IndexError):
        return ""


def playerctl(*args: str) -> str | None:
    try:
        out = subprocess.run(["playerctl", *args], capture_output=True,
                             text=True, timeout=3)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def snapshot() -> dict:
    """The same shape the Windows agent serves — keep them in lockstep."""
    status = playerctl("status")
    if status not in ("Playing", "Paused"):
        return {"active": False}

    def meta(key: str) -> str:
        return playerctl("metadata", key) or ""

    title, artist, album = meta("title"), meta("artist"), meta("album")
    art_url = meta("mpris:artUrl")
    art_id = md5(f"{title}{artist}{album}".encode()).hexdigest()[:12]

    def secs(v: str | None, scale: float = 1.0) -> int:
        try:
            return int(float(v) * scale)
        except (TypeError, ValueError):
            return 0

    return {
        "active": True,
        "playing": status == "Playing",
        "title": title, "artist": artist, "album": album,
        "app": playerctl("--list-all") or "",
        "position_s": secs(playerctl("position")),
        # mpris:length is microseconds
        "duration_s": secs(meta("mpris:length"), 1e-6),
        "art_id": art_id,
        "art_url": art_url,   # consumed by /media/art, not by clients
        "can": {"play_pause": True, "next": True, "prev": True},
    }


def art_bytes() -> bytes | None:
    url = snapshot().get("art_url") or ""
    if url.startswith("file://"):
        try:
            return Path(urllib.parse.unquote(url[7:])).read_bytes()
        except OSError:
            return None
    return None   # http art urls are the player's business, not ours


class Handler(BaseHTTPRequestHandler):
    server_version = "media-agent/0.1"

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _lan(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_private
        except ValueError:
            return False

    def _authed(self, q: dict) -> bool:
        t = token()
        return bool(t) and (q.get("token") or [""])[0] == t

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if not self._lan():
            return self._json(403, {"error": "forbidden"})
        if url.path == "/monitor" and self._authed(q):
            return self._json(200, monitors())
        if url.path == "/":
            body = b"media-agent\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._authed(q):
            return self._json(403, {"error": "forbidden"})
        if url.path == "/audio":
            return self._json(200, audio())
        if url.path == "/media":
            snap = snapshot()
            snap.pop("art_url", None)
            return self._json(200, snap)
        if url.path == "/media/art":
            art = art_bytes()
            if not art:
                return self._json(404, {"error": "no art"})
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(art)))
            self.end_headers()
            self.wfile.write(art)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if not self._lan() or not self._authed(q):
            return self._json(403, {"error": "forbidden"})
        if url.path == "/monitor":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, OSError):
                return self._json(400, {"ok": False, "reason": "bad body"})
            idx = body.get("index")
            return self._json(200, monitor_switch(
                str(body.get("input") or ""),
                idx if isinstance(idx, int) else -1))
        if url.path == "/media/command":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, OSError):
                return self._json(400, {"sent": False})
            action = {"play_pause": "play-pause", "next": "next",
                      "prev": "previous"}.get(str(body.get("action")))
            if action is None:
                return self._json(400, {"sent": False, "reason": "unknown action"})
            ok = playerctl(action) is not None
            return self._json(200, {"sent": ok})
        self._json(404, {"error": "not found"})


# ── monitors (DDC/CI via ddcutil) ─────────────────────────────────────────
# The Linux counterpart to the sim-agent's DdcBridge, publishing the SAME
# shape so deck-api and the SCREENS surface never learn which OS answered.
# ddcutil talks to the same VCP 0x60 the Windows side does.
#
# `ddcutil detect` is slow (it probes every I2C bus), so the bus list is
# discovered once and cached; only the per-monitor input read repeats.

MON_INPUTS = {"vga": 0x01, "dp1": 0x0F, "dp2": 0x10, "hdmi1": 0x11, "hdmi2": 0x12}
_buses: list[dict] | None = None
_last_error: str | None = None

# What each monitor DECLARES it has, cached, and never probed on the request
# path. `ddcutil capabilities` is the slow call: the HP 32f refuses it outright
# and takes about a minute to say so, which hangs /monitor and takes the whole
# SCREENS surface down with it. So the probe runs once per bus on its own
# thread and callers are served from cache; until it lands the monitor reports
# None and the panel falls back to deck-api's stated default. A panel's input
# list cannot change, so one attempt is enough. This mirrors
# DdcBridge.CachedInputs on the Windows side deliberately — the two agents
# publish the same shape, and they should reach it the same way.
_caps: dict[str, list[str] | None] = {}
_caps_asked: set[str] = set()
_caps_lock = threading.Lock()

# ddcutil emits the input list two different ways depending on version and on
# whether --brief was honoured. The raw MCCS string carries "60(01 11 12)"
# inline; the pretty-printed form has a "Feature: 60" heading with one indented
# "11: HDMI-1" per value beneath it. Parse either rather than pinning a version
# and finding out on the workstation.
_CAPS_INLINE_RE = re.compile(r"(?<![0-9A-Fa-f])60\s*\(([0-9A-Fa-f ]*)\)")
_CAPS_FEATURE_RE = re.compile(r"Feature:\s*60\b")
_CAPS_VALUE_RE = re.compile(r"^\s+([0-9A-Fa-f]{2}):")


def _parse_caps_inputs(text: str) -> list[str] | None:
    """Input names a capabilities reply declares, or None if it names none."""
    codes: list[int] = []
    inline = _CAPS_INLINE_RE.search(text)
    if inline:
        codes = [int(t, 16) for t in inline.group(1).split()]
    else:
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if not _CAPS_FEATURE_RE.search(line):
                continue
            for nxt in lines[i + 1:]:
                # The values are indented under the feature. A line back at the
                # margin, or the next Feature:, ends this one — without that
                # the input list would swallow every value of every feature
                # below it and offer buttons for colour presets.
                if nxt.strip() and not nxt[0].isspace():
                    break
                if "Feature:" in nxt:
                    break
                found = _CAPS_VALUE_RE.match(nxt)
                if found:
                    codes.append(int(found.group(1), 16))
            break
    names = [name for code in codes
             for name, value in MON_INPUTS.items() if value == code]
    return names or None


def _declared_inputs(bus: str) -> list[str] | None:
    text = _ddcutil(["capabilities", "--bus", bus, "--brief"],
                    timeout=25, quiet=True)
    return _parse_caps_inputs(text) if text else None


def _cached_inputs(bus: str) -> list[str] | None:
    """This monitor's declared inputs if we have them, else None right now and
    a background probe so the next poll can do better."""
    with _caps_lock:
        if bus in _caps:
            return _caps[bus]
        if bus in _caps_asked:
            return None                       # already in flight
        _caps_asked.add(bus)

    def probe() -> None:
        got = _declared_inputs(bus)
        with _caps_lock:
            _caps[bus] = got

    threading.Thread(target=probe, daemon=True, name="ddc-caps").start()
    return None


def _ddcutil(args: list[str], timeout: float = 10,
             quiet: bool = False) -> str | None:
    """Run ddcutil, keeping why it failed. Every caller here treats None as
    "no monitors", so without _last_error an empty SCREENS cannot tell a
    missing binary from a permission denial from a GPU that has no DDC.

    `quiet` suppresses that bookkeeping, for calls whose failure is not a
    fault: a capabilities probe that a monitor refuses is ordinary, and
    letting it write _last_error would have SCREENS report ddcutil as broken
    while it was busy working perfectly for every other call."""
    global _last_error

    def fail(msg: str) -> None:
        global _last_error
        if not quiet:
            _last_error = msg

    try:
        out = subprocess.run(["ddcutil", *args], capture_output=True, text=True,
                             timeout=timeout)
    except FileNotFoundError:
        fail("ddcutil is not installed")
        return None
    except (OSError, subprocess.SubprocessError) as e:
        fail(f"ddcutil did not run: {e}")
        return None
    if out.returncode != 0:
        fail((out.stderr or out.stdout or "").strip()[:400]
             or f"ddcutil exited {out.returncode}")
        return None
    if not quiet:
        _last_error = None
    return out.stdout


# "I2C bus:  /dev/i2c-4" — the path sits mid-line, so anchoring a match to the
# start of the line finds nothing and yields an empty monitor list on a machine
# where DDC works perfectly. Match the path wherever it appears.
_BUS_RE = re.compile(r"/dev/i2c-(\d+)")


def _detect() -> list[dict]:
    """Displays ddcutil can reach, cached — detection probes every I2C bus."""
    global _buses
    if _buses:
        return _buses
    found: list[dict] = []
    text = _ddcutil(["detect", "--brief"], timeout=25) or ""
    bus, model, conn = None, "", ""

    def flush() -> None:
        nonlocal bus, model, conn
        if bus is not None:
            found.append({"bus": bus, "model": model, "connector": conn})
        bus, model, conn = None, "", ""

    for line in text.splitlines():
        s = line.strip()
        # Records start at "Display N" and are blank-line separated; keying on
        # both survives either layout. An "Invalid display" block still names a
        # bus, so it is kept and simply reads back no VCP value — SCREENS shows
        # it as a monitor with ddc false, which is truer than hiding it.
        if s.startswith("Display ") or s.startswith("Invalid display"):
            flush()
            continue
        if not s:
            flush()
            continue
        m = _BUS_RE.search(s)
        if m:
            bus = m.group(1)
        elif s.startswith("DRM connector:"):
            # "card1-HDMI-A-1" — the only link between an I2C bus and a thing
            # the display server has an opinion about, so resolution and
            # desktop position hang off this.
            conn = s.split(":", 1)[1].strip()
        elif s.startswith("Monitor:"):
            parts = s.split(":", 1)[1].split(":")
            model = parts[1].strip() if len(parts) > 1 else ""
    flush()
    # Deliberately not caching an empty result. The unit starts at boot under
    # linger, which can be before the i2c group or the GPU driver is ready —
    # caching [] there would keep SCREENS dark until someone restarted the
    # service, long after the machine had become perfectly capable.
    _buses = found
    return found


def _conn_key(name: str) -> str:
    """Normalize a connector name so ddcutil's DRM spelling and the display
    server's meet. DRM says "card1-HDMI-A-1", xrandr says "HDMI-1" for the
    same port, and amdgpu says "DisplayPort-0" where DRM says "DP-1" — so
    reduce both to protocol + trailing number."""
    s = re.sub(r"^card\d+-", "", name).strip().lower()
    m = re.match(r"([a-z]+)", s)
    proto = m.group(1) if m else s
    proto = {"displayport": "dp"}.get(proto, proto)
    nums = re.findall(r"\d+", s)
    return proto + (nums[-1] if nums else "")


def _sysfs_mode(connector: str) -> tuple[int, int] | None:
    """Resolution straight from DRM. Needs no display server and no session,
    which matters because this unit lingers and may run before either."""
    if not connector:
        return None
    try:
        first = Path(f"/sys/class/drm/{connector}/modes").read_text().split("\n")[0]
    except OSError:
        return None
    m = re.match(r"(\d+)x(\d+)", first.strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


_XRANDR_RE = re.compile(
    r"^(\S+)\s+connected\s+(primary\s+)?(\d+)x(\d+)\+(\d+)\+(\d+)")


def _run_env(cmd: list[str]) -> str | None:
    """Run a display-server query with a session environment guessed in. The
    unit lingers, so it may have started with no session at all and picked up
    none of these — which is survivable, since every caller treats an empty
    answer as "geometry unknown"."""
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=5, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _layout_xrandr() -> dict[str, dict]:
    text = _run_env(["xrandr", "--query"])
    if not text:
        return {}
    found = {}
    for line in text.splitlines():
        m = _XRANDR_RE.match(line.strip())
        if m:
            found[_conn_key(m.group(1))] = {
                "w": int(m.group(3)), "h": int(m.group(4)),
                "x": int(m.group(5)), "y": int(m.group(6)),
                "primary": bool(m.group(2)),
                # xrandr's own spelling of the output, kept so the layout can
                # be handed straight back to xrandr --output. The normalized
                # key cannot be: "hdmi1" is not an output name.
                "_out": m.group(1),
            }
    return found


def _layout_wlr() -> dict[str, dict]:
    """wlr-randr, for wlroots compositors (sway, labwc, Hyprland). Connector
    names come out bare — "HDMI-A-1" — and the rectangle is spread over
    "Position:" plus whichever mode is marked current."""
    text = _run_env(["wlr-randr"])
    if not text:
        return {}
    found: dict[str, dict] = {}
    name = None
    for raw in text.splitlines():
        if raw and not raw[0].isspace():
            name = _conn_key(raw.split()[0])
            found[name] = {}
            continue
        if name is None:
            continue
        s = raw.strip()
        m = re.match(r"Position:\s*(-?\d+),\s*(-?\d+)", s)
        if m:
            found[name]["x"], found[name]["y"] = int(m.group(1)), int(m.group(2))
            continue
        m = re.match(r"(\d+)x(\d+)\s+px.*current", s)
        if m:
            found[name]["w"], found[name]["h"] = int(m.group(1)), int(m.group(2))
    return {k: v for k, v in found.items() if "x" in v}


def _layout_sway() -> dict[str, dict]:
    text = _run_env(["swaymsg", "-t", "get_outputs", "-r"])
    if not text:
        return {}
    try:
        outs = json.loads(text)
    except ValueError:
        return {}
    found = {}
    for o in outs:
        r = o.get("rect") or {}
        if not o.get("active") or "x" not in r:
            continue
        found[_conn_key(str(o.get("name") or ""))] = {
            "w": r.get("width"), "h": r.get("height"),
            "x": r.get("x"), "y": r.get("y"),
            "primary": bool(o.get("primary")),
        }
    return found


def _layout_configured() -> dict[str, dict]:
    """Operator-stated left-to-right order, for the compositors that will not
    say. MON_ORDER=hdmi1,dp1 in the agent's environment names the connectors
    across the desk; the x values it invents are ordinals, not pixels, and
    exist only to sort by. Resolution still comes from DRM, so nothing here
    fabricates anything the panel presents as measured."""
    raw = os.environ.get("MON_ORDER", "")
    keys = [_conn_key(s) for s in raw.split(",") if s.strip()]
    # _order sorts; it is deliberately not "x", because an ordinal is not a
    # pixel offset and the panel would have printed it as one.
    return {k: {"_order": i} for i, k in enumerate(keys)}


def _layout() -> dict[str, dict]:
    """Desktop rectangles, keyed by _conn_key. Only the display server knows
    which panel is on the left, so where none will answer the geometry is
    omitted rather than guessed — unless the operator has stated the order,
    which is a fact rather than a guess."""
    for source in (_layout_xrandr, _layout_wlr, _layout_sway, _layout_configured):
        found = source()
        if found:
            return found
    return {}


def _position_labels(count: int) -> list[str]:
    if count == 1:
        return [""]
    if count == 2:
        return ["LEFT", "RIGHT"]
    if count == 3:
        return ["LEFT", "CENTER", "RIGHT"]
    return [str(i + 1) for i in range(count)]


def monitors() -> dict:
    """Same payload as the Windows agent's /monitor.

    Geometry is best-effort and every field is optional: DRM gives resolution
    without a session, but only a display server knows which panel sits on the
    left, and under a bare Wayland compositor there may be no way to ask. The
    panel renders whatever arrives and omits the rest, so a missing rectangle
    costs a label, never the input buttons.
    """
    out = []
    layout = _layout()
    for i, disp in enumerate(_detect()):
        raw, name = None, None
        text = _ddcutil(["getvcp", "60", "--bus", disp["bus"], "--brief"]) or ""
        # brief form: "VCP 60 SNC x11"
        parts = text.split()
        if len(parts) >= 4 and parts[-1].startswith("x"):
            try:
                raw = int(parts[-1][1:], 16)
                name = next((k for k, v in MON_INPUTS.items() if v == raw), "other")
            except ValueError:
                raw = None
        rec = {
            # The ddcutil ordinal, NOT this monitor's place in the sorted list:
            # POST /monitor selects by it, so it has to survive the reordering
            # below or the buttons would drive the wrong panel.
            "index": i,
            "desc": disp["model"] or f"i2c-{disp['bus']}",
            "position": "",
            "ddc": raw is not None,
            "input_raw": raw,
            "input": name,
            # Cached/background — never blocks this response. See _cached_inputs.
            "inputs": _cached_inputs(disp["bus"]),
        }
        conn = disp.get("connector") or ""
        # DRM knows the mode with no display server and no session, so
        # resolution is reported even when nothing will say where a panel sits.
        mode = _sysfs_mode(conn)
        if mode:
            rec["w"], rec["h"] = mode
        geo = dict(layout.get(_conn_key(conn)) or {})
        order = geo.pop("_order", None)
        geo.pop("_out", None)      # xrandr's output name is ours, not the panel's
        for k, v in geo.items():
            if v is not None:
                rec[k] = v
        # Sort by real desktop x where a compositor gave one, else by the
        # operator's stated order. Kept off the payload either way.
        rec["_order"] = order if order is not None else rec.get("x")
        out.append(rec)

    # Order the cards the way the monitors are actually arranged, so the
    # leftmost panel is the leftmost card. ddcutil enumerates by I2C bus, which
    # bears no relation to how the desks are laid out — that ordering is what
    # made the two HP panels come out reversed.
    if out and all(m["_order"] is not None for m in out):
        out.sort(key=lambda m: (m["_order"], m.get("y", 0)))
        for label, m in zip(_position_labels(len(out)), out):
            m["position"] = label
    for m in out:
        m.pop("_order", None)

    if out:
        return {"monitors": out}
    # An empty list is the one answer SCREENS cannot act on and cannot explain,
    # so say why. deck-api passes this through untouched and the panel renders
    # the surface from "monitors" alone, so the extra key costs nothing there.
    return {"monitors": [], "reason": _last_error or "ddcutil reached no I2C bus"}


# How long to wait before checking whether the desktop survived a switch.
# The monitor has to drop its input, settle, and re-assert hotplug; the display
# server then reconfigures. Under about 4 s and the check races that.
_RESTORE_AFTER_S = 6.0
# Off with MEDIA_NO_RESTORE=1 in /etc/dualboot/media-agent.env, for a desk whose
# monitors keep hotplug asserted on the inactive input — there the re-apply is
# a no-op, but a no-op that costs an xrandr call per switch.
RESTORE_LAYOUT = os.environ.get("MEDIA_NO_RESTORE", "") not in ("1", "true", "yes")


def _restore_layout(before: dict[str, dict]) -> None:
    """Put the desktop back the way it was before an input switch.

    Sending a monitor to another input usually drops its hotplug line. The
    display server sees the output vanish, tears it down, and when the monitor
    returns the driver brings it back at whatever mode it can prove is safe —
    1024x768, typically, on a panel whose EDID it no longer trusts. That is why
    switching away and back leaves a black screen or a 4:3 desktop.

    None of this is DDC's doing and none of it is DDC's to fix; the switch
    worked. But the agent is the only thing that knows a switch just happened,
    so it is the only thing positioned to put the mode back.
    """
    time.sleep(_RESTORE_AFTER_S)
    after = _layout()
    if not before or not after:
        return                      # no display server either side: nothing to do
    for key, was in before.items():
        now = after.get(key)
        out = was.get("_out")
        if not now or not out:
            continue
        same = (now.get("w"), now.get("h"), now.get("x"), now.get("y")) == \
               (was.get("w"), was.get("h"), was.get("x"), was.get("y"))
        if same:
            continue
        _run_env(["xrandr", "--output", out,
                  "--mode", f"{was['w']}x{was['h']}",
                  "--pos", f"{was['x']}x{was['y']}"])


def monitor_switch(input_name: str, index: int) -> dict:
    """Command one monitor (or all when index < 0). "sent" means ddcutil
    returned success — the read-back is the only proof anything moved."""
    code = MON_INPUTS.get(input_name)
    if code is None:
        return {"ok": False, "reason": "input must be one of " + "/".join(MON_INPUTS)}
    # Captured BEFORE the switch: once the monitor leaves this input the
    # display server has already forgotten what the mode was.
    before = _layout() if RESTORE_LAYOUT else {}
    results, any_ok = [], False
    for i, disp in enumerate(_detect()):
        if index >= 0 and i != index:
            continue
        sent = _ddcutil(["setvcp", "60", hex(code), "--bus", disp["bus"]]) is not None
        any_ok |= sent
        results.append({"index": i, "desc": disp["model"], "sent": sent})
    if not results:
        return {"ok": False, "reason": f"no monitor at index {index}"}
    # In the background: the panel gets its answer immediately, and the reply
    # still means only "the DDC write returned success". Whether the desktop
    # came back is a separate question from whether the input changed.
    if any_ok and before:
        threading.Thread(target=_restore_layout, args=(before,),
                         daemon=True).start()
    return {"ok": any_ok, "input": input_name, "monitors": results}


# ── audio spectrum (PipeWire/PulseAudio monitor capture) ──────────────────
# The Linux twin of the sim agent's AudioBridge, publishing the SAME shape so
# the panel's AUDIO surface never learns which OS measured the sound. The
# workstation is always in one boot or the other, and the visualiser was
# Windows-only for exactly as long as nobody rebooted.
#
# parec on the default MONITOR source: that is the loopback of whatever is
# being played, so it needs no virtual cable and changes nothing about what
# reaches the interface. @DEFAULT_MONITOR@ follows the default sink, which is
# what you want on a desk where the sink changes when a monitor with speakers
# is plugged in.
#
# The bands are real. Silence is a flat spectrum because silence IS a flat
# spectrum; there is no idle animation and no fallback pattern, for the same
# reason SIM never draws a commanded gear position.

AUDIO_BANDS = 64
_AUDIO_FFT = 4096          # 11.7 Hz per bin at 48 kHz - see AudioBridge.cs
_AUDIO_RATE = 48000
_AUDIO_PUBLISH = 0.05      # 20 Hz

_audio_lock = threading.Lock()
_audio_bands = [0.0] * AUDIO_BANDS
_audio_peak = 0.0
_audio_active = False
_audio_why = "not started"

try:
    import numpy as _np
except ImportError:                     # noqa: BLE001 - absence is a fact, not a fault
    _np = None


def _audio_fail(why: str) -> None:
    global _audio_active, _audio_why, _audio_peak
    with _audio_lock:
        _audio_active = False
        _audio_why = why
        _audio_peak = 0.0
        for i in range(AUDIO_BANDS):
            _audio_bands[i] = 0.0


def _audio_edges() -> list:
    """Fractional FFT-bin edges for each band, log-spaced 30 Hz to 16 kHz.

    Linear spacing would put four fifths of the bars above 5 kHz, where music
    has almost nothing, and the bass would be one bar wide.
    """
    hz_per_bin = _AUDIO_RATE / _AUDIO_FFT
    lo, hi = 30.0, min(16000.0, _AUDIO_RATE / 2 - hz_per_bin)
    return [(lo * (hi / lo) ** (b / AUDIO_BANDS)) / hz_per_bin
            for b in range(AUDIO_BANDS + 1)]


def _audio_map(mag) -> list:
    """Bin magnitudes onto the bars, matching AudioBridge.MapBands exactly.

    A band narrower than one bin INTERPOLATES between its neighbours rather
    than clamping to one of them. Clamping is what made the first nine bands
    on the Windows side carry an identical number, so the whole bottom of the
    spectrum moved as one block.
    """
    edges = _audio_edges()
    bins = len(mag)
    out = []
    for b in range(AUDIO_BANDS):
        x0, x1 = edges[b], edges[b + 1]
        i0 = max(1, min(int(math.ceil(x0)), bins - 1))
        i1 = max(0, min(int(math.floor(x1)), bins - 1))
        if i1 >= i0:
            v = max(mag[i0:i1 + 1])
        else:
            x = min(max((x0 + x1) / 2.0, 1), bins - 2)
            i = int(x)
            f = x - i
            v = mag[i] * (1 - f) + mag[i + 1] * f
        db = 20 * math.log10(v + 1e-9)
        out.append(max(0.0, min(1.0, (db + 60) / 60.0)))
    return out


def _audio_pump() -> None:
    global _audio_active, _audio_why, _audio_peak
    window = _np.zeros(_AUDIO_FFT, dtype=_np.float32) if _np is not None else None
    hann = _np.hanning(_AUDIO_FFT).astype(_np.float32) if _np is not None else None

    while True:
        if _np is None:
            # A pure-Python 4096-point FFT at 20 Hz is several seconds of CPU
            # per second of audio. Saying so beats shipping a visualiser that
            # pins a core and still stutters.
            _audio_fail("numpy is not installed (pip install numpy)")
            time.sleep(30)
            continue
        proc = None
        try:
            proc = subprocess.Popen(
                ["parec", "--format=float32le", f"--rate={_AUDIO_RATE}",
                 "--channels=2", "--latency-msec=50", "-d", "@DEFAULT_MONITOR@"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            with _audio_lock:
                _audio_active, _audio_why = True, ""
            chunk = 2048 * 2 * 4          # frames * channels * float32
            last = 0.0
            while True:
                raw = proc.stdout.read(chunk)
                if not raw:
                    raise OSError("parec stopped")
                # Mono-mix: a spectrum is about content, not stereo image, and
                # one set of bars cannot honestly show two channels.
                samples = _np.frombuffer(raw, dtype=_np.float32)
                if samples.size % 2:
                    samples = samples[:-1]
                mono = samples.reshape(-1, 2).mean(axis=1)
                if mono.size >= _AUDIO_FFT:
                    window[:] = mono[-_AUDIO_FFT:]
                else:
                    window[:-mono.size] = window[mono.size:]
                    window[-mono.size:] = mono

                now = time.monotonic()
                if now - last < _AUDIO_PUBLISH:
                    continue
                last = now
                spec = _np.abs(_np.fft.rfft(window * hann)) / (_AUDIO_FFT / 2)
                bands = _audio_map(spec.tolist())
                with _audio_lock:
                    for i, v in enumerate(bands):
                        # Attack fast, release slow. A spectrum that falls as
                        # fast as it rises reads as flicker.
                        _audio_bands[i] = v if v > _audio_bands[i] \
                            else _audio_bands[i] * 0.75 + v * 0.25
                    _audio_peak = max(bands)
        except FileNotFoundError:
            _audio_fail("parec is not installed (pulseaudio-utils)")
            time.sleep(30)
        except Exception as e:                      # noqa: BLE001
            _audio_fail(f"{type(e).__name__}: {e}")
            time.sleep(3)
        finally:
            if proc is not None:
                proc.kill()


def audio() -> dict:
    """The current spectrum, in the sim agent's /audio shape."""
    with _audio_lock:
        return {"active": _audio_active,
                "peak": round(_audio_peak, 4),
                "bands": [round(v, 4) for v in _audio_bands],
                "reason": _audio_why or None}


def main() -> int:
    if "--check" in sys.argv:
        print(json.dumps(snapshot(), indent=2))
        return 0
    if "--audio" in sys.argv:
        threading.Thread(target=_audio_pump, daemon=True, name="audio").start()
        time.sleep(2)
        print(json.dumps(audio(), indent=2))
        return 0
    if "--monitors" in sys.argv:
        print(json.dumps(monitors(), indent=2))
        return 0
    threading.Thread(target=_audio_pump, daemon=True, name="audio").start()
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
