#!/usr/bin/env bash
# pi/push-setup.sh — let node1 push a notice or a waiting yes to the phone app.
#
# Run ON THE PI:
#   sudo ./pi/push-setup.sh AuthKey_ABC123DEFG.p8 ABC123DEFG TEAMID1234 [bundle id]
#   sudo ./pi/push-setup.sh off
#
# The phone app (phone/) is told about a new warning or critical notice, a
# change of Douglas's that is waiting for a yes, and a machine that has
# stopped answering, when the app is closed. That goes through Apple's push
# service, which wants a key from the developer account:
#
#   developer.apple.com > Certificates, Identifiers & Profiles > Keys > +
#   tick "Apple Push Notifications service (APNs)", download the .p8 (once),
#   and note the Key ID. The Team ID is under Membership details.
#
# Copy the .p8 to node1 (scp), then run this with the file, the Key ID, the
# Team ID and, if it is not the default, the app's bundle identifier (the
# one in phone/app.json). The key is moved to /etc/flightsim/apns.p8,
# readable by deck-api's user and nobody else; the four settings go into
# boot.env; deck-api is restarted. `off` takes all of it away again.
#
# What is sent through Apple: the notice's own sentence. Of a change waiting
# for a yes, only its kind. Nothing is sent until a phone has registered
# itself, from the app.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
env=/etc/flightsim/boot.env
key=/etc/flightsim/apns.p8
[ -f "$env" ] || { echo "there is no $env yet: run pi/setup-deck.sh first"; exit 1; }
forget() { sed -i '/^# Apple.s push service, for the phone app/d; /^APNS_KEY_FILE=/d; /^APNS_KEY_ID=/d; /^APNS_TEAM_ID=/d; /^APNS_TOPIC=/d' "$env"; }

if [ "${1:-}" = off ]; then
    forget
    rm -f "$key"
    systemctl restart deck-api
    echo "The push key and its settings are gone from node1. Revoke the key at developer.apple.com to end it there too."
    exit 0
fi

[ $# -ge 3 ] || { sed -n '3,6p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
src="$1"; kid="$2"; team="$3"; topic="${4:-com.dougmissioncontrol.phone}"
[ -f "$src" ] || { echo "no such file: $src"; exit 1; }
grep -q 'BEGIN PRIVATE KEY' "$src" || { echo "$src is not a .p8 key from Apple"; exit 1; }
case "$kid$team" in *[!A-Z0-9]*) echo "the Key ID and Team ID are ten capital letters and digits each"; exit 1 ;; esac
command -v openssl >/dev/null || { echo "openssl is not installed (sudo apt install openssl)"; exit 1; }
curl --version | grep -q HTTP2 || { echo "this curl cannot speak HTTP/2, which Apple requires (sudo apt install curl)"; exit 1; }

user="$(systemctl show -p User --value deck-api 2>/dev/null)"
[ -n "$user" ] || { echo "cannot tell which user deck-api runs as: is it installed? (pi/setup-deck.sh)"; exit 1; }
install -o "$user" -g "$user" -m 600 "$src" "$key"
forget
{
    echo "# Apple's push service, for the phone app (pi/push-setup.sh)."
    echo "APNS_KEY_FILE=$key"
    echo "APNS_KEY_ID=$kid"
    echo "APNS_TEAM_ID=$team"
    echo "APNS_TOPIC=$topic"
} >> "$env"
systemctl restart deck-api
echo "node1 can push to $topic now. The key is at $key; remove your copy of $src."
echo "In the app: NOTICES ON THIS PHONE > TURN ON, then SEND A TEST."
