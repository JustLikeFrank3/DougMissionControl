#!/usr/bin/env python3
"""douglas-switch: the owner's off-switches for what Douglas may use.

One file, a line a tool: `search=off`. The voice pipeline reads it when a
turn begins and offers him nothing that is off; it cannot write it. This
program is the one thing that writes it, and it does one thing: at a socket
that only douglas-agent's unit may open (the owner's screens reach the agent
through Mission Control) it takes "this tool, on or off", checks that the
tool is one it knows by name, and writes the file. It reads nothing else,
runs nothing, and has no network. So what Douglas may use can be taken away
from a screen, and nothing that is said to him or read by him can give it
back.

    douglas-switch.py serve     at the socket systemd hands it
    douglas-switch.py show      the switches as they are

A question is one line of JSON:
    {"op": "show"}                               -> {"ok": true, "off": [...]}
    {"op": "set", "tool": "search", "on": false} -> the same, after the change

Settings (environment):
    SWITCHES_FILE   /var/lib/douglas-switch/capabilities
"""

from __future__ import annotations

import json
import os
import socket
import sys

FILE = os.environ.get("SWITCHES_FILE", "/var/lib/douglas-switch/capabilities")
# What can be switched: the names the pipeline knows its tools by. A name
# that is not here is not written down, whatever is asked.
TOOLS = ("weather", "news", "wikipedia", "read_page", "search", "reddit", "look", "draw", "edit",
         "jobcontext", "repo", "memory", "fleet", "self", "camera")


def log(msg: str) -> None:
    print(f"douglas-switch: {msg}", file=sys.stderr, flush=True)


def off_now() -> list[str]:
    """What the file says is off. No file: nothing is."""
    try:
        with open(FILE, encoding="utf-8") as f:
            lines = f.read(8192).splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        name, _, state = line.strip().partition("=")
        if name.strip() in TOOLS and state.strip() == "off" and name.strip() not in out:
            out.append(name.strip())
    return [t for t in TOOLS if t in out]


def write(off: list[str]) -> None:
    """The whole file, written beside itself and moved into place: whoever
    reads it sees the old one or the new one, never half of either."""
    body = ("# What the owner has switched off for Douglas, in Mission Control. Written by douglas-switch;\n"
            "# read by the voice pipeline when a turn begins. A line a tool. No line: it is on.\n"
            + "".join(f"{t}=off\n" for t in TOOLS if t in off))
    tmp = f"{FILE}.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, body.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o644)
    os.replace(tmp, FILE)


def answer(req) -> dict:
    if not isinstance(req, dict) or req.get("op") not in ("show", "set"):
        return {"ok": False, "text": "A question is one line of JSON: show, or set with a tool and on."}
    if req["op"] == "set":
        tool, on = req.get("tool"), req.get("on")
        if tool not in TOOLS:
            return {"ok": False, "text": "That is not something of his that can be switched."}
        if not isinstance(on, bool):
            return {"ok": False, "text": "on is true or false."}
        off = [t for t in off_now() if t != tool] + ([] if on else [tool])
        write(off)
        log(f"{tool} switched {'on' if on else 'off'}")
    return {"ok": True, "off": off_now()}


def serve() -> None:
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or int(os.environ.get("LISTEN_FDS") or 0) < 1:
        sys.exit("douglas-switch: no socket was handed over (it is started by douglas-switch.socket)")
    os.umask(0o022)                              # the file is for the pipeline to read
    srv = socket.socket(fileno=3)
    log("at the door, with " + (", ".join(off_now()) + " switched off" if off_now() else "nothing switched off"))
    while True:
        conn, _ = srv.accept()
        with conn:
            conn.settimeout(5)
            try:
                with conn.makefile("rb") as f:
                    line = f.readline(4096)
                reply = answer(json.loads(line or b"null"))
            except Exception as e:               # noqa: BLE001 - one bad question, not the service
                log(f"a question tripped it: {type(e).__name__}")
                reply = {"ok": False, "text": f"The switches tripped over that ({type(e).__name__})."}
            try:
                conn.sendall(json.dumps(reply).encode() + b"\n")
            except OSError:
                pass


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    if what == "serve":
        serve()
    elif what == "show":
        print(", ".join(off_now()) + " switched off" if off_now() else "nothing is switched off")
    else:
        sys.exit(__doc__.split("\n\n")[2])
