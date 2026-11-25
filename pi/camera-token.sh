#!/usr/bin/env bash
# pi/camera-token.sh — the token for Douglas's camera tool, made here on the Pi.
#
# Run ON THE PI:   sudo ./pi/camera-token.sh          make it, if there is none
#                  sudo ./pi/camera-token.sh lines    the two lines for Douglas, on stdout
#                  sudo ./pi/camera-token.sh off      take it away again
#
# DECK_CAMERA_TOKEN goes into /etc/flightsim/boot.env beside DECK_TOKEN and
# DECK_READ_TOKEN, and deck-api is restarted. It opens GET /api/eyes/cameras
# and POST /api/eyes/image and nothing else: not the deck, not the fleet, not
# the owner's camera switches. DougRing still decides every picture.
#
# `lines` writes CAMERA_URL and CAMERA_TOKEN for /etc/douglas/voice.env on
# Douglas. Pipe it there rather than printing it, so the token is on no
# screen and no command line:
#   ssh fvm3@node1 'sudo ~/dougmissioncontrol/pi/camera-token.sh lines' \
#     | ssh fvm3@douglas 'sudo sh -c "cat >> /etc/douglas/voice.env"'
# then restart douglas-voice there when nobody is talking to him.
# `off` removes the token here, which closes both routes at once.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
env=/etc/flightsim/boot.env
[ -f "$env" ] || { echo "there is no $env yet: run pi/setup-deck.sh first"; exit 1; }
have() { grep -q '^DECK_CAMERA_TOKEN=.' "$env"; }

case "${1:-on}" in
off)
    sed -i '/^# Lets Douglas look through the cameras/d; /^DECK_CAMERA_TOKEN=/d' "$env"
    systemctl restart deck-api
    echo "DECK_CAMERA_TOKEN is out of $env: Douglas's camera routes are closed."
    ;;
lines)
    have || { echo "there is no DECK_CAMERA_TOKEN yet: run this without 'lines' first" >&2; exit 1; }
    port="$(sed -n 's/^DECK_PORT=//p' "$env" | tail -n1)"
    echo ""
    echo "# The Ring cameras, through Mission Control on the Pi (pi/camera-token.sh)."
    echo "CAMERA_URL=http://$(hostname | tr 'A-Z' 'a-z'):${port:-8088}/api/eyes"
    echo "CAMERA_TOKEN=$(sed -n 's/^DECK_CAMERA_TOKEN=//p' "$env" | tail -n1)"
    ;;
on)
    if have; then
        echo "DECK_CAMERA_TOKEN is in $env already."
    else
        python3 -I - "$env" <<'PY'
import secrets, sys
with open(sys.argv[1], "a") as f:
    f.write("\n# Lets Douglas look through the cameras DougRing has switched on (/api/eyes) and nothing else.\n"
            f"DECK_CAMERA_TOKEN={secrets.token_urlsafe(32)}\n")
PY
        systemctl restart deck-api
        echo "DECK_CAMERA_TOKEN is in $env now, and deck-api has been restarted."
    fi
    ;;
*)
    echo "usage: $0 [lines|off]"; exit 1 ;;
esac
