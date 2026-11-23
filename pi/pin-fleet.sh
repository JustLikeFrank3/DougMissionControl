#!/usr/bin/env bash
# pi/pin-fleet.sh — pin Douglas's and DougVoice's tailnet addresses in this
# Pi's hosts file, so that the deck finds them without asking DNS.
#
# Run ON THE PI:   sudo ./pi/pin-fleet.sh        (and `off` to undo it)
#
# deck-api looks both machines up by short name (DOUGLAS_HOST and VOICE_HOST
# in boot.env) every few seconds. A short name resolves only while
# /etc/resolv.conf is the one Tailscale wrote. NetworkManager rewrites that
# file whenever the Wi-Fi rejoins and Tailscale writes it back, but when the
# Wi-Fi rejoins several times in a row Tailscale can lose the last round. The
# names then stop resolving, and the deck shows two machines OFFLINE that are
# up and answering. A name in /etc/hosts is answered before DNS is asked, so
# it cannot be lost that way.
#
# Three things to know:
#   - The addresses come from Tailscale itself (`tailscale ip -4 <name>`):
#     nothing is typed, and no address is kept in this repository.
#   - A machine that is removed from the tailnet and added again gets a new
#     address, and the deck shows it OFFLINE until this is run again.
#   - Where cloud-init rebuilds /etc/hosts at every boot (manage_etc_hosts,
#     as on Raspberry Pi OS trixie), the same lines go into its template:
#     without that the pin would last until the next restart.
set -euo pipefail

HOSTS="${PIN_HOSTS:-/etc/hosts}"
TEMPLATE="${PIN_TEMPLATE:-/etc/cloud/templates/hosts.debian.tmpl}"
CONF="${PIN_CONF:-/etc/flightsim/boot.env}"
BEGIN="# >>> flight deck: fleet machines by their Tailscale addresses (pi/pin-fleet.sh)"
END="# <<< flight deck"

[ -w "$HOSTS" ] || { echo "cannot write $HOSTS - run with sudo" >&2; exit 1; }

new=""
trap '[ -z "$new" ] || rm -f "$new"' EXIT

setting() {     # the last KEY= line of boot.env, without its quotes
    [ -r "$CONF" ] && sed -n "s/^$1=//p" "$CONF" | tail -1 | tr -d "\"'" || true
}

address() {     # a machine's tailnet IPv4 as Tailscale has it, or nothing
    local a
    a="$(tailscale ip -4 "$1" 2>/dev/null | head -1)" || true
    [[ "$a" =~ ^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}$ ]] && echo "$a"
}

whole() {       # the file holds one whole block, or none
    local b e
    b="$(grep -cxF -e "$BEGIN" "$1" || true)"
    e="$(grep -cxF -e "$END" "$1" || true)"
    [ "$b" = "$e" ] && [ "$b" -le 1 ]
}

without() {     # the file's text, without the block
    awk -v b="$BEGIN" -v e="$END" '$0 == b {skip = 1} !skip {print} $0 == e {skip = 0}' "$1"
}

put() {         # file, block ("" for none): true if the file had to change
    local file="$1" block="$2"
    new="$(mktemp "$file.XXXXXX")"
    # awk ends the last line it prints, so the block starts on a line of its
    # own even after a file that had no newline at its end.
    without "$file" > "$new"
    [ -z "$block" ] || printf '%s\n' "$block" >> "$new"
    if cmp -s "$new" "$file"; then
        rm -f "$new"; new=""
        return 1
    fi
    chmod --reference="$file" "$new"
    chown --reference="$file" "$new" 2>/dev/null || true
    # Swapped in whole, so nothing ever reads half a hosts file.
    mv -f "$new" "$file" 2>/dev/null || { cat "$new" > "$file"; rm -f "$new"; }
    new=""
}

if [ "${1:-on}" = off ]; then
    gone=0
    for f in "$HOSTS" "$TEMPLATE"; do
        [ -f "$f" ] && grep -qxF -e "$BEGIN" "$f" || continue
        whole "$f" || { echo "$f holds a pin that is not whole: put it right by hand." >&2; exit 1; }
        [ -w "$f" ] || { echo "cannot write $f - run with sudo" >&2; exit 1; }
        if put "$f" ""; then gone=1; fi
    done
    if [ "$gone" = 1 ]; then
        echo "The pins are gone. The deck finds both machines through DNS again."
    else
        echo "There were no pins to remove."
    fi
    exit 0
fi

# cloud-init says so in the hosts file it writes; its template is the master then.
files=("$HOSTS")
if [ -f "$TEMPLATE" ] && grep -q "manage_etc_hosts" "$HOSTS"; then
    [ -w "$TEMPLATE" ] || { echo "cannot write $TEMPLATE - run with sudo" >&2; exit 1; }
    files+=("$TEMPLATE")
fi
for f in "${files[@]}"; do
    whole "$f" || { echo "$f holds a pin that is not whole: put it right by hand." >&2; exit 1; }
done

names=()
for pair in DOUGLAS_HOST:douglas VOICE_HOST:dougvoice; do
    name="$(setting "${pair%%:*}")"
    name="${name:-${pair##*:}}"
    if [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9-]*$ ]]; then
        names+=("$name")
    else
        echo "${pair%%:*} is not a short name, so there is nothing to pin for it."
    fi
done
[ "${#names[@]}" -gt 0 ] || { echo "Nothing to pin."; exit 0; }

# Every address first, then the files: a machine Tailscale does not know
# leaves everything as it was.
declare -A at
block="$BEGIN
# The deck finds these by name. Pinned here so that it does not depend on DNS.
# After a machine is added to the tailnet again: sudo ./pi/pin-fleet.sh"
for n in "${names[@]}"; do
    a="$(address "$n")" || {
        echo "Tailscale does not know a machine called $n (is tailscale up?). Nothing was changed." >&2
        exit 1
    }
    at[$n]="$a"
    block+=$'\n'"$a $n"
done
block+=$'\n'"$END"

changed=0
for f in "${files[@]}"; do
    if put "$f" "$block"; then changed=1; fi
done

for n in "${names[@]}"; do printf '%-10s %s\n' "$n" "${at[$n]}"; done
where="$HOSTS"
[ "${#files[@]}" -eq 1 ] || where+=" and in cloud-init's template, which rebuilds it at every boot"
if [ "$changed" = 1 ]; then
    echo "Pinned in $where. Nothing needs restarting."
else
    echo "Already pinned in $where, at the addresses Tailscale gives. Nothing was changed."
fi

# On the real file, ask the way deck-api does: a pin that is not the answer is no pin.
if [ "$HOSTS" = /etc/hosts ]; then
    for n in "${names[@]}"; do
        got="$(getent hosts "$n" | awk '{print $1; exit}')"
        [ "$got" = "${at[$n]}" ] && continue
        echo "$n is pinned, but this machine answers ${got:-nothing} for it." >&2
        echo "Is 'files' first on the hosts line of /etc/nsswitch.conf?" >&2
        exit 1
    done
fi
