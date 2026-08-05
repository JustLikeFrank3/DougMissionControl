#!/usr/bin/env bash
# linux/setup.sh — one-time prep on the workstation's LINUX boot so the Pi
# can steer the next boot into Windows. Run with sudo, passing the Pi's
# public key (printed by pi/setup.sh):
#
#   sudo ./linux/setup.sh 'ssh-ed25519 AAAA... flightsim-boot@pi'
#
# Installs:
#   * GRUB_DEFAULT=saved (grub-reboot needs it; saved default is pinned to
#     the CURRENT default entry, so day-to-day boot behavior is unchanged)
#   * /usr/local/bin/boot-to-windows — grub-reboot <Windows entry> + reboot
#   * sudoers rule: invoking user may run exactly that script, nothing else
#   * authorized_keys entry for the Pi key, command=-forced to that script
#   * WOL persistence: ethtool wol g on the wired NIC at boot (the Linux
#     driver otherwise disarms WOL, breaking wake-after-Linux-shutdown)
#   * logon autostart: spoken boot confirmation (neural TTS with espeak
#     fallback) + VS Code — the Linux mirror of jarvis-greeting.ps1
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/grub_utils.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/ssh_utils.sh"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
PUBKEY="${1:-}"

REAL_USER="${SUDO_USER:?run via sudo, not a root login}"
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
# No WS_MAC supplied: use the default-route NIC — this script runs on the
# workstation itself, so its own wired NIC is the WOL target.
WS_MAC="${WS_MAC:-}"
if [ -z "$WS_MAC" ]; then
    DEFAULT_IFACE="$(ip -o route show default | awk '{print $5}' | head -1 || true)"
    [ -n "$DEFAULT_IFACE" ] && WS_MAC="$(cat "/sys/class/net/$DEFAULT_IFACE/address" 2>/dev/null || true)"
    [ -n "$WS_MAC" ] && echo "WS_MAC auto-detected: $WS_MAC ($DEFAULT_IFACE)"
fi
WS_MAC="${WS_MAC:-aa:bb:cc:dd:ee:ff}"

GRUB_CFG=/boot/grub/grub.cfg
[ -f "$GRUB_CFG" ] || GRUB_CFG=/boot/grub2/grub.cfg
UPDATE_GRUB="update-grub"
command -v update-grub >/dev/null || UPDATE_GRUB="grub-mkconfig -o $GRUB_CFG"

WIN_ENTRY="$(extract_windows_grub_entry "$GRUB_CFG")"
if [ -z "$WIN_ENTRY" ]; then
    echo "ERROR: no Windows entry in $GRUB_CFG." >&2
    echo "Enable os-prober (GRUB_DISABLE_OS_PROBER=false in /etc/default/grub)," >&2
    echo "run $UPDATE_GRUB, then re-run this script." >&2
    exit 1
fi
echo "Windows GRUB entry: $WIN_ENTRY"

# grub-reboot is a no-op unless GRUB_DEFAULT=saved. Pin the saved default
# to today's default entry first so nothing else changes.
CUR_DEFAULT="$(grep -oP '^GRUB_DEFAULT=\K.*' /etc/default/grub | tr -d '"' || echo 0)"
if [ "$CUR_DEFAULT" != "saved" ]; then
    cp /etc/default/grub "/etc/default/grub.bak.flightsim"
    sed -i 's/^GRUB_DEFAULT=.*/GRUB_DEFAULT=saved/' /etc/default/grub
    $UPDATE_GRUB
    grub-set-default "$CUR_DEFAULT"
    echo "GRUB_DEFAULT=saved (saved default pinned to previous default: $CUR_DEFAULT)"
fi

cat > /usr/local/bin/boot-to-windows <<EOF
#!/bin/bash
# Installed by this project's linux/setup.sh — one-shot reboot into
# Windows, invoked by the Pi's forced-command ssh key.
set -e
grub-reboot "$WIN_ENTRY"
# -i: desktop sessions hold shutdown inhibitors that block plain shutdown.
systemctl reboot -i
EOF
chmod 755 /usr/local/bin/boot-to-windows

echo "$REAL_USER ALL=(root) NOPASSWD: /usr/local/bin/boot-to-windows" \
    > /etc/sudoers.d/flightsim
chmod 440 /etc/sudoers.d/flightsim

# Forced command + restrict: this key can reboot-to-Windows and do nothing
# else — no shell, no forwarding, regardless of what the client requests.
AK="$REAL_HOME/.ssh/authorized_keys"
install -d -m 700 -o "$REAL_USER" -g "$REAL_USER" "$REAL_HOME/.ssh"
chown "$REAL_USER:$REAL_USER" "$AK" 2>/dev/null || true
add_restricted_authorized_key "$AK" "$PUBKEY" "/usr/local/bin/boot-to-windows"

# Keep WOL armed after a Linux shutdown (driver default often disarms it).
# `|| true`: no match must not kill the script under set -e/pipefail.
IFACE="$(ip -o link | grep -i "$WS_MAC" | awk -F': ' '{print $2}' | head -1 || true)"
if [ -n "$IFACE" ]; then
    cat > /etc/systemd/system/flightsim-wol.service <<EOF
[Unit]
Description=Arm Wake-on-LAN on $IFACE (flight sim bootup)
After=network.target

[Service]
Type=oneshot
ExecStart=$(command -v ethtool) -s $IFACE wol g

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl enable --now flightsim-wol.service >/dev/null 2>&1 || true
    echo "WOL armed on $IFACE (persisted via flightsim-wol.service)"
else
    echo "WARN: no interface with MAC $WS_MAC found — arm WOL manually (ethtool -s <iface> wol g)"
fi

# --- Logon greeting + VS Code (mirror of the Windows JarvisGreeting) -------
apt-get install -y -qq mpg123 espeak-ng python3-pip >/dev/null 2>&1 || true
sudo -u "$REAL_USER" python3 -m pip install --user --quiet --break-system-packages edge-tts 2>/dev/null \
    || echo "WARN: edge-tts install failed — greeting will use the espeak fallback voice"

cat > /usr/local/bin/workstation-greeting <<'EOF'
#!/bin/bash
# Installed by this project's linux/setup.sh — spoken boot confirmation
# + VS Code at Linux logon. Neural voice via edge-tts when network/pip
# allow, espeak-ng as the offline fallback.
sleep 6
temp=$(nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>/dev/null | head -1)
hour=$(date +%H)
if   [ "$hour" -lt 12 ]; then tod="Good morning"
elif [ "$hour" -lt 18 ]; then tod="Good afternoon"
else tod="Good evening"; fi
msg="$tod, sir. Linux workstation online. All services nominal.${temp:+ G P U thermals at ${temp} degrees.} Development environment ready."

# Cache the last good neural render — offline boots replay it rather than
# dropping to the espeak robot voice.
cache=/var/tmp/ws-greeting-cache.mp3
spoken=0
if python3 -m edge_tts --voice en-GB-RyanNeural --rate=-5% \
        --text "$msg" --write-media /tmp/ws-greeting.mp3 2>/dev/null \
        && [ -s /tmp/ws-greeting.mp3 ] && command -v mpg123 >/dev/null; then
    cp /tmp/ws-greeting.mp3 "$cache" 2>/dev/null || true
    mpg123 -q /tmp/ws-greeting.mp3 && spoken=1
elif [ -s "$cache" ] && command -v mpg123 >/dev/null; then
    mpg123 -q "$cache" && spoken=1
fi
[ "$spoken" -eq 0 ] && command -v espeak-ng >/dev/null && espeak-ng -v en-gb "$msg"

command -v code >/dev/null && (code >/dev/null 2>&1 &)
exit 0
EOF
chmod 755 /usr/local/bin/workstation-greeting

install -d -m 755 -o "$REAL_USER" -g "$REAL_USER" "$REAL_HOME/.config/autostart"
cat > "$REAL_HOME/.config/autostart/flightsim-greeting.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Workstation Greeting
Exec=/usr/local/bin/workstation-greeting
X-GNOME-Autostart-enabled=true
EOF
chown "$REAL_USER:$REAL_USER" "$REAL_HOME/.config/autostart/flightsim-greeting.desktop"

install -m 755 "$SCRIPT_DIR/boot-agent.py" /usr/local/bin/boot-agent.py

# Scarlett keeps stale state across warm dual-boot reboots; reset it at
# boot so it enumerates without a physical replug.
install -m 755 "$SCRIPT_DIR/scarlett-reset.py" /usr/local/bin/scarlett-reset.py
cat > /etc/systemd/system/flightsim-scarlett-reset.service <<EOF
[Unit]
Description=USB-reset Focusrite Scarlett after dual-boot reboot
After=sound.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/bin/scarlett-reset.py

[Install]
WantedBy=multi-user.target
EOF
if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl enable flightsim-scarlett-reset.service >/dev/null 2>&1 || true
fi

cat > /usr/local/bin/boot-agent-http <<'EOF'
#!/bin/bash
exec python3 /usr/local/bin/boot-agent.py
EOF
chmod 755 /usr/local/bin/boot-agent-http
cat > /etc/systemd/system/flightsim-boot-agent.service <<EOF
[Unit]
Description=Local boot agent for flightsim
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/boot-agent-http
Restart=always
WorkingDirectory=/tmp

[Install]
WantedBy=multi-user.target
EOF
if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl enable --now flightsim-boot-agent.service >/dev/null 2>&1 || true
fi
if ! curl -sf --max-time 2 http://127.0.0.1:9108/status >/dev/null 2>&1; then
    nohup /usr/local/bin/boot-agent-http >/dev/null 2>&1 &
    sleep 1
fi

echo
if [ -n "$PUBKEY" ]; then
    echo "Done. Test from the Pi:  ssh -i ~/.ssh/flightsim_ed25519 $REAL_USER@192.168.100.1 boot"
    echo "(that reboots this machine into Windows immediately)"
else
    echo "Done. The boot helper is installed, but no Pi public key was supplied."
    echo "You can still test by adding the Pi key later with:"
    echo "  sudo ./linux/setup.sh '<pi public key>'"
fi
echo
echo "For a fully hands-free Linux boot (greeting + VS Code without touching"
echo "the keyboard), enable desktop auto-login for $REAL_USER in your display"
echo "manager (GNOME: /etc/gdm3/custom.conf -> AutomaticLoginEnable=true,"
echo "AutomaticLogin=$REAL_USER; or Settings > Users > Automatic Login)."
