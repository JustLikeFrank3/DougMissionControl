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


def main() -> int:
    if "--check" in sys.argv:
        print(json.dumps(snapshot(), indent=2))
        return 0
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
