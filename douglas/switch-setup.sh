#!/usr/bin/env bash
# douglas/switch-setup.sh — the owner's off-switches for what Douglas may use.
#
# Run ON DOUGLAS, from a checkout:   sudo ./douglas/switch-setup.sh
#                                    sudo ./douglas/switch-setup.sh off
#
# What it leaves behind:
#   user     douglas-switch (the one program that writes the switches down:
#            no network, one small file)
#   group    douglas-switch-owner (who may open its door: douglas-agent's
#            unit, which is how the owner's screens reach it)
#   code     /opt/douglas/douglas-switch.py
#   units    douglas-switch.socket and douglas-switch.service, and a drop-in
#            for douglas-agent that gives it the group and nothing else
#   the file /var/lib/douglas-switch/capabilities, a line a tool: written by
#            douglas-switch, read by the voice pipeline when a turn begins
#   and      SWITCH_SOCKET in /etc/douglas/agent.env
#
# The pipeline has no part in it but to read the file: it cannot open the
# door and cannot write the file, so nothing that is said to Douglas or read
# by him switches anything on. It restarts douglas-agent (which holds
# nothing) and not douglas-voice, which needs no restart: it looks at the
# file when a turn begins. Idempotent.
#
# `off` takes the door, the helper and the file away: everything he has is
# on again, as before there were switches.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
agent=/etc/douglas/agent.env
dropin=/etc/systemd/system/douglas-agent.service.d/switch.conf
file=/var/lib/douglas-switch/capabilities

if [ "${1:-on}" = off ]; then
    systemctl disable --now douglas-switch.socket douglas-switch.service 2>/dev/null || true
    rm -f /etc/systemd/system/douglas-switch.socket /etc/systemd/system/douglas-switch.service "$dropin" "$file"
    [ -f "$agent" ] && sed -i "/^# The owner's switches (douglas-switch)/d; /^SWITCH_SOCKET=/d" "$agent"
    systemctl daemon-reload
    systemctl try-restart douglas-agent 2>/dev/null || true
    echo "The switches are gone, and with them whatever was switched off: he has all he is set up with from his next turn."
    exit 0
fi

[ -f /etc/systemd/system/douglas-agent.service ] || { echo "douglas-agent is not installed here: run setup.sh first"; exit 1; }
getent group douglas-switch-owner >/dev/null || groupadd --system douglas-switch-owner
getent passwd douglas-switch >/dev/null \
    || useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin douglas-switch
install -d -m 755 /opt/douglas
install -m 755 "$here/douglas-switch.py" /opt/douglas/douglas-switch.py
install -m 644 "$here/douglas-switch.socket" /etc/systemd/system/douglas-switch.socket
install -m 644 "$here/douglas-switch.service" /etc/systemd/system/douglas-switch.service
install -d -m 755 "$(dirname "$dropin")"
printf '# Written by switch-setup.sh: this unit may open the door of douglas-switch.\n[Service]\nSupplementaryGroups=douglas-switch-owner\n' > "$dropin"
if [ -f "$agent" ] && ! grep -q '^SWITCH_SOCKET=' "$agent"; then
    printf "\n# The owner's switches (douglas-switch): where the screens switch his tools on and off.\nSWITCH_SOCKET=/run/douglas-switch/switch.sock\n" >> "$agent"
fi
systemctl daemon-reload
systemctl enable douglas-switch.socket >/dev/null 2>&1
systemctl stop douglas-switch.service 2>/dev/null || true
systemctl restart douglas-switch.socket
systemctl restart douglas-agent

# Asked the way the agent asks, and the way the pipeline may not.
ask='
import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(10)
try:
    s.connect("/run/douglas-switch/switch.sock")
except PermissionError:
    print("refused"); raise SystemExit
s.sendall(b"{\"op\": \"show\"}\n"); r = json.loads(s.makefile().readline())
print(("nothing is switched off" if not r["off"] else ", ".join(r["off"]) + " switched off") if r.get("ok") else "said no")'
theirs="$(runuser -u douglas-agent -g douglas-agent -G douglas-switch-owner -- python3 -I -c "$ask" 2>&1 || true)"
case "$theirs" in *"switched off") ;; *) echo "douglas-switch did not answer the agent ($theirs) - journalctl -u douglas-switch"; exit 1 ;; esac
vu="$(systemctl show douglas-voice -p User --value 2>/dev/null)"; vu="${vu:-douglas}"
if id "$vu" >/dev/null 2>&1; then
    his="$(runuser -u "$vu" -- python3 -I -c "$ask" 2>&1 || true)"
    [ "$his" = refused ] || { echo "the door of the switches opens to the voice pipeline ($his): not right"; exit 1; }
    if runuser -u "$vu" -- test -w /var/lib/douglas-switch 2>/dev/null \
        || { [ -e "$file" ] && runuser -u "$vu" -- test -w "$file" 2>/dev/null; }; then
        echo "the voice pipeline can write the switches: not right"; exit 1
    fi
    runuser -u "$vu" -- test -r /var/lib/douglas-switch \
        || { echo "the voice pipeline cannot read where the switches are kept: not right"; exit 1; }
fi
echo "douglas-switch is at its door: $theirs. The agent can switch, and the voice pipeline can read the switches and nothing more."
