#!/usr/bin/env python3
"""media-agent.py — now-playing under Linux, on :9110.

The Linux counterpart to the sim-agent's /media: deck-api polls whichever OS
is up, and both serve the SAME normalized shape, so the panel widget neither
knows nor cares which OS is playing. Reads MPRIS via playerctl, which covers
Spotify, browsers, VLC and anything else speaking the standard.

    GET /            -> 200 "media-agent"      (liveness, no token)
    GET /media?token= -> the shape below
    GET /media/art?token= -> jpeg/png bytes, 404 when the track has none
    POST /media/command?token=  {"action": "play_pause"|"next"|"prev"}

Token: first line of /etc/dualboot/media-agent.token (0600, root-owned is
fine — this runs as the desktop user via a systemd user unit). Same guard
rules as every other agent in this repo: wrong token 403, RFC1918 only.

UNTESTED as of writing: authored while the workstation was booted into
Windows. The next Linux session should run:
    python3 media-agent.py --check
which exercises playerctl parsing locally and reports.

Install (as the desktop user):
    sudo install -d /etc/dualboot
    uuidgen | tr -d - | sudo tee /etc/dualboot/media-agent.token
    sudo chmod 644 /etc/dualboot/media-agent.token
    systemctl --user enable --now path-to/media-agent.service
"""

from __future__ import annotations

import ipaddress
import json
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


def _ddcutil(args: list[str], timeout: float = 10) -> str | None:
    try:
        out = subprocess.run(["ddcutil", *args], capture_output=True, text=True,
                             timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _detect() -> list[dict]:
    """Displays ddcutil can reach, cached — detection probes every I2C bus."""
    global _buses
    if _buses is not None:
        return _buses
    found: list[dict] = []
    text = _ddcutil(["detect", "--brief"], timeout=25) or ""
    bus, model = None, ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("/dev/i2c-"):
            bus = s.rsplit("-", 1)[-1]
        elif s.startswith("Monitor:"):
            parts = s.split(":", 1)[1].split(":")
            model = parts[1].strip() if len(parts) > 1 else ""
        elif not s and bus is not None:
            found.append({"bus": bus, "model": model})
            bus, model = None, ""
    if bus is not None:
        found.append({"bus": bus, "model": model})
    _buses = found
    return found


def monitors() -> dict:
    """Same payload as the Windows agent's /monitor, minus desktop geometry:
    ddcutil sees I2C buses, not desktop rectangles, so position is left to
    the operator's ordering rather than invented."""
    out = []
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
        out.append({
            "index": i,
            "desc": disp["model"] or f"i2c-{disp['bus']}",
            "position": "",
            "ddc": raw is not None,
            "input_raw": raw,
            "input": name,
            "inputs": None,
        })
    return {"monitors": out}


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
