#!/usr/bin/env python3
# boot-agent.py — tiny LAN control endpoint on :9108 (Linux), the mirror of
# windows/boot-agent.ps1: lets the Pi reboot this machine into Windows
# without depending on the ssh forced-command key.
#
#   GET /status                  -> 200 "linux"
#   GET /reboot?token=<token>    -> 200 "rebooting", then boot-to-windows
#
# Token: first line of /etc/flightsim/boot-agent.token (created by
# linux/setup.sh, mirrored into /etc/flightsim/boot.env on the Pi as
# LINUX_AGENT_TOKEN). A missing or empty token file refuses /reboot
# outright — a half-finished install must never leave an unauthenticated
# reboot endpoint listening on the LAN.
#
# Like the Windows agent, this is a hand-rolled listener on a LAN port:
# fine behind a home router, not something to expose to the internet.
import hmac
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("FLIGHTSIM_BOOT_PORT", "9108"))
# Overridable so tests can exercise the auth matrix without rebooting.
BOOT_CMD = os.environ.get("FLIGHTSIM_BOOT_CMD", "/usr/local/bin/boot-to-windows")
TOKEN_FILE = os.environ.get("FLIGHTSIM_TOKEN_FILE", "/etc/flightsim/boot-agent.token")


def read_token():
    """Re-read per request so a rotated token takes effect without a restart."""
    try:
        with open(TOKEN_FILE) as fh:
            return fh.readline().strip()
    except OSError:
        return ""


class BootHandler(BaseHTTPRequestHandler):
    server_version = "flightsim-boot-agent"

    def _respond(self, code, body):
        payload = (body + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urlparse(self.path)

        # "/" included: the Pi's liveness probe may hit the root path. Reads
        # only, so both stay unauthenticated.
        if parsed.path in ("/", "/status"):
            self._respond(200, "linux")
            return

        if parsed.path == "/reboot":
            token = read_token()
            supplied = parse_qs(parsed.query).get("token", [""])[0]
            if not token or not hmac.compare_digest(supplied, token):
                self._respond(403, "forbidden")
                return
            # Answer before rebooting: boot-to-windows calls `systemctl
            # reboot -i`, which can take the machine down before a response
            # written afterwards ever reaches the caller.
            self._respond(200, "rebooting")
            try:
                self.wfile.flush()
            except OSError:
                pass
            subprocess.Popen(
                [BOOT_CMD], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return

        self._respond(404, "not found")

    def log_message(self, fmt, *args):
        # Onto stderr -> journald. Rejected reboot attempts are worth seeing.
        print("boot-agent: %s - %s" % (self.address_string(), fmt % args), flush=True)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", PORT), BootHandler).serve_forever()
