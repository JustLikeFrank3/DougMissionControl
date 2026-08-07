#!/usr/bin/env bash
# linux/seamless-displays.sh — stop a monitor input switch from tearing the
# desktop down. Run with sudo on the workstation's LINUX boot:
#
#   sudo ./linux/seamless-displays.sh            # apply
#   sudo ./linux/seamless-displays.sh --revert   # undo
#
# THE PROBLEM
#
# Sending a monitor to another input drops its hotplug line. The kernel sees
# the connector disconnect, userspace tears the output down, and when the
# monitor returns the driver brings it back at whatever mode it can prove is
# safe — 1024x768 on a panel whose EDID it no longer trusts. Switching away and
# back leaves a black screen or a 4:3 desktop. media-agent.py repairs this a few
# seconds later; this script stops it happening.
#
# THE FIX
#
# Save each connected monitor's EDID and hand it to the kernel as firmware,
# then force those connectors permanently enabled. The kernel stops asking the
# monitor whether it is there and stops caring when it says no — the mode
# survives the input switch because as far as DRM is concerned nothing changed.
# This is the same treatment a KVM switch needs, for the same reason.
#
# WHAT IT TOUCHES  (all reversible, and --revert does exactly this backwards)
#   * /lib/firmware/edid/<connector>.bin   — a copy of each monitor's own EDID
#   * /etc/initramfs-tools/hooks/flightdeck-edid — puts them in the initramfs,
#     because amdgpu probes before the root filesystem is mounted
#   * GRUB_CMDLINE_LINUX_DEFAULT           — video=<conn>:e and drm.edid_firmware
#   * a timestamped backup of /etc/default/grub
#
# IF IT GOES WRONG
#
# A wrong EDID means a display that does not come up. Recovery does not need a
# working screen to start from: at the GRUB menu press `e`, delete the
# video= and drm.edid_firmware= words from the linux line, and Ctrl-X to boot
# once without them. Then run this script with --revert.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

GRUB_DEFAULT_FILE=/etc/default/grub
EDID_DIR=/lib/firmware/edid
HOOK=/etc/initramfs-tools/hooks/flightdeck-edid
UPDATE_GRUB="update-grub"
command -v update-grub >/dev/null || UPDATE_GRUB="grub-mkconfig -o /boot/grub/grub.cfg"

# Strip any words this script previously added, leaving everything else alone.
strip_params() {
    sed -E 's/\bvideo=[^ "]*:e\b//g; s/\bdrm\.edid_firmware=[^ "]*//g; s/  +/ /g; s/ "/"/g'
}

if [ "${1:-}" = "--revert" ]; then
    cp "$GRUB_DEFAULT_FILE" "$GRUB_DEFAULT_FILE.bak.seamless.$(date +%s)"
    line="$(grep '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB_DEFAULT_FILE" | strip_params)"
    sed -i "s|^GRUB_CMDLINE_LINUX_DEFAULT=.*|$line|" "$GRUB_DEFAULT_FILE"
    rm -f "$HOOK"
    rm -rf "$EDID_DIR"
    $UPDATE_GRUB
    command -v update-initramfs >/dev/null && update-initramfs -u
    echo "Reverted. Reboot to return to the kernel's own display detection."
    exit 0
fi

# Connected connectors, by their DRM names — the same names ddcutil prints as
# "DRM connector" and the kernel expects in video= and drm.edid_firmware.
mapfile -t CONNECTED < <(
    for s in /sys/class/drm/card*-*/status; do
        [ -r "$s" ] || continue
        [ "$(cat "$s")" = "connected" ] || continue
        d="$(dirname "$s")"
        # "card1-HDMI-A-1" -> "HDMI-A-1"; the kernel wants it without the card.
        basename "$d" | sed -E 's/^card[0-9]+-//'
    done
)

[ "${#CONNECTED[@]}" -gt 0 ] || { echo "No connected displays found." >&2; exit 1; }

install -d -m 755 "$EDID_DIR"
params=""
firmware=""
for conn in "${CONNECTED[@]}"; do
    src="$(echo /sys/class/drm/card*-"$conn"/edid | awk '{print $1}')"
    if [ ! -s "$src" ]; then
        echo "WARN: $conn reports no EDID — skipping it (it will keep the old behaviour)"
        continue
    fi
    # A truncated EDID is worse than none: it would pin a mode the panel cannot
    # do. 128 bytes is one block, 256 with an extension.
    size="$(stat -c %s "$src")"
    if [ "$size" -lt 128 ]; then
        echo "WARN: $conn EDID is only ${size}B — too short to trust, skipping"
        continue
    fi
    lower="$(echo "$conn" | tr '[:upper:]' '[:lower:]')"
    cp "$src" "$EDID_DIR/$lower.bin"
    chmod 644 "$EDID_DIR/$lower.bin"
    echo "Saved $conn EDID (${size}B) -> $EDID_DIR/$lower.bin"
    params="$params video=$conn:e"
    firmware="${firmware:+$firmware,}$conn:edid/$lower.bin"
done

[ -n "$firmware" ] || { echo "No usable EDIDs captured; nothing changed." >&2; exit 1; }

# amdgpu is in the initramfs, so it asks for these before / is mounted.
cat > "$HOOK" <<'EOF'
#!/bin/sh
# Installed by linux/seamless-displays.sh — the saved EDIDs must be in the
# initramfs, because the GPU driver probes connectors before the root
# filesystem is available and would otherwise fall back to live detection.
PREREQ=""
prereqs() { echo "$PREREQ"; }
case "$1" in prereqs) prereqs; exit 0;; esac
. /usr/share/initramfs-tools/hook-functions
for f in /lib/firmware/edid/*.bin; do
    [ -e "$f" ] || continue
    add_firmware "edid/$(basename "$f")" 2>/dev/null \
        || { mkdir -p "$DESTDIR/lib/firmware/edid"; cp "$f" "$DESTDIR/lib/firmware/edid/"; }
done
exit 0
EOF
chmod 755 "$HOOK"

cp "$GRUB_DEFAULT_FILE" "$GRUB_DEFAULT_FILE.bak.seamless.$(date +%s)"
current="$(grep '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB_DEFAULT_FILE" | strip_params \
           | sed -E 's/^GRUB_CMDLINE_LINUX_DEFAULT="?//; s/"$//')"
newline="GRUB_CMDLINE_LINUX_DEFAULT=\"${current}${params} drm.edid_firmware=$firmware\""
# Collapse the double spaces a stripped-empty original leaves behind.
newline="$(echo "$newline" | sed -E 's/  +/ /g; s/=" /="/')"
if grep -q '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB_DEFAULT_FILE"; then
    sed -i "s|^GRUB_CMDLINE_LINUX_DEFAULT=.*|$newline|" "$GRUB_DEFAULT_FILE"
else
    echo "$newline" >> "$GRUB_DEFAULT_FILE"
fi
echo "Kernel command line is now:"
grep '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB_DEFAULT_FILE" | sed 's/^/    /'

$UPDATE_GRUB
command -v update-initramfs >/dev/null && update-initramfs -u

cat <<EOF

Done. Reboot for it to take effect.

After the reboot, switch a monitor to another input and back from SCREENS.
The desktop should not flinch: no black screen, no 1024x768, no windows
shuffled onto the surviving panel.

Check the kernel took the EDIDs:
    dmesg | grep -i "edid\|Got external EDID"
    cat /sys/class/drm/card*-*/status     # should stay "connected" throughout

media-agent.py's own repair stays in place and simply stops finding anything
to fix. Turn it off with MEDIA_NO_RESTORE=1 in /etc/dualboot/media-agent.env
once you trust this.

To undo:  sudo ./linux/seamless-displays.sh --revert
EOF
