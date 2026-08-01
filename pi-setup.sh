#!/usr/bin/env bash
# pi-setup.sh — install the "flight sim bootup" trigger stack on the Pi:
#   * fauxmo (Wemo emulator) in a venv at /opt/fauxmo, exposing a virtual
#     smart device named "flight sim" that the Echo discovers on the LAN
#   * a minimal command plugin (fauxmo v3 ships no CommandLinePlugin —
#     writing our own ~20 lines beats vendoring fauxmo-plugins)
#   * flightsim-boot.sh -> /usr/local/bin (the orchestrator fauxmo fires)
#   * /etc/flightsim/boot.env with the workstation's addresses
#   * a dedicated ssh keypair the workstation's Linux boot will trust with
#     a forced command (see linux-setup.sh) — printed at the end
#
# Run from the workstation (either boot), same conventions as pi-deploy.sh:
#   ./scripts/flightsim/pi-setup.sh
#   PI_HOST=user@192.168.1.51 ./scripts/flightsim/pi-setup.sh   # from Windows
set -euo pipefail

PI="${PI_HOST:-pi-node1}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The fauxmo device must answer on the LAN the Echo lives on, not the
# direct link.
PI_LAN_IP="${PI_LAN_IP:-192.168.1.51}"
DEVICE_NAME="${DEVICE_NAME:-flight sim}"
DEVICE_PORT=49915

scp -q "${ROOT}/flightsim/flightsim-boot.sh" "${PI}:/tmp/flightsim-boot.sh"

ssh "${PI}" "PI_LAN_IP='${PI_LAN_IP}' DEVICE_NAME='${DEVICE_NAME}' DEVICE_PORT='${DEVICE_PORT}' bash -s" <<'REMOTE'
set -euo pipefail

# A Windows checkout may ship CRLF — a CR after the shebang breaks exec.
sed -i 's/\r$//' /tmp/flightsim-boot.sh
sudo install -m 755 /tmp/flightsim-boot.sh /usr/local/bin/flightsim-boot.sh
rm /tmp/flightsim-boot.sh

# Defaults live in the script; the env file exists to override without a
# redeploy. Create once, keep local edits.
sudo mkdir -p /etc/flightsim
if [ ! -f /etc/flightsim/boot.env ]; then
    sudo tee /etc/flightsim/boot.env >/dev/null <<'ENV'
# flightsim-boot.sh overrides (defaults shown; uncomment to change)
#WS_LAN=192.168.1.50
#WS_MAC=AA:BB:CC:DD:EE:FF
#WS_BROADCAST=192.168.1.255
#LINUX_SSH=user@192.168.100.1
# Required for the "boot into Linux" device — the token printed by
# windows-setup.ps1 (C:\ProgramData\jobcontext\boot-agent.token):
#WIN_AGENT_TOKEN=
#WIN_AGENT_PORT=9107
ENV
fi

# Dedicated key for the reboot request — restricted on the workstation to
# one forced command, so this key can boot-to-Windows and nothing else.
if [ ! -f "$HOME/.ssh/flightsim_ed25519" ]; then
    ssh-keygen -t ed25519 -N "" -C "flightsim-boot@pi" -f "$HOME/.ssh/flightsim_ed25519"
fi

# fauxmo venv
if [ ! -x /opt/fauxmo/venv/bin/fauxmo ]; then
    sudo mkdir -p /opt/fauxmo
    sudo chown "$USER" /opt/fauxmo
    python3 -m venv /opt/fauxmo/venv
    /opt/fauxmo/venv/bin/pip install --quiet fauxmo
fi

cat > /opt/fauxmo/cmdplugin.py <<'PLUGIN'
"""Shell-command fauxmo plugin (fauxmo v3 core ships none)."""
import subprocess

from fauxmo.plugins import FauxmoPlugin


class CommandPlugin(FauxmoPlugin):
    def __init__(self, *, name, port, on_cmd, off_cmd="true", state_cmd=None):
        self.on_cmd, self.off_cmd, self.state_cmd = on_cmd, off_cmd, state_cmd
        super().__init__(name=name, port=port)

    def _run(self, cmd):
        return subprocess.run(cmd, shell=True).returncode == 0

    def on(self):
        return self._run(self.on_cmd)

    def off(self):
        return self._run(self.off_cmd)

    def get_state(self):
        if not self.state_cmd:
            return "unknown"
        return "on" if self._run(self.state_cmd) else "off"
PLUGIN

cat > /opt/fauxmo/config.json <<CONF
{
  "FAUXMO": {"ip_address": "${PI_LAN_IP}"},
  "PLUGINS": {
    "CommandPlugin": {
      "path": "/opt/fauxmo/cmdplugin.py",
      "DEVICES": [
        {
          "name": "${DEVICE_NAME}",
          "port": ${DEVICE_PORT},
          "on_cmd": "/usr/local/bin/flightsim-boot.sh windows bg",
          "off_cmd": "true",
          "state_cmd": "curl -sf --max-time 2 http://192.168.1.50:9106/ >/dev/null || ! flock -n /tmp/flightsim-boot.lock true"
        },
        {
          "name": "workstation",
          "port": 49916,
          "on_cmd": "/usr/local/bin/flightsim-boot.sh linux bg",
          "off_cmd": "true",
          "state_cmd": "curl -sf --max-time 2 http://192.168.1.50:9105/ >/dev/null || ! flock -n /tmp/flightsim-boot.lock true"
        }
      ]
    }
  }
}
CONF

# System unit (not user) so it is up right after Pi reboot, but running as
# this user — flightsim-boot.sh needs $HOME/.ssh/flightsim_ed25519.
sudo tee /etc/systemd/system/fauxmo.service >/dev/null <<UNIT
[Unit]
Description=fauxmo Wemo emulator (Alexa "flight sim" trigger)
After=network-online.target
Wants=network-online.target

[Service]
User=${USER}
ExecStart=/opt/fauxmo/venv/bin/fauxmo -c /opt/fauxmo/config.json
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable fauxmo.service
sudo systemctl restart fauxmo.service
sleep 2
systemctl is-active fauxmo.service

echo
echo "Pi-side public key — pass to linux-setup.sh on the workstation's Linux boot:"
cat "$HOME/.ssh/flightsim_ed25519.pub"
REMOTE

echo
echo "Next:"
echo "  1. On the workstation's Linux boot:  sudo ./scripts/flightsim/linux-setup.sh '<pubkey printed above>'"
echo "  2. Say: \"Alexa, discover devices\"  (finds '${DEVICE_NAME}')"
echo "  3. Alexa app -> Routines -> phrase \"flight sim bootup\" -> Alexa Speaks + turn on '${DEVICE_NAME}'"
