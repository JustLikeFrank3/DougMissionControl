#!/usr/bin/env bash
# dougvoice/setup.sh — turn a Pi Zero 2 W + PiSugar Whisplay HAT into DougVoice.
#
# Run ON THE PI (Raspberry Pi OS Lite 64-bit), from a checkout:
#
#   sudo DOUGLAS_AGENT_TOKEN=<token from Douglas's /etc/douglas/agent.env> \
#        ./dougvoice/setup.sh
#
# Optional, in the environment:
#   TS_AUTHKEY=tskey-...      join the tailnet without the interactive login URL
#   WAKE_WORD=ok_nabu         run openWakeWord locally (~150 MB of the 512 MB)
#   WHISPLAY_REF=main         which commit/branch of PiSugar/Whisplay to install
#   WHISPLAY_REINSTALL=1      re-run PiSugar's driver installer even if the card exists
#
# Idempotent: re-running updates the code and units and keeps
# /etc/dougvoice/dougvoice.env as you left it.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }

NAME=dougvoice
OPT=/opt/dougvoice
WHISPLAY_REPO=https://github.com/PiSugar/Whisplay.git
WHISPLAY_REF="${WHISPLAY_REF:-main}"
need_reboot=0

# ── 1. hostname ──────────────────────────────────────────────────────────
if [ "$(hostname)" != "$NAME" ]; then
    hostnamectl set-hostname "$NAME"
    sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$NAME/" /etc/hosts
    grep -q "^127\.0\.1\.1" /etc/hosts || printf '127.0.1.1\t%s\n' "$NAME" >> /etc/hosts
    echo "hostname -> $NAME"
fi

# ── 2. packages (all small; nothing here does inference) ─────────────────
apt-get update -qq
apt-get install -y -qq git curl alsa-utils python3-venv python3-libgpiod python3-spidev

# ── 3. tailnet ───────────────────────────────────────────────────────────
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
if tailscale status >/dev/null 2>&1; then
    tailscale set --hostname="$NAME"
else
    tailscale up --hostname="$NAME" ${TS_AUTHKEY:+--auth-key="$TS_AUTHKEY"}
fi

# ── 4. the Whisplay's audio + display driver — PiSugar's own ─────────────
# Upstream: github.com/PiSugar/Whisplay. Its install_driver.sh enables SPI
# and installs PiSugar's whisplay-soundcard overlay and WM8960 driver; the
# ALSA card it creates is "whisplaysound". runtime/whisplay.py from the same
# checkout is the LCD/LED/button driver dougvoice-face imports.
install -d -m 755 "$OPT"
if [ -d "$OPT/Whisplay/.git" ]; then
    git -C "$OPT/Whisplay" fetch -q --depth 1 origin "$WHISPLAY_REF"
    git -C "$OPT/Whisplay" checkout -q FETCH_HEAD
else
    git clone -q --depth 1 --branch "$WHISPLAY_REF" "$WHISPLAY_REPO" "$OPT/Whisplay" \
        || { git clone -q "$WHISPLAY_REPO" "$OPT/Whisplay"
             git -C "$OPT/Whisplay" checkout -q "$WHISPLAY_REF"; }
fi
echo "Whisplay driver at $(git -C "$OPT/Whisplay" rev-parse --short HEAD)"
if [ "${WHISPLAY_REINSTALL:-0}" = 1 ] || ! aplay -l 2>/dev/null | grep -qi whisplay; then
    # The installer asks "Proceed? [y/N]". pipefail off so yes's SIGPIPE
    # does not count as the installer failing.
    ( cd "$OPT/Whisplay" && set +o pipefail && yes | bash install_driver.sh )
    need_reboot=1
fi

# ── 5. the satellite (and, if asked, the wake word) in venvs ─────────────
id -u dougvoice >/dev/null 2>&1 \
    || useradd --system --no-create-home --shell /usr/sbin/nologin dougvoice
usermod -aG audio,gpio,spi dougvoice

[ -x "$OPT/venv/bin/python3" ] || python3 -m venv "$OPT/venv"
"$OPT/venv/bin/pip" install -q --upgrade pip
"$OPT/venv/bin/pip" install -q --upgrade wyoming-satellite

wake="${WAKE_WORD:-}"
[ -z "$wake" ] && [ -f /etc/dougvoice/dougvoice.env ] \
    && wake="$(sed -n 's/^WAKE_WORD=//p' /etc/dougvoice/dougvoice.env | tail -1)"
if [ -n "$wake" ]; then
    [ -x "$OPT/wake-venv/bin/python3" ] || python3 -m venv "$OPT/wake-venv"
    "$OPT/wake-venv/bin/pip" install -q --upgrade pip
    "$OPT/wake-venv/bin/pip" install -q --upgrade wyoming-openwakeword
fi

# ── 6. our code, config and units ────────────────────────────────────────
install -m 755 "$here/dougvoice.py" "$OPT/dougvoice.py"
for u in dougvoice-face dougvoice-satellite dougvoice-wake; do
    install -m 644 "$here/$u.service" "/etc/systemd/system/$u.service"
done

env=/etc/dougvoice/dougvoice.env
install -d -m 755 /etc/dougvoice
if [ ! -f "$env" ]; then
    cat > "$env" <<CONF
# DougVoice config. Douglas connects to the satellite on :10700.
# Same token as DOUGLAS_AGENT_TOKEN in Douglas's /etc/douglas/agent.env:
# the face polls Douglas's voice state with it and sends the button's PTT.
DOUGLAS_AGENT_TOKEN=${DOUGLAS_AGENT_TOKEN:-}
DOUGLAS_HOST=douglas
DOUGLAS_AGENT_PORT=9111
SATELLITE_PORT=10700
SATELLITE_NAME=DougVoice
AUDIO_DEVICE=plughw:CARD=whisplaysound,DEV=0
# Empty = stream mode: the mic streams to Douglas, Douglas gates on PTT.
# Set (e.g. ok_nabu) = wake mode: local openWakeWord, the button injects a
# detection. Re-run setup.sh after changing it, to install or stop the model.
WAKE_WORD=$wake
#MIC_VOLUME=2.0
#SND_VOLUME=1.0
BACKLIGHT=60
CONF
    chown root:dougvoice "$env"
    chmod 640 "$env"
    echo "wrote $env"
fi
grep -q '^DOUGLAS_AGENT_TOKEN=.\+' "$env" \
    || echo "NOTE: DOUGLAS_AGENT_TOKEN is empty in $env - the button will do nothing"

systemctl daemon-reload
if [ -n "$wake" ]; then
    systemctl enable dougvoice-wake
else
    systemctl disable --now dougvoice-wake 2>/dev/null || true
fi
systemctl enable dougvoice-face dougvoice-satellite

if [ "$need_reboot" = 1 ]; then
    echo "Whisplay driver installed - reboot to load it: sudo reboot"
    exit 0
fi
[ -n "$wake" ] && systemctl restart dougvoice-wake
systemctl restart dougvoice-face dougvoice-satellite
sleep 3
systemctl is-active -q dougvoice-satellite \
    && echo "DougVoice up: satellite on :10700, $(tailscale ip -4 2>/dev/null | head -1)" \
    || { echo "satellite did not start - journalctl -u dougvoice-satellite"; exit 1; }
