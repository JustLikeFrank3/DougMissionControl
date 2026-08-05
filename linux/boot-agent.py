#!/usr/bin/env python3
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("FLIGHTSIM_BOOT_PORT", "9108"))
BOOT_CMD = "/usr/local/bin/boot-to-windows"


class BootHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # "/" included: the Pi's linux_up() liveness probe hits the root path.
        if self.path == "/" or self.path.startswith("/status"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"linux\n")
            return

        if self.path.startswith("/reboot"):
            subprocess.run([BOOT_CMD], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"rebooting\n")
            return

        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"not found\n")

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), BootHandler)
    server.serve_forever()
