#!/usr/bin/env bash
# douglas/setup.sh — install douglas-agent on Douglas (the A9 Mega).
#
# Run ON DOUGLAS, from a checkout:   sudo ./douglas/setup.sh
#
# Installs only the panel's window into Douglas. The model server, chat UI
# and voice services come from ./provision.sh, which runs this at its end.
# Idempotent: re-running updates the agent and keeps the existing token.
#
# With it comes the socket the agent gives this machine's vitals out at
# (douglas-agent-vitals.socket), and, where the voice pipeline is installed,
# VITALS_SOCKET in its settings: that is how Douglas can say how his own
# machine is doing. He learns of it when douglas-voice is next restarted,
# which this does not do. To take it away again, remove that line from
# /etc/douglas/voice.env and restart douglas-voice.
#
# And the other way: where the voice pipeline has been given the view of the
# fleet (FLEET_URL and FLEET_TOKEN, from pi/fleet-read.sh on the Pi), the
# agent is given the same two lines, for its watch: the rules that raise a
# notice when a machine stops answering or runs hot. That token opens one
# reading on the Pi and nothing else. Without it the watch still looks at
# this machine. WATCH=0 in /etc/douglas/agent.env switches the watch off.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }

install -d -m 755 /opt/douglas /etc/douglas
# The agent's own user: no home, no shell, nothing it owns. (It used to run
# as a user systemd made up at each start, and that one is still in use by
# the running agent under the same name: it is stopped first.)
if ! getent passwd douglas-agent 2>/dev/null | grep -q ':/nonexistent:'; then
    systemctl stop douglas-agent 2>/dev/null || true
    getent passwd douglas-agent >/dev/null 2>&1 \
        || useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin douglas-agent
fi
install -m 755 "$here/douglas-agent.py" /opt/douglas/douglas-agent.py
install -m 644 "$here/douglas-agent.service" /etc/systemd/system/douglas-agent.service
install -m 644 "$here/douglas-agent-vitals.socket" /etc/systemd/system/douglas-agent-vitals.socket

env=/etc/douglas/agent.env
if [ ! -f "$env" ]; then
    token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
    cat > "$env" <<CONF
# douglas-agent config. The deck Pi needs the same token as
# DOUGLAS_AGENT_TOKEN in /etc/flightsim/boot.env.
DOUGLAS_AGENT_TOKEN=$token
DOUGLAS_AGENT_PORT=9111
# llama.cpp's server. Start it with --metrics for the tok/s meters.
LLM_URL=http://127.0.0.1:8081
# The voice pipeline's files and push-to-talk endpoint. Until a pipeline
# writes them, the DOUGLAS surface shows "no voice pipeline".
VOICE_STATE_FILE=/run/douglas/voice.json
TRANSCRIPT_FILE=/var/lib/douglas/transcript.jsonl
#VOICE_PTT_URL=http://127.0.0.1:10800/ptt
CONF
    chmod 600 "$env"
    echo "wrote $env - copy DOUGLAS_AGENT_TOKEN into the deck Pi's boot.env"
fi

# The fleet, for the watch: the two lines the pipeline has, copied by a
# program from one root-only file to the other. Nothing of it is shown.
if [ -f /etc/douglas/voice.env ] && ! grep -q '^FLEET_TOKEN=.' "$env"; then
    python3 -I - /etc/douglas/voice.env "$env" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
got = {k: v for k, _, v in (l.partition("=") for l in open(src).read().splitlines())
       if k in ("FLEET_URL", "FLEET_TOKEN") and v}
if len(got) == 2:
    kept = [l for l in open(dst).read().splitlines() if not l.startswith(("FLEET_URL=", "FLEET_TOKEN="))]
    while kept and not kept[-1].strip():
        kept.pop()
    kept += ["", "# The machines at home, as Mission Control on the Pi sees them: for the watch. The token opens nothing else.",
             f"FLEET_URL={got['FLEET_URL']}", f"FLEET_TOKEN={got['FLEET_TOKEN']}"]
    open(dst, "w").write("\n".join(kept) + "\n")
    print("the watch has the view of the fleet now (FLEET_URL and FLEET_TOKEN, not shown)")
PY
fi
# The time of day where the owner is, as the pipeline has it: a notice that
# names a day should name the owner's day, not the machine's.
tz="$(sed -n 's/^TZ=//p' /etc/douglas/voice.env 2>/dev/null | tail -n1)"
if [ -n "$tz" ] && ! grep -q '^TZ=' "$env"; then
    printf '\n# The owner'"'"'s time of day, for the days a notice names.\nTZ=%s\n' "$tz" >> "$env"
fi
chown root:root "$env" && chmod 600 "$env"

systemctl daemon-reload
# The socket first: a door cannot be made while the service behind it is up,
# and the agent takes it over when it starts.
systemctl stop douglas-agent 2>/dev/null || true
systemctl enable douglas-agent-vitals.socket >/dev/null 2>&1
systemctl restart douglas-agent-vitals.socket
systemctl enable douglas-agent >/dev/null 2>&1
systemctl start douglas-agent
voice=/etc/douglas/voice.env
if [ -f "$voice" ] && ! grep -q '^VITALS_SOCKET=' "$voice"; then
    printf "\n# This machine's vitals, from douglas-agent: how Douglas knows how his own machine is doing.\nVITALS_SOCKET=/run/douglas-agent/vitals.sock\n" >> "$voice"
    echo "VITALS_SOCKET is in $voice now: restart douglas-voice when nobody is talking to him."
fi
sleep 1
curl -fsS "http://127.0.0.1:${DOUGLAS_AGENT_PORT:-9111}/health" >/dev/null \
    && echo "douglas-agent up on :9111" \
    || { echo "douglas-agent did not answer - journalctl -u douglas-agent"; exit 1; }
# The vitals, asked for the way the voice pipeline asks: as its user.
vu="$(systemctl show douglas-voice -p User --value 2>/dev/null)"; vu="${vu:-douglas}"
if id "$vu" >/dev/null 2>&1; then
    runuser -u "$vu" -- python3 -I -c '
import json, socket
s = socket.socket(socket.AF_UNIX); s.settimeout(10); s.connect("/run/douglas-agent/vitals.sock")
v = json.loads(s.makefile().readline())
assert "transcript" not in v and "host" in v
print("the vitals socket answers: %d services read, %d model files on the disk" % (len(v.get("units") or []), len(v.get("models") or [])))' \
        || { echo "the vitals socket did not answer - journalctl -u douglas-agent"; exit 1; }
fi
