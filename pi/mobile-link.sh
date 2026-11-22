#!/usr/bin/env bash
# pi/mobile-link.sh — print the link that hands a phone the deck.
#
# Run ON THE PI:   sudo ./pi/mobile-link.sh
#
# The phone page (/m) needs DECK_TOKEN on every request. This prints
# http://<tailnet name>:<port>/m?token=… (or the https:// address, once
# pi/mobile-https.sh has run) once; the page stores the token and
# strips it from the address bar. Shown as a QR code too when qrencode is
# installed (sudo apt install qrencode), so it never has to be typed or sent.
# The link is a credential: do not paste it anywhere but the phone.
set -euo pipefail

CONF=/etc/flightsim/boot.env
[ -r "$CONF" ] || { echo "cannot read $CONF - run with sudo" >&2; exit 1; }
token="$(sed -n 's/^DECK_TOKEN=//p' "$CONF" | tail -1)"
port="$(sed -n 's/^DECK_PORT=//p' "$CONF" | tail -1)"
[ -n "$token" ] || { echo "no DECK_TOKEN in $CONF - run pi/setup-deck.sh" >&2; exit 1; }

# The tailnet name, so the link works from anywhere, not just the LAN.
host="$(tailscale status --json 2>/dev/null \
        | python3 -c 'import sys,json; print(json.load(sys.stdin)["Self"]["HostName"])' \
        2>/dev/null || hostname)"
url="http://${host,,}:${port:-8088}/m?token=${token}"
# On HTTPS when pi/mobile-https.sh has put the page there: that is the page a
# phone lends its microphone to.
https="$(sed -n 's/^DECK_HTTPS_URL=//p' "$CONF" | tail -1)"
[ -n "$https" ] && url="${https%/}/m?token=${token}"

echo "$url"
if command -v qrencode >/dev/null; then
    qrencode -t ansiutf8 "$url"
fi
