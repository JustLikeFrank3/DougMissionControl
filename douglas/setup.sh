#!/usr/bin/env bash
# douglas/setup.sh — install douglas-agent on Douglas (the A9 Mega).
#
# Run ON DOUGLAS, from a checkout:   sudo ./douglas/setup.sh
#
# Installs only the panel's window into Douglas. The model server, chat UI
# and voice services are Douglas's own business and are not touched here.
# Idempotent: re-running updates the agent and keeps the existing token.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }

install -d -m 755 /opt/douglas /etc/douglas
install -m 755 "$here/douglas-agent.py" /opt/douglas/douglas-agent.py
install -m 644 "$here/douglas-agent.service" /etc/systemd/system/douglas-agent.service

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

systemctl daemon-reload
systemctl enable --now douglas-agent
systemctl restart douglas-agent
sleep 1
curl -fsS "http://127.0.0.1:${DOUGLAS_AGENT_PORT:-9111}/health" >/dev/null \
    && echo "douglas-agent up on :9111" \
    || { echo "douglas-agent did not answer - journalctl -u douglas-agent"; exit 1; }
