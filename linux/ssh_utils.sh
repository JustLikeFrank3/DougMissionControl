#!/usr/bin/env bash

add_restricted_authorized_key() {
    local auth_file="$1"
    local pubkey="$2"
    local command_path="$3"

    if [[ -z "$pubkey" ]]; then
        echo "No Pi public key supplied; skipping authorized_keys entry" >&2
        return 0
    fi

    install -d -m 700 "$(dirname "$auth_file")"
    touch "$auth_file"
    chmod 600 "$auth_file"

    local line="command=\"sudo ${command_path}\",restrict ${pubkey}"
    if ! grep -qF "$pubkey" "$auth_file"; then
        echo "$line" >> "$auth_file"
    fi
}
