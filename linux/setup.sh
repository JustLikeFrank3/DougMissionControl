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
#   * media-agent.py as a systemd USER unit on :9110 — the Linux half of the
#     MEDIA widget and the SCREENS surface (ddcutil, i2c-dev, the i2c group)
#   * a logon hook that hands the session's DISPLAY/XAUTHORITY to the user
#     manager, without which the agent cannot read desktop geometry and the
#     SCREENS cards lose their left-to-right order
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

# Shared token guarding the agent's /reboot, mirroring the Windows agent.
# Generated once and kept across re-runs so the Pi's copy stays valid.
install -d -m 755 /etc/flightsim
AGENT_TOKEN_FILE=/etc/flightsim/boot-agent.token
if [ ! -s "$AGENT_TOKEN_FILE" ]; then
    (umask 077; python3 -c 'import secrets; print(secrets.token_hex(16))' \
        > "$AGENT_TOKEN_FILE")
fi
chmod 600 "$AGENT_TOKEN_FILE"
AGENT_TOKEN="$(head -n1 "$AGENT_TOKEN_FILE")"

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
    systemctl enable flightsim-boot-agent.service >/dev/null 2>&1 || true
    # restart, not `enable --now`: a re-run must pick up the new agent code
    # rather than leaving the already-running old copy in place.
    systemctl restart flightsim-boot-agent.service >/dev/null 2>&1 || true
fi
if ! curl -sf --max-time 2 http://127.0.0.1:9108/status >/dev/null 2>&1; then
    nohup /usr/local/bin/boot-agent-http >/dev/null 2>&1 &
    sleep 1
fi

# --- media-agent: now-playing (MPRIS) + monitor input switching (DDC) -------
# The Linux half of the MEDIA widget and the SCREENS surface. deck-api polls
# whichever OS is up and both agents publish the identical shape, so the panel
# never learns which OS holds the video cables.
#
# This is a systemd USER unit, not a system one, and that is forced: playerctl
# reads MPRIS over the user's session bus, which a system unit has no route to.
# DDC then needs /dev/i2c-*, hence the i2c group on the same user.
apt-get install -y -qq ddcutil playerctl >/dev/null 2>&1 || true

# ddcutil talks to monitors over I2C; the bus nodes only exist once i2c-dev is
# loaded, and it is not autoloaded on most desktops.
modprobe i2c-dev >/dev/null 2>&1 || true
echo i2c-dev > /etc/modules-load.d/flightsim-ddc.conf

# The ddcutil package usually creates this group; create it if it did not, or
# the usermod below fails and DDC silently returns nothing.
getent group i2c >/dev/null || groupadd -r i2c
usermod -aG i2c "$REAL_USER"

install -m 755 "$SCRIPT_DIR/media-agent.py" /usr/local/bin/media-agent.py

# Same token discipline as the boot agent: minted once, preserved across
# re-runs so the Pi's copy stays valid. Owned by the desktop user because the
# user unit reads it as that user — 600, not the 644 the docstring once said.
install -d -m 755 /etc/dualboot
MEDIA_TOKEN_FILE=/etc/dualboot/media-agent.token
if [ ! -s "$MEDIA_TOKEN_FILE" ]; then
    (umask 077; python3 -c 'import secrets; print(secrets.token_hex(16))' \
        > "$MEDIA_TOKEN_FILE")
fi
chown "$REAL_USER:$REAL_USER" "$MEDIA_TOKEN_FILE"
chmod 600 "$MEDIA_TOKEN_FILE"
MEDIA_TOKEN="$(head -n1 "$MEDIA_TOKEN_FILE")"

USER_UNIT_DIR="$REAL_HOME/.config/systemd/user"
install -d -m 755 -o "$REAL_USER" -g "$REAL_USER" "$USER_UNIT_DIR"
cat > "$USER_UNIT_DIR/flightsim-media-agent.service" <<EOF
[Unit]
Description=Flight Deck media + monitor agent (MPRIS now-playing, DDC input switching)
After=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/media-agent.py
Restart=always
RestartSec=2
# Left-to-right monitor order, for compositors that will not report desktop
# geometry — GNOME on Wayland has no CLI for it. xrandr, wlr-randr and swaymsg
# are all tried first, so this is only consulted when none of them answer.
EnvironmentFile=-/etc/dualboot/media-agent.env

[Install]
WantedBy=default.target
EOF

# Created empty, and never overwritten: it holds an operator statement about
# the physical desk that this script has no way to discover.
if [ ! -f /etc/dualboot/media-agent.env ]; then
    cat > /etc/dualboot/media-agent.env <<'EOF'
# Left-to-right order of the monitors on the desk, by connector name as
# `ddcutil detect --brief` reports it under "DRM connector" (card1- prefix
# optional). Only used when no display server will give desktop geometry.
#
#   MON_ORDER=hdmi1,dp1
EOF
fi
chown "$REAL_USER:$REAL_USER" "$USER_UNIT_DIR/flightsim-media-agent.service"

# Linger so the agent is up before anyone logs in: SCREENS should work from
# the greeter. With no session there is no MPRIS, which the agent already
# reports as {"active": false} — the widget reads that as "no source", not an
# error, so this degrades exactly the way it should.
loginctl enable-linger "$REAL_USER" >/dev/null 2>&1 || true

# ...but lingering has a cost the agent cannot pay by itself: a unit started at
# boot never inherits DISPLAY or XAUTHORITY, so xrandr cannot authenticate and
# the agent gets no desktop geometry. It then cannot say which panel is on the
# left, the SCREENS cards fall back to ddcutil's bus order, and on a desk where
# that order does not match the desk they come out reversed.
#
# Push the session's own variables into the user manager at login and restart
# the agent so it picks them up. Same idea as the import-environment line most
# desktops run for their own units.
cat > /usr/local/bin/flightdeck-media-env <<'EOF'
#!/bin/bash
# Hand the graphical session's environment to the user manager, then restart
# the media agent so its xrandr/wlr-randr probes can reach the display server.
systemctl --user import-environment DISPLAY XAUTHORITY WAYLAND_DISPLAY 2>/dev/null || true
systemctl --user restart flightsim-media-agent.service 2>/dev/null || true
EOF
chmod 755 /usr/local/bin/flightdeck-media-env
cat > "$REAL_HOME/.config/autostart/flightdeck-media-env.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Flight Deck media agent session environment
Exec=/usr/local/bin/flightdeck-media-env
X-GNOME-Autostart-enabled=true
EOF
chown "$REAL_USER:$REAL_USER" "$REAL_HOME/.config/autostart/flightdeck-media-env.desktop"

REAL_UID="$(id -u "$REAL_USER")"
if command -v systemctl >/dev/null 2>&1; then
    sudo -u "$REAL_USER" XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
        systemctl --user daemon-reload >/dev/null 2>&1 || true
    sudo -u "$REAL_USER" XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
        systemctl --user enable flightsim-media-agent.service >/dev/null 2>&1 || true
    # restart rather than `enable --now`, so a re-run picks up new agent code
    sudo -u "$REAL_USER" XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
        systemctl --user restart flightsim-media-agent.service >/dev/null 2>&1 || true
fi

echo
echo "Linux boot-agent token — add this line to /etc/flightsim/boot.env on the Pi,"
echo "or the Pi falls back to the ssh key for every reboot request:"
echo "    LINUX_AGENT_TOKEN=$AGENT_TOKEN"
echo
echo "Linux media/monitor agent token — add this line to boot.env on the Pi too,"
echo "or MEDIA and SCREENS stay dark under Linux (deck-api skips the agent"
echo "entirely when the token is unset):"
echo "    LINUX_MEDIA_TOKEN=$MEDIA_TOKEN"
echo
echo "Verify DDC before trusting SCREENS:"
echo "    python3 /usr/local/bin/media-agent.py --monitors"
echo "Expect one entry per monitor with \"ddc\": true. An empty list means"
echo "ddcutil reached no I2C bus — group membership does not apply to sessions"
echo "that were already open, so log out and back in (or reboot) and re-check."
echo
echo "Then check what the SERVICE serves, which is what SCREENS actually sees:"
echo "    curl -s \"http://127.0.0.1:9110/monitor?token=\$(sudo cat $MEDIA_TOKEN_FILE)\""
echo "The two can disagree: your shell has DISPLAY and XAUTHORITY, a unit"
echo "started at boot does not. If \"position\" is empty there, the agent got no"
echo "desktop geometry, could not tell left from right, and the cards fall back"
echo "to ddcutil's bus order — which is how they come out reversed. Logging out"
echo "and back in now installs the fix; failing that, state the order yourself:"
echo "    echo 'MON_ORDER=dp1,hdmi1' | sudo tee -a /etc/dualboot/media-agent.env"
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
