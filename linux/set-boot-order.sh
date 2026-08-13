#!/usr/bin/env bash
# set-boot-order.sh — make a COLD boot land in Windows.
#
# Run on the workstation's Linux boot (linux/setup.sh calls it for you):
#
#   sudo ./linux/set-boot-order.sh            # apply
#   sudo ./linux/set-boot-order.sh --print    # show what it would do
#
# The problem it solves: with the machine powered off, nothing on the LAN
# can pick a boot target. The Pi can only send a magic packet, and the
# machine then boots whatever the last running OS left behind. GRUB's
# saved default is Linux, so "boot Windows" from cold booted LINUX, waited
# for it to come up, and only then rebooted into Windows — two boots, a
# GRUB countdown showing the wrong OS, and a real chance of blowing the
# Pi's POLL_SECS window.
#
# GRUB cannot be the answer, because only one of the two OSes can write to
# it: Windows has no ext4 writer, so it can neither run grub-reboot nor
# edit grubenv. The UEFI boot variables are the one switch both sides can
# throw — efibootmgr here, `bcdedit /set {fwbootmgr} bootsequence` there.
# So the COLD default moves into the firmware, pointed at Windows, and
# each OS one-shots the firmware when it hands over to the other:
#
#   cold           -> firmware BootOrder[0] = Windows Boot Manager -> Windows
#   Linux->Windows -> efibootmgr -n <windows>   (boot-to-windows)
#   Windows->Linux -> bcdedit bootsequence <ubuntu> -> GRUB -> Linux
#
# GRUB's own configuration is left completely alone: its saved default
# stays Linux, which is exactly what the Windows->Linux leg needs once the
# firmware has handed control to GRUB.
#
# Trade-off worth knowing: powering the machine on by hand at the desk now
# goes straight to Windows without showing the GRUB menu. To reach Linux
# that way, use the firmware's one-time boot menu (F8/F11/F12 depending on
# the board).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/efi_utils.sh"

PRINT_ONLY=0
[ "${1:-}" = "--print" ] && PRINT_ONLY=1

ENTRIES_FILE=/etc/flightsim/boot-entries.env

if [ ! -d /sys/firmware/efi ]; then
    echo "This machine booted in legacy BIOS mode, not UEFI - there are no" >&2
    echo "firmware boot variables to reorder. Leaving the GRUB saved default" >&2
    echo "in charge of cold boots (a cold 'boot Windows' will keep taking the" >&2
    echo "slow route through Linux)." >&2
    exit 2
fi

if ! command -v efibootmgr >/dev/null 2>&1; then
    if [ "$PRINT_ONLY" -eq 1 ]; then
        echo "efibootmgr is not installed (apt-get install efibootmgr)" >&2
        exit 2
    fi
    apt-get install -y -qq efibootmgr >/dev/null 2>&1 ||
        { echo "ERROR: could not install efibootmgr" >&2; exit 2; }
fi

OUT="$(efibootmgr)"
WIN_NUM="$(extract_efi_windows_num "$OUT")"
LINUX_NUM="$(extract_efi_linux_num "$OUT")"
ORDER="$(printf '%s\n' "$OUT" | sed -n 's/^BootOrder: *//p' | head -1)"

if [ -z "$WIN_NUM" ]; then
    echo "ERROR: no Windows Boot Manager entry in the firmware boot list." >&2
    echo "Nothing was changed. Full list:" >&2
    printf '%s\n' "$OUT" >&2
    exit 1
fi
# Not fatal on its own - the Windows leg is the one this script exists for -
# but the Pi cannot get BACK to Linux without it, so say so loudly.
if [ -z "$LINUX_NUM" ]; then
    echo "WARN: no GRUB/ubuntu entry found in the firmware boot list." >&2
    echo "      Windows will not be able to hand back to Linux." >&2
fi

NEW_ORDER="$(efi_boot_order_first "$WIN_NUM" "$ORDER")"

echo "Windows Boot Manager: Boot$WIN_NUM"
[ -n "$LINUX_NUM" ] && echo "GRUB / Linux:         Boot$LINUX_NUM"
echo "BootOrder now:        ${ORDER:-<unset>}"
echo "BootOrder wanted:     $NEW_ORDER"

if [ "$PRINT_ONLY" -eq 1 ]; then
    exit 0
fi

[ "$(id -u)" -eq 0 ] || { echo "run with sudo to apply" >&2; exit 1; }

if [ "$NEW_ORDER" != "$ORDER" ]; then
    efibootmgr -o "$NEW_ORDER" >/dev/null
    echo "BootOrder set - a cold power-on should now boot Windows directly."
    echo
    echo "CHECK THIS AFTER THE NEXT POWER CYCLE:  efibootmgr | head -3"
    echo "Many consumer boards keep their own boot priority list in NVRAM and"
    echo "re-sync BootOrder from it at POST, silently undoing the line above."
    echo "This one did. The symptom is a cold 'boot Windows' going through GRUB"
    echo "and Linux again, exactly as before. The fix is not here: set Boot"
    echo "Option #1 to Windows Boot Manager in the BIOS, which is the copy such"
    echo "a board actually honours. BootNext is unaffected either way, so both"
    echo "OS-to-OS legs keep working while the cold default is wrong."
else
    echo "BootOrder already Windows-first - nothing to change."
fi

# Recorded for boot-to-windows, which one-shots the firmware rather than
# GRUB once this ordering is in place.
install -d -m 755 /etc/flightsim
cat > "$ENTRIES_FILE" <<EOF
# Written by linux/set-boot-order.sh - firmware boot numbers, read by
# /usr/local/bin/boot-to-windows. Re-run that script if you reinstall
# either bootloader; the numbers change.
WIN_EFI_NUM=$WIN_NUM
LINUX_EFI_NUM=$LINUX_NUM
EOF
chmod 644 "$ENTRIES_FILE"
echo "Recorded in $ENTRIES_FILE"
