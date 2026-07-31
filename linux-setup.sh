#!/usr/bin/env bash
# linux-setup.sh — one-time prep on the workstation's LINUX boot so the Pi
# can steer the next boot into Windows. Run with sudo, passing the Pi's
# public key (printed by pi-setup.sh):
#
#   sudo ./scripts/flightsim/linux-setup.sh 'ssh-ed25519 AAAA... flightsim-boot@pi'
#
# Installs:
#   * GRUB_DEFAULT=saved (grub-reboot needs it; saved default is pinned to
#     the CURRENT default entry, so day-to-day boot behavior is unchanged)
#   * /usr/local/bin/boot-to-windows — grub-reboot <Windows entry> + reboot
#   * sudoers rule: invoking user may run exactly that script, nothing else
#   * authorized_keys entry for the Pi key, command=-forced to that script
#   * WOL persistence: ethtool wol g on the wired NIC at boot (the Linux
#     driver otherwise disarms WOL, breaking wake-after-Linux-shutdown)
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
PUBKEY="${1:-}"
[ -n "$PUBKEY" ] || { echo "usage: sudo $0 '<pi public key>'" >&2; exit 1; }

REAL_USER="${SUDO_USER:?run via sudo, not a root login}"
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
WS_MAC="${WS_MAC:-aa:bb:cc:dd:ee:ff}"

GRUB_CFG=/boot/grub/grub.cfg
[ -f "$GRUB_CFG" ] || GRUB_CFG=/boot/grub2/grub.cfg
UPDATE_GRUB="update-grub"
command -v update-grub >/dev/null || UPDATE_GRUB="grub-mkconfig -o $GRUB_CFG"

WIN_ENTRY="$(grep -oP "menuentry '\K[^']*Windows[^']*" "$GRUB_CFG" | head -1 || true)"
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
# Installed by scripts/flightsim/linux-setup.sh — one-shot reboot into
# Windows, invoked by the Pi's forced-command ssh key.
set -e
grub-reboot "$WIN_ENTRY"
shutdown -r +0 "Rebooting into Windows (flight sim bootup)"
EOF
chmod 755 /usr/local/bin/boot-to-windows

echo "$REAL_USER ALL=(root) NOPASSWD: /usr/local/bin/boot-to-windows" \
    > /etc/sudoers.d/flightsim
chmod 440 /etc/sudoers.d/flightsim

# Forced command + restrict: this key can reboot-to-Windows and do nothing
# else — no shell, no forwarding, regardless of what the client requests.
AK="$REAL_HOME/.ssh/authorized_keys"
install -d -m 700 -o "$REAL_USER" -g "$REAL_USER" "$REAL_HOME/.ssh"
touch "$AK"; chown "$REAL_USER:$REAL_USER" "$AK"; chmod 600 "$AK"
LINE="command=\"sudo /usr/local/bin/boot-to-windows\",restrict $PUBKEY"
grep -qF "$PUBKEY" "$AK" || echo "$LINE" >> "$AK"

# Keep WOL armed after a Linux shutdown (driver default often disarms it).
IFACE="$(ip -o link | grep -i "$WS_MAC" | awk -F': ' '{print $2}' | head -1)"
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
    systemctl daemon-reload
    systemctl enable --now flightsim-wol.service
    echo "WOL armed on $IFACE (persisted via flightsim-wol.service)"
else
    echo "WARN: no interface with MAC $WS_MAC found — arm WOL manually (ethtool -s <iface> wol g)"
fi

echo
echo "Done. Test from the Pi:  ssh -i ~/.ssh/flightsim_ed25519 $REAL_USER@192.168.100.1 boot"
echo "(that reboots this machine into Windows immediately)"
