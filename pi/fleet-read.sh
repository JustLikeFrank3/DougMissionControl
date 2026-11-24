#!/usr/bin/env bash
# pi/fleet-read.sh — the token that lets something see the fleet and touch
# nothing (Douglas), made here on the Pi.
#
# Run ON THE PI:   sudo ./pi/fleet-read.sh          make it, if there is none
#                  sudo ./pi/fleet-read.sh show     the two lines for Douglas
#                  sudo ./pi/fleet-read.sh off      take it away again
#
# DECK_READ_TOKEN goes into /etc/flightsim/boot.env beside DECK_TOKEN, and
# deck-api is restarted. It opens GET /api/fleet and no other route: not the
# state the panel is drawn from, not Douglas's conversation or memory, and
# nothing that boots, shuts down or changes anything.
#
# `show` prints what goes into /etc/douglas/voice.env on Douglas (FLEET_URL
# and FLEET_TOKEN); restart douglas-voice there afterwards, when nobody is
# talking to him. `off` removes the token here, which closes the route to
# whoever holds it; take the two lines out of voice.env as well.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
env=/etc/flightsim/boot.env
[ -f "$env" ] || { echo "there is no $env yet: run pi/setup-deck.sh first"; exit 1; }
have() { grep -q '^DECK_READ_TOKEN=.' "$env"; }

case "${1:-on}" in
off)
    sed -i '/^# Lets a reader see the fleet/d; /^DECK_READ_TOKEN=/d' "$env"
    systemctl restart deck-api
    echo "DECK_READ_TOKEN is out of $env, and /api/fleet is closed to whoever held it."
    ;;
show)
    have || { echo "there is no DECK_READ_TOKEN yet: run this without 'show' first"; exit 1; }
    port="$(sed -n 's/^DECK_PORT=//p' "$env" | tail -n1)"
    echo "# For /etc/douglas/voice.env on Douglas:"
    echo "FLEET_URL=http://$(hostname):${port:-8088}/api/fleet"
    echo "FLEET_TOKEN=$(sed -n 's/^DECK_READ_TOKEN=//p' "$env" | tail -n1)"
    ;;
on)
    if have; then
        echo "DECK_READ_TOKEN is in $env already."
    else
        # Made and written by one process: it is on no command line.
        python3 -I - "$env" <<'PY'
import secrets, sys
with open(sys.argv[1], "a") as f:
    f.write("\n# Lets a reader see the fleet (GET /api/fleet) and nothing else. For Douglas.\n"
            f"DECK_READ_TOKEN={secrets.token_urlsafe(32)}\n")
PY
        systemctl restart deck-api
        echo "DECK_READ_TOKEN is in $env now, and deck-api has been restarted."
    fi
    echo "Next: sudo $0 show   (the two lines for /etc/douglas/voice.env on Douglas)"
    ;;
*)
    echo "usage: $0 [show|off]"; exit 1 ;;
esac
