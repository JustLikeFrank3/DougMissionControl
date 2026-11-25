#!/usr/bin/env bash
# flightsim-state.sh windows|linux - exit 0 when that OS on the workstation
# answers, by any route this Pi has. fauxmo's state_cmd, so Alexa's "is it
# on" agrees with the deck.
#
# It used to ask WS_LAN alone, and the workstation's LAN port has no cable:
# the only cable is the one to this Pi. So the cable is asked too - Windows
# at WIN_DIRECT_IP, Linux at the LINUX_SSH address - with short timeouts,
# because a host on a direct cable answers in milliseconds or not at all.
set -u
CONF=/etc/flightsim/boot.env
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"
WS_LAN="${WS_LAN:-192.168.1.50}"
WIN_PORT="${WIN_PORT:-9106}"
WIN_AGENT_PORT="${WIN_AGENT_PORT:-9107}"
WIN_DIRECT_IP="${WIN_DIRECT_IP:-}"
LINUX_PORT="${LINUX_PORT:-9105}"
LINUX_SSH="${LINUX_SSH:-user@192.168.100.1}"
LINUX_PROBE_IP="${LINUX_PROBE_IP:-${LINUX_SSH#*@}}"

answers() { curl -sf --max-time "$2" "http://$1/" >/dev/null; }

case "${1:-windows}" in
    windows)
        answers "${WS_LAN}:${WIN_PORT}" 2 && exit 0
        [ -n "$WIN_DIRECT_IP" ] || exit 1
        answers "${WIN_DIRECT_IP}:${WIN_PORT}" 1 && exit 0
        timeout 1 bash -c "echo > /dev/tcp/${WIN_DIRECT_IP}/${WIN_AGENT_PORT}" 2>/dev/null
        ;;
    linux)
        answers "${WS_LAN}:${LINUX_PORT}" 2 && exit 0
        ping -c1 -W1 "$LINUX_PROBE_IP" >/dev/null 2>&1
        ;;
    *) echo "usage: $0 windows|linux" >&2; exit 2 ;;
esac
