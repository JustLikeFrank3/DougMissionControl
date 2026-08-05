#!/usr/bin/env bash

extract_windows_grub_entry() {
    local grub_cfg="${1:-}"
    local entry=""

    if [[ -z "$grub_cfg" ]]; then
        return 1
    fi

    if [[ -f "$grub_cfg" ]]; then
        entry="$(grep -oP "menuentry ['\"]\K[^'\"]*(?=['\"])" "$grub_cfg" | grep -i "windows" | head -1 || true)"
    fi

    if [[ -z "$entry" ]]; then
        entry="$(grep -oP "menuentry '\K[^']*Windows[^']*" "$grub_cfg" | head -1 || true)"
    fi

    if [[ -z "$entry" ]]; then
        entry="$(grep -oP 'menuentry "\K[^"]*Windows[^"]*' "$grub_cfg" | head -1 || true)"
    fi

    printf '%s\n' "$entry"
}
