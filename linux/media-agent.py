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
import os
import re
import subprocess
import sys
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


def _ddcutil(args: list[str], timeout: float = 10) -> str | None:
    """Run ddcutil, keeping why it failed. Every caller here treats None as
    "no monitors", so without _last_error an empty SCREENS cannot tell a
    missing binary from a permission denial from a GPU that has no DDC."""
    global _last_error
    try:
        out = subprocess.run(["ddcutil", *args], capture_output=True, text=True,
                             timeout=timeout)
    except FileNotFoundError:
        _last_error = "ddcutil is not installed"
        return None
    except (OSError, subprocess.SubprocessError) as e:
        _last_error = f"ddcutil did not run: {e}"
        return None
    if out.returncode != 0:
        _last_error = (out.stderr or out.stdout or "").strip()[:400] \
            or f"ddcutil exited {out.returncode}"
        return None
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


def _layout() -> dict[str, dict]:
    """Desktop rectangles, keyed by _conn_key. Only the display server knows
    which panel is on the left, so without one there is no honest answer and
    the geometry is simply omitted — never guessed."""
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    try:
        out = subprocess.run(["xrandr", "--query"], capture_output=True,
                             text=True, timeout=5, env=env)
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    found = {}
    for line in out.stdout.splitlines():
        m = _XRANDR_RE.match(line.strip())
        if m:
            found[_conn_key(m.group(1))] = {
                "w": int(m.group(3)), "h": int(m.group(4)),
                "x": int(m.group(5)), "y": int(m.group(6)),
                "primary": bool(m.group(2)),
            }
    return found


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
            "inputs": None,
        }
        geo = layout.get(_conn_key(disp.get("connector") or ""))
        if geo:
            rec.update(geo)
        else:
            # No display server to ask, but DRM still knows the mode. Report
            # the resolution and leave x/primary out rather than inventing a
            # desktop position — the panel omits what is absent.
            mode = _sysfs_mode(disp.get("connector") or "")
            if mode:
                rec["w"], rec["h"] = mode
        out.append(rec)

    # Order the cards the way the monitors are actually arranged, so the
    # leftmost panel is the leftmost card. ddcutil enumerates by I2C bus, which
    # bears no relation to how the desks are laid out — that ordering is what
    # made the two HP panels come out reversed.
    if out and all("x" in m for m in out):
        out.sort(key=lambda m: (m["x"], m.get("y", 0)))
        for label, m in zip(_position_labels(len(out)), out):
            m["position"] = label

    if out:
        return {"monitors": out}
    # An empty list is the one answer SCREENS cannot act on and cannot explain,
    # so say why. deck-api passes this through untouched and the panel renders
    # the surface from "monitors" alone, so the extra key costs nothing there.
    return {"monitors": [], "reason": _last_error or "ddcutil reached no I2C bus"}


def monitor_switch(input_name: str, index: int) -> dict:
    """Command one monitor (or all when index < 0). "sent" means ddcutil
    returned success — the read-back is the only proof anything moved."""
    code = MON_INPUTS.get(input_name)
    if code is None:
        return {"ok": False, "reason": "input must be one of " + "/".join(MON_INPUTS)}
    results, any_ok = [], False
    for i, disp in enumerate(_detect()):
        if index >= 0 and i != index:
            continue
        sent = _ddcutil(["setvcp", "60", hex(code), "--bus", disp["bus"]]) is not None
        any_ok |= sent
        results.append({"index": i, "desc": disp["model"], "sent": sent})
    if not results:
        return {"ok": False, "reason": f"no monitor at index {index}"}
    return {"ok": any_ok, "input": input_name, "monitors": results}


def main() -> int:
    if "--check" in sys.argv:
        print(json.dumps(snapshot(), indent=2))
        return 0
    if "--monitors" in sys.argv:
        print(json.dumps(monitors(), indent=2))
        return 0
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
