#!/usr/bin/env bash
# efi_utils.sh — pure parsers over `efibootmgr` output, sourced by
# linux/set-boot-order.sh (and exercised by tests/test_efi_utils.sh).
#
# Why the firmware and not GRUB: only ONE of the two boots can steer GRUB.
# Linux can run grub-reboot; Windows cannot write ext4, so it has no way to
# pick a GRUB entry. That asymmetry is what made a cold "boot Windows"
# request land in Linux first — GRUB's saved default is Linux, and a
# powered-off machine has nobody left to change it. UEFI variables are the
# one boot-time switch BOTH sides can set (efibootmgr here, bcdedit over
# there), so the cold default lives there instead.
#
# efibootmgr prints:
#   BootCurrent: 0001
#   BootOrder: 0001,0000,0003
#   Boot0000* Windows Boot Manager	HD(1,GPT,...)/File(\EFI\Microsoft\Boot\bootmgfw.efi)
#   Boot0001* ubuntu	HD(1,GPT,...)/File(\EFI\ubuntu\shimx64.efi)

# Boot number of the Windows Boot Manager entry, or empty. Matched on the
# loader path first: the description is whatever the firmware vendor felt
# like writing, but bootmgfw.efi is Windows by definition.
extract_efi_windows_num() {
    local out="${1:-}"
    local num
    num="$(printf '%s\n' "$out" |
        grep -iP '^Boot([0-9A-Fa-f]{4})\*?\s.*bootmgfw\.efi' |
        head -1 | grep -oP '^Boot\K[0-9A-Fa-f]{4}' || true)"
    [ -n "$num" ] || num="$(printf '%s\n' "$out" |
        grep -iP '^Boot([0-9A-Fa-f]{4})\*?\s+Windows Boot Manager' |
        head -1 | grep -oP '^Boot\K[0-9A-Fa-f]{4}' || true)"
    printf '%s\n' "$num"
}

# Boot number of the entry that lands in GRUB, or empty. Anything naming
# Windows is excluded outright — "Windows Boot Manager" contains none of
# the Linux keywords, but a firmware that labels its entries oddly could
# still collide, and booting Windows when asked for Linux is the exact
# failure this file exists to remove.
extract_efi_linux_num() {
    local out="${1:-}"
    local pat
    # In priority order: the distro's own entry, then the loader path, then
    # the generic "UEFI OS" that a fallback-path GRUB install leaves behind.
    for pat in '\s(ubuntu|debian|fedora|grub|linux)\b' \
               '\s.*(shimx64|grubx64)\.efi' \
               '\sUEFI OS(\s|$)'; do
        local num
        num="$(printf '%s\n' "$out" |
            grep -iP "^Boot([0-9A-Fa-f]{4})\*?${pat}" |
            grep -viP 'windows|bootmgfw\.efi' |
            head -1 | grep -oP '^Boot\K[0-9A-Fa-f]{4}' || true)"
        if [ -n "$num" ]; then printf '%s\n' "$num"; return 0; fi
    done
    printf '\n'
}

# The existing BootOrder with $1 moved to the front, every other entry kept
# in its current relative order. Rewriting the order rather than replacing
# it matters: the list also holds the USB and network entries a recovery
# boot needs, and dropping them is a trip to the BIOS menu with a keyboard.
efi_boot_order_first() {
    local first="${1:-}" order="${2:-}"
    [ -n "$first" ] || { printf '%s\n' "$order"; return 0; }
    local out="$first" item
    local IFS=,
    for item in $order; do
        item="$(printf '%s' "$item" | tr -d '[:space:]')"
        [ -n "$item" ] || continue
        # Case-insensitive: firmware and efibootmgr disagree on hex case.
        [ "${item,,}" = "${first,,}" ] && continue
        out="$out,$item"
    done
    printf '%s\n' "$out"
}
