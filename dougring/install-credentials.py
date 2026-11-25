#!/usr/bin/env python3
"""Put the Ring app's credentials on node1 without them touching a command
line, a log, the screen or this conversation.

Run ON THE WORKSTATION:
    python3 -I dougring/install-credentials.py [/path/to/app-credentials.csv] [ssh-host]

Defaults: ~/Downloads/app-credentials.csv (Ring's "Download CSV": rows
"Client ID", "Client Secret", "HMAC Signature Key") and fvm3@node1.

It writes /etc/dougring/ring-app.env (0600 root) on node1 as
    RING_CLIENT_ID=...
    RING_CLIENT_SECRET=...
    RING_HMAC_KEY=...
sent over ssh on standard input; the remote command line holds only paths.
It prints lengths, never values. The CSV is read, never moved or changed.
"""
import csv
import os
import subprocess
import sys

src = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/Downloads/app-credentials.csv")
host = sys.argv[2] if len(sys.argv) > 2 else "fvm3@node1"
labels = {"client id": "RING_CLIENT_ID", "client secret": "RING_CLIENT_SECRET",
          "hmac signature key": "RING_HMAC_KEY"}

vals = {}
with open(src, newline="", encoding="utf-8-sig") as f:
    for row in csv.reader(f):
        if len(row) >= 2 and row[0].strip().lower() in labels:
            vals[labels[row[0].strip().lower()]] = row[1].strip()
missing = [v for v in labels.values() if not vals.get(v)]
if missing:
    sys.exit(f"the CSV lacks {', '.join(missing)}")
for k, v in vals.items():
    if any(c in v for c in "\r\n") or v != v.strip():
        sys.exit(f"{k} has a character this file format cannot hold")

body = "".join(f"{k}={vals[k]}\n" for k in ("RING_CLIENT_ID", "RING_CLIENT_SECRET", "RING_HMAC_KEY"))
remote = ("sudo install -d -m 700 -o root -g root /etc/dougring && "
          "sudo sh -c 'umask 077; cat > /etc/dougring/ring-app.env.new && "
          "chmod 600 /etc/dougring/ring-app.env.new && "
          "mv /etc/dougring/ring-app.env.new /etc/dougring/ring-app.env' && "
          "sudo stat -c '%a %U:%G %s bytes' /etc/dougring/ring-app.env")
r = subprocess.run(["ssh", host, remote], input=body.encode(), capture_output=True)
if r.returncode:
    sys.exit(f"ssh failed: {r.stderr.decode().strip()}")
print("installed /etc/dougring/ring-app.env on", host, "->", r.stdout.decode().strip())
print("lengths:", {k: len(v) for k, v in vals.items()})
