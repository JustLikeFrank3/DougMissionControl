#!/usr/bin/env bash
# douglas/mcp-setup.sh — what Douglas remembers and sees, for the owner's
# other programs to read: an MCP server with six readings and nothing else.
#
# Run ON DOUGLAS, from a checkout:   sudo ./douglas/mcp-setup.sh
#                                    sudo ./douglas/mcp-setup.sh show
#                                    sudo ./douglas/mcp-setup.sh off
#
# What it leaves behind:
#   user     douglas-mcp (the server: reads three things, writes nothing)
#   group    douglas-memory-reader (who may open the memory's door for
#            readers: this unit, and nothing else)
#   code     /opt/douglas/douglas-mcp.py
#   units    douglas-mcp.service, and douglas-memory-reader.socket: a third
#            door on douglas-memory, which is restarted to be handed it
#   config   /etc/douglas/mcp.env, root's alone: a token made here, and
#            where the server reads from (the fleet view and its read token
#            are copied from the voice pipeline's settings, if it has them)
# It needs the memory (memory-setup.sh) and the agent (setup.sh) first.
#
# The server answers on :9112/mcp, to this machine and the tailnet only, and
# only to whoever holds the token. Nobody does until the owner gives it to a
# program: `show` prints the address and the token for that, and is the one
# thing here that shows it. Whichever program is given it, and whoever is
# behind that program, then reads what Douglas remembers.
#
# `off` stops the server and takes its door and its token away. Idempotent.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
env=/etc/douglas/mcp.env
voice=/etc/douglas/voice.env
mode="${1:-on}"
case "$mode" in on|off|show) ;; *) echo "usage: $0 [show|off]"; exit 1 ;; esac
value() { sed -n "s/^$2=//p" "$1" 2>/dev/null | tail -n1; }

if [ "$mode" = show ]; then
    [ -f "$env" ] || { echo "there is no $env yet: run this without 'show' first"; exit 1; }
    port="$(value "$env" DOUGLAS_MCP_PORT)"
    echo "Address:  http://$(hostname):${port:-9112}/mcp   (from a machine on the tailnet)"
    echo "Header:   Authorization: Bearer $(value "$env" DOUGLAS_MCP_TOKEN)"
    echo "Whatever is given this reads what Douglas remembers. '$0 off' takes it away from all of them."
    exit 0
fi

# The memory restarted with or without its third door. A door cannot be made
# or taken away while the service behind it is up.
memory_with() {
    systemctl stop douglas-memory.service 2>/dev/null || true
    if [ "$1" = reader ]; then
        systemctl enable douglas-memory-reader.socket >/dev/null 2>&1
        systemctl restart douglas-memory-reader.socket
    else
        systemctl disable --now douglas-memory-reader.socket 2>/dev/null || true
    fi
    systemctl start douglas-memory.service
}

if [ "$mode" = off ]; then
    systemctl disable --now douglas-mcp.service 2>/dev/null || true
    rm -f /etc/systemd/system/douglas-mcp.service
    if [ -f /etc/systemd/system/douglas-memory-reader.socket ]; then
        memory_with nothing
        rm -f /etc/systemd/system/douglas-memory-reader.socket
    fi
    rm -f "$env"
    systemctl daemon-reload
    echo "douglas-mcp is off: its door on the memory is gone, and so is its token."
    exit 0
fi

[ -f /etc/systemd/system/douglas-memory.service ] && grep -q '"reader"' /opt/douglas/douglas-memory.py 2>/dev/null \
    || { echo "the memory installed here has no door for readers: run memory-setup.sh from this checkout first"; exit 1; }
getent group douglas-memory-reader >/dev/null || groupadd --system douglas-memory-reader
getent passwd douglas-mcp >/dev/null \
    || useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin douglas-mcp
install -d -m 755 /opt/douglas /etc/douglas
install -m 755 "$here/douglas-mcp.py" /opt/douglas/douglas-mcp.py

# Its settings: a token made and written by one process, and the fleet view
# copied from one root-only file to another. Nothing of either is shown.
python3 -I - "$env" "$voice" <<'PY'
import os, secrets, sys
path, voice = sys.argv[1], sys.argv[2]
have = dict(l.split("=", 1) for l in open(path).read().splitlines() if "=" in l and not l.startswith("#")) \
    if os.path.exists(path) else {}
theirs = dict(l.split("=", 1) for l in open(voice).read().splitlines() if "=" in l and not l.startswith("#")) \
    if os.path.exists(voice) else {}
made = len(have.get("DOUGLAS_MCP_TOKEN", "")) < 20
token = secrets.token_urlsafe(32) if made else have["DOUGLAS_MCP_TOKEN"]
lines = ["# douglas-mcp: what Douglas remembers and sees, for the owner's other programs to read.",
         "# Whoever holds this token reads what he remembers. mcp-setup.sh show prints it; off takes it away.",
         f"DOUGLAS_MCP_TOKEN={token}",
         f"DOUGLAS_MCP_PORT={have.get('DOUGLAS_MCP_PORT', '9112')}",
         "MEMORY_READER_SOCKET=/run/douglas-memory/reader.sock",
         f"VITALS_SOCKET={theirs.get('VITALS_SOCKET') or '/run/douglas-agent/vitals.sock'}"]
if theirs.get("TZ"):
    lines += ["# The owner's time of day, for the times a reading names.", f"TZ={theirs['TZ']}"]
if theirs.get("FLEET_URL") and theirs.get("FLEET_TOKEN"):
    lines += ["# The machines at home, as Mission Control on the Pi sees them. The token opens nothing else.",
              f"FLEET_URL={theirs['FLEET_URL']}", f"FLEET_TOKEN={theirs['FLEET_TOKEN']}"]
old = os.umask(0o077)
open(path, "w").write("\n".join(lines) + "\n")
os.umask(old)
print("a token was made for it (not shown)" if made else "its token is as it was",
      "and it has the view of the fleet" if "FLEET_URL" in "".join(lines) else "and it has no view of the fleet")
PY
chown root:root "$env" && chmod 600 "$env"

install -m 644 "$here/douglas-memory.service" /etc/systemd/system/douglas-memory.service
install -m 644 "$here/douglas-memory-reader.socket" /etc/systemd/system/douglas-memory-reader.socket
install -m 644 "$here/douglas-mcp.service" /etc/systemd/system/douglas-mcp.service
systemctl daemon-reload
memory_with reader
systemctl enable douglas-mcp.service >/dev/null 2>&1
systemctl restart douglas-mcp.service

port="$(value "$env" DOUGLAS_MCP_PORT)"; port="${port:-9112}"
for _ in $(seq 1 20); do
    curl -fsS -m 2 -o /dev/null "http://127.0.0.1:$port/health" 2>/dev/null && break
    sleep 0.5
done
curl -fsS -m 2 -o /dev/null "http://127.0.0.1:$port/health" \
    || { echo "douglas-mcp did not come up - journalctl -u douglas-mcp"; exit 1; }
systemctl is-active --quiet douglas-memory || { echo "douglas-memory did not come back - journalctl -u douglas-memory"; exit 1; }

# Asked the way the server asks, and the ways it may not: as its user with
# its group the door for readers answers, and neither of the other two does.
ask='
import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(10)
try:
    s.connect(sys.argv[1])
except PermissionError:
    print("refused"); raise SystemExit
s.sendall(json.dumps({"op": sys.argv[2], "kind": "fact", "subject": "x", "text": "Planted by a reader.", "source": "said"}).encode() + b"\n")
r = json.loads(s.makefile().readline())
print("answered" if r.get("ok") else "said no")'
as_mcp() { runuser -u douglas-mcp -g douglas-mcp -G douglas-memory-reader -- python3 -I -c "$ask" "$@" 2>&1 || true; }
reads="$(as_mcp /run/douglas-memory/reader.sock stats)"
writes="$(as_mcp /run/douglas-memory/reader.sock remember)"
his="$(as_mcp /run/douglas-memory/assistant.sock stats)"
owners="$(as_mcp /run/douglas-memory/owner.sock stats)"
[ "$reads" = answered ] || { echo "the memory's door for readers did not answer douglas-mcp ($reads) - journalctl -u douglas-memory"; exit 1; }
[ "$writes" = "said no" ] || { echo "the door for readers took something to note down ($writes): not right"; exit 1; }
[ "$his" = refused ] || { echo "Douglas's own door opens to douglas-mcp ($his): not right"; exit 1; }
[ "$owners" = refused ] || { echo "the owner's door opens to douglas-mcp ($owners): not right"; exit 1; }

# And the server itself, the way a program would use it: with the token, and without.
python3 -I - "$env" "$port" <<'PY'
import json, sys, urllib.error, urllib.request
token = next(l.split("=", 1)[1] for l in open(sys.argv[1]).read().splitlines() if l.startswith("DOUGLAS_MCP_TOKEN="))
url = f"http://127.0.0.1:{sys.argv[2]}/mcp"


def post(msg, auth=True):
    req = urllib.request.Request(url, data=json.dumps(msg).encode(), headers=dict(
        {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        **({"Authorization": "Bearer " + token} if auth else {})))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "mcp-setup", "version": "1"}}}
assert post(init, auth=False)[0] == 401, "it answered without its token"
code, got = post(init)
assert code == 200 and got["result"]["serverInfo"]["name"] == "douglas", "it did not say who it is"
tools = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[1]["result"]["tools"]
assert tools and all(t["annotations"]["readOnlyHint"] for t in tools), "a tool that is not a reading"
told = {}
for name in ("notices", "vitals", "fleet"):
    res = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": {}}})[1]["result"]
    told[name] = "answers" if not res["isError"] else "does not answer"
print(f"douglas-mcp is up on :{sys.argv[2]}/mcp with {len(tools)} readings, all of them read-only: "
      + ", ".join(f"{k} {v}" for k, v in told.items()) + ". Without its token it answers nobody.")
PY
echo "The memory's door for readers opens to douglas-mcp alone, and reads only."
echo "Nobody holds its token yet: 'sudo $0 show' prints what a program of yours needs to be given."
