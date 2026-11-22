#!/usr/bin/env bash
# pi/mobile-https.sh — put the phone page on HTTPS, so that a phone will lend
# it its microphone.
#
# Run ON THE PI:   sudo ./pi/mobile-https.sh        (and `off` to undo it)
#
# A browser lets a page use the microphone only on HTTPS. `tailscale serve`
# gives this machine a real certificate for its tailnet name and forwards
# https://<name>.<tailnet>.ts.net to deck-api here. It is reachable from the
# tailnet only, as the plain page is; this is not Funnel.
#
# Two things to know before running it:
#   - HTTPS has to be enabled for the tailnet (admin console, DNS, HTTPS
#     Certificates), and issuing the certificate records this machine's
#     tailnet name in the public certificate transparency log.
#   - Everything served this way reaches deck-api from 127.0.0.1, the address
#     the panel is trusted on. deck-api knows a forwarded request by the
#     headers the proxy adds and asks it for DECK_TOKEN like any other. This
#     script checks that it does, and takes the HTTPS front down again if a
#     request without the token gets in.
set -euo pipefail

CONF=/etc/flightsim/boot.env
[ -w "$CONF" ] || { echo "cannot write $CONF - run with sudo" >&2; exit 1; }
port="$(sed -n 's/^DECK_PORT=//p' "$CONF" | tail -1)"
port="${port:-8088}"
name="$(tailscale status --json \
        | python3 -c 'import sys,json; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
[ -n "$name" ] || { echo "this machine has no tailnet name - is tailscale up?" >&2; exit 1; }

forget() { sed -i '/^DECK_HTTPS_URL=/d' "$CONF"; }

if [ "${1:-on}" = off ]; then
    tailscale serve --https=443 off || true
    forget
    systemctl restart deck-api
    echo "HTTPS front removed. The phone page is at http://${name%%.*}:$port/m, without the microphone."
    exit 0
fi

tailscale serve --bg --https=443 "http://127.0.0.1:$port"

# The certificate is issued on the first request, which can take a minute.
code=000
for _ in $(seq 1 20); do
    code="$(curl -s -o /dev/null -m 30 -w '%{http_code}' "https://$name/api/state" || true)"
    [ "$code" != 000 ] && break
    sleep 3
done
if [ "$code" != 403 ]; then
    tailscale serve --https=443 off || true
    forget
    echo "A request without the token got HTTP $code through the HTTPS front, where 403 was due." >&2
    echo "The front is down again. Is deck-api the version that asks forwarded requests for the token?" >&2
    exit 1
fi

forget
echo "DECK_HTTPS_URL=https://$name" >> "$CONF"
systemctl restart deck-api
echo "https://$name/m is up, and asks for the token like the plain page does."
echo "The plain page now offers it, and pi/mobile-link.sh prints its link."
