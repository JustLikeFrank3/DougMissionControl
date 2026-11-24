#!/usr/bin/env bash
# douglas/memory-setup.sh — install Douglas's memory on Douglas: what he
# keeps from one conversation to the next, and the record of what he did.
#
# Run ON DOUGLAS, from a checkout:   sudo ./douglas/memory-setup.sh
#                                    sudo ./douglas/memory-setup.sh off
#
# What it leaves behind:
#   user     douglas-memory (the service: no network, one database)
#   groups   douglas-memory-assistant (who may open Douglas's own door: the
#            voice pipeline's unit) and douglas-memory-owner (who may open
#            the owner's: douglas-agent's unit, for Mission Control)
#   code     /opt/douglas/douglas-memory.py
#   units    douglas-memory.service and its two doors,
#            douglas-memory-assistant.socket and douglas-memory-owner.socket,
#            and a drop-in each for douglas-voice and douglas-agent that
#            gives the unit its group and nothing else
#   config   /etc/douglas/memory.env (written only if missing)
#   and      MEMORY_SOCKET in /etc/douglas/voice.env and MEMORY_OWNER_SOCKET
#            in /etc/douglas/agent.env, where they have none
# The database is /var/lib/douglas-memory/memory.db.
#
# It restarts douglas-agent (which holds nothing) and does not restart
# douglas-voice, which is how he comes to use his memory: do that when
# nobody is talking to him (sudo systemctl restart douglas-voice).
# Idempotent. `off` stops and disables it all and takes the settings and the
# two groups' drop-ins away again: he then runs exactly as he did before he
# had a memory. The database stays where it is.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
units="douglas-memory-assistant.socket douglas-memory-owner.socket douglas-memory.service"
voice=/etc/douglas/voice.env
agent=/etc/douglas/agent.env
dropin() { echo "/etc/systemd/system/$1.service.d/memory.conf"; }

if [ "${1:-on}" = off ]; then
    systemctl disable --now douglas-memory.service douglas-memory-assistant.socket \
        douglas-memory-owner.socket 2>/dev/null || true
    rm -f "$(dropin douglas-voice)" "$(dropin douglas-agent)"
    rmdir /etc/systemd/system/douglas-voice.service.d /etc/systemd/system/douglas-agent.service.d 2>/dev/null || true
    if [ -f "$agent" ] && grep -q '^MEMORY_OWNER_SOCKET=' "$agent"; then
        sed -i "/^# Douglas's memory (douglas-memory)/d; /^MEMORY_OWNER_SOCKET=/d" "$agent"
    fi
    systemctl daemon-reload
    systemctl try-restart douglas-agent 2>/dev/null || true
    if [ -f "$voice" ] && grep -q '^MEMORY_SOCKET=' "$voice"; then
        sed -i "/^# Douglas's memory (douglas-memory)/d; /^MEMORY_SOCKET=/d" "$voice"
        echo "MEMORY_SOCKET is out of $voice: restart douglas-voice when nobody is talking to him."
    fi
    echo "The memory is off. What he remembered is still in /var/lib/douglas-memory."
    exit 0
fi

id douglas >/dev/null 2>&1 || { echo "there is no douglas user yet: run provision.sh first"; exit 1; }
python3 -I -c 'import sqlite3; sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")' \
    2>/dev/null || { echo "this Python's SQLite has no full-text search (FTS5): not installed"; exit 1; }

for g in douglas-memory-assistant douglas-memory-owner; do
    getent group "$g" >/dev/null || groupadd --system "$g"
done
id douglas-memory >/dev/null 2>&1 \
    || useradd --system --user-group --home-dir /var/lib/douglas-memory --shell /usr/sbin/nologin douglas-memory
install -d -m 755 /opt/douglas /etc/douglas
install -m 755 "$here/douglas-memory.py" /opt/douglas/douglas-memory.py

env=/etc/douglas/memory.env
if [ ! -f "$env" ]; then
    cat > "$env" <<'CONF'
# douglas-memory: what Douglas remembers from one conversation to the next.
# Restart after editing:  sudo systemctl restart douglas-memory
#
# How many memories are kept in use. Past that, the ones he is least sure
# of and has gone longest without using are let go (kept, marked deleted,
# for the owner to bring back or purge).
#MEMORY_MAX=5000
# How many lines of the record of what he did are kept.
#MEMORY_EVENTS=5000
CONF
fi
chown root:douglas-memory "$env" && chmod 640 "$env"

for u in $units; do
    install -m 644 "$here/$u" "/etc/systemd/system/$u"
done
# The database is the service's alone to read, whatever made it.
chmod -f go-rwx /var/lib/douglas-memory/memory.db* 2>/dev/null || true
# Each of the two callers is given its group, and only its group: the
# pipeline's unit may open Douglas's door, the agent's the owner's.
for pair in douglas-voice:douglas-memory-assistant douglas-agent:douglas-memory-owner; do
    install -d -m 755 "$(dirname "$(dropin "${pair%%:*}")")"
    printf '# Written by memory-setup.sh: this unit may open its door to douglas-memory.\n[Service]\nSupplementaryGroups=%s\n' \
        "${pair##*:}" > "$(dropin "${pair%%:*}")"
done
systemctl daemon-reload
systemctl enable $units >/dev/null 2>&1
# A door cannot be remade while the service behind it is up.
systemctl stop douglas-memory.service 2>/dev/null || true
systemctl restart douglas-memory-assistant.socket douglas-memory-owner.socket
systemctl start douglas-memory.service

if [ -f "$agent" ] && ! grep -q '^MEMORY_OWNER_SOCKET=' "$agent"; then
    printf "\n# Douglas's memory (douglas-memory): the owner's door, for Mission Control.\nMEMORY_OWNER_SOCKET=/run/douglas-memory/owner.sock\n" >> "$agent"
fi
systemctl try-restart douglas-agent 2>/dev/null || true
if [ -f "$voice" ] && ! grep -q '^MEMORY_SOCKET=' "$voice"; then
    printf "\n# Douglas's memory (douglas-memory): what he keeps from one conversation to the next.\nMEMORY_SOCKET=/run/douglas-memory/assistant.sock\n" >> "$voice"
    echo "MEMORY_SOCKET is in $voice now: restart douglas-voice when nobody is talking to him."
fi

sleep 1
systemctl is-active --quiet douglas-memory || { echo "douglas-memory is not running - journalctl -u douglas-memory"; exit 1; }
# Asked the way each caller asks, and the way neither may: as the pipeline's
# user with its group (Douglas's door answers), as that user without it
# (it does not: the other services of that user stay out), and at the
# owner's door (never).
ask='
import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(10)
try:
    s.connect(sys.argv[1])
except PermissionError:
    print("refused"); raise SystemExit
s.sendall(b"{\"op\": \"stats\"}\n"); r = json.loads(s.makefile().readline())
print("%d memories kept" % r["active"] if r.get("ok") else "said no")'
mine="$(runuser -u douglas -g douglas -G douglas-memory-assistant -- python3 -I -c "$ask" /run/douglas-memory/assistant.sock 2>&1 || true)"
plain="$(runuser -u douglas -- python3 -I -c "$ask" /run/douglas-memory/assistant.sock 2>&1 || true)"
theirs="$(runuser -u douglas -g douglas -G douglas-memory-assistant -- python3 -I -c "$ask" /run/douglas-memory/owner.sock 2>&1 || true)"
case "$mine" in *"memories kept") ;; *) echo "douglas-memory did not answer the voice pipeline ($mine) - journalctl -u douglas-memory"; exit 1 ;; esac
[ "$plain" = refused ] || { echo "Douglas's door opens to the pipeline's user without its group ($plain): not right"; exit 1; }
[ "$theirs" = refused ] || { echo "the owner's door opens to the voice pipeline ($theirs): not right"; exit 1; }
echo "douglas-memory is up: $mine. Douglas's door opens to the voice pipeline only, the owner's to douglas-agent only."
