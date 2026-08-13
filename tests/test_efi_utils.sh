#!/usr/bin/env bash
# Parsing efibootmgr output, which decides which firmware entry a cold boot
# lands in. Getting the Windows number wrong here reorders the boot list
# around the wrong entry, and the machine boots something unexpected with
# nobody at the keyboard - so every shape efibootmgr emits is pinned.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$repo_dir/linux/efi_utils.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
is() { [ "$2" = "$3" ] || fail "$1: expected '$3', got '$2'"; }

typical="$(cat <<'EOF'
BootCurrent: 0001
Timeout: 1 seconds
BootOrder: 0001,0000,0003,0002
Boot0000* Windows Boot Manager	HD(1,GPT,aaaa,0x800,0x82000)/File(\EFI\Microsoft\Boot\bootmgfw.efi)
Boot0001* ubuntu	HD(1,GPT,aaaa,0x800,0x82000)/File(\EFI\ubuntu\shimx64.efi)
Boot0002* UEFI: Samsung SSD 990 PRO	HD(1,GPT,aaaa,0x800,0x82000)
Boot0003* UEFI: PXE IPv4 Intel(R) Ethernet	PciRoot(0x0)/Pci(0x1c,0x0)
EOF
)"

is "windows num" "$(extract_efi_windows_num "$typical")" "0000"
is "linux num"   "$(extract_efi_linux_num   "$typical")" "0001"

# The reorder keeps every other entry, in order: the USB and PXE entries
# are the way back in when a boot goes wrong, and silently dropping them
# turns a bad boot into a trip to the BIOS with a keyboard.
is "reorder" "$(efi_boot_order_first 0000 '0001,0000,0003,0002')" "0000,0001,0003,0002"
is "reorder idempotent" \
    "$(efi_boot_order_first 0000 '0000,0001,0003,0002')" "0000,0001,0003,0002"
# Firmware and efibootmgr disagree on hex case; a case-blind compare would
# leave the entry in the list twice and efibootmgr rejects duplicates.
is "reorder case-insensitive" "$(efi_boot_order_first 000a '0001,000A')" "000a,0001"
is "reorder with no order set" "$(efi_boot_order_first 0000 '')" "0000"

# A description-free firmware: the loader path still identifies both sides.
pathonly="$(cat <<'EOF'
BootOrder: 0002,0001
Boot0001* HD(1,GPT,aaaa)/File(\EFI\Microsoft\Boot\bootmgfw.efi)
Boot0002* HD(1,GPT,aaaa)/File(\EFI\ubuntu\grubx64.efi)
EOF
)"
is "windows by path" "$(extract_efi_windows_num "$pathonly")" "0001"
is "linux by path"   "$(extract_efi_linux_num   "$pathonly")" "0002"

# A GRUB installed to the removable fallback path shows up under the
# firmware's own generic label. Second choice, never first - and never the
# Windows entry, whatever it is labelled.
fallback="$(cat <<'EOF'
BootOrder: 0000,0005
Boot0000* Windows Boot Manager	HD(1,GPT,aaaa)/File(\EFI\Microsoft\Boot\bootmgfw.efi)
Boot0005* UEFI OS	HD(1,GPT,aaaa)/File(\EFI\BOOT\BOOTX64.EFI)
EOF
)"
is "linux via UEFI OS" "$(extract_efi_linux_num "$fallback")" "0005"

# Windows-only machine: no Linux entry to find, and saying so is what lets
# set-boot-order.sh warn instead of pointing the firmware at nothing.
winonly="$(printf 'BootOrder: 0000\nBoot0000* Windows Boot Manager\tHD(1,GPT,aaaa)/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)\n')"
is "no linux entry" "$(extract_efi_linux_num "$winonly")" ""
is "no windows entry" "$(extract_efi_windows_num 'BootOrder: 0000')" ""

echo "efi utils tests passed"
