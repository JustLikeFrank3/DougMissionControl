#!/bin/bash
# flightdeck-kiosk.sh — launch Flight Deck as a client of the EXISTING
# labwc session, in place of wallboard-kiosk.sh.
#
# Derived directly from /usr/local/bin/wallboard-kiosk.sh (preserved verbatim
# alongside this file). Everything that script learned the hard way is kept:
#
#   * the single-instance flock — SAME lock path, so this and the original
#     are mutually exclusive by construction and can never both be running.
#     Relaunching without it once bred "4 loops x 68 chromium processes and
#     starved the Pi" (2026-07-20 incident).
#   * localhost, never a LAN IP — a DHCP lease change stranded the kiosk
#     before (2026-07-15 incident).
#   * wlrctl pointer parking every 30 s, so a bumped mouse gets ~30 s of
#     fame and then hides.
#   * a three-strike health check that restarts chromium.
#
# What changes: chromium points at Flight Deck instead of directly at a
# Grafana playlist. Flight Deck's EVALS surface then frames the very same
# playlist, and deck-api does the OS->playlist selection — reproducing this
# script's semantics, including that an indeterminate probe must NOT flap
# the display. The playlist is still resolved by NAME, never a baked uid.
#
# Grafana, Prometheus, k3s and every dashboard remain jobContext's and are
# untouched. Requires [security] allow_embedding = true on Grafana, or the
# EVALS frame is refused by X-Frame-Options.
set -u

exec 9>"/run/user/$(id -u)/wallboard-kiosk.lock"
flock -n 9 || { echo "a wallboard/flight-deck kiosk is already running"; exit 0; }

DECK="${DECK_URL:-http://127.0.0.1:8088}"
G="${GRAFANA_URL:-http://localhost:3000}"

# Wait for both before showing anything: deck-api draws the console, Grafana
# fills the EVALS surface. A kiosk that opens on a connection error and stays
# there is worse than one that opens ten seconds later.
until curl -sf -o /dev/null --max-time 3 "$DECK/api/state"; do sleep 5; done
# No Grafana gate. That wait was inherited from wallboard-kiosk.sh, which
# pointed chromium AT Grafana and had nothing to show without it. Flight Deck
# owns its surfaces: EVALS renders an honest "unreachable" overlay and heals
# itself when Grafana arrives — k3s takes minutes on a cold boot, and hiding
# the entire deck for one surface's backend read as a dead panel.

while true; do
    chromium \
        --password-store=basic \
        --ozone-platform=wayland \
        --disable-features=OverlayScrollbar \
        --kiosk \
        --noerrdialogs \
        --disable-infobars \
        --disable-session-crashed-bubble \
        "$DECK/" &
    # --ozone-platform on the command line, NOT trusted to the profile: this
    # session is Wayland-only, and the flag used to live in chromium's Local
    # State as a chrome://flags preference — so a wiped or fresh profile made
    # chromium try X11 and die with "Missing X server or $DISPLAY". A kiosk
    # must survive losing its profile.
    # OverlayScrollbar off: the auto-hiding slivers are unfindable by finger,
    # and the Grafana iframe is cross-origin so no page CSS can widen them —
    # only the browser can. Classic always-visible bars everywhere instead.
    CHROME_PID=$!

    fails=0
    while kill -0 "$CHROME_PID" 2>/dev/null; do
        sleep 30
        wlrctl pointer move 9999 9999 2>/dev/null
        if curl -sf -o /dev/null --max-time 5 "$DECK/api/state"; then
            fails=0
        else
            fails=$((fails + 1))
        fi
        # No playlist swap here any more — deck-api owns which board is shown
        # and the page follows over SSE, so an OS flip no longer costs a
        # chromium restart.
        if [ "$fails" -ge 3 ]; then
            kill "$CHROME_PID" 2>/dev/null
            wait "$CHROME_PID" 2>/dev/null
            break
        fi
    done
    sleep 2
done
