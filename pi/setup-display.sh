#!/usr/bin/env bash
# setup-display.sh — put the XENEON Edge into kiosk service on the Pi.
#
#   sudo ./pi/setup-display.sh                    # install, open the self-test
#   sudo ./pi/setup-display.sh --force-mode       # also pin 2560x720@60 in cmdline.txt
#   sudo ./pi/setup-display.sh --url http://...   # point the kiosk somewhere else
#
# Installs cage (a Wayland kiosk compositor — one fullscreen surface, no
# desktop, no panels, nothing to blank the screen) plus chromium, a launcher
# that finds whichever chromium the distro shipped, and a systemd unit that
# brings it up on tty1 at boot.
#
# Run pi/display-check.sh first. Every failure it reports is easier to fix
# from a shell than from inside a fullscreen kiosk with no window manager.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

KIOSK_USER="${SUDO_USER:-${KIOSK_USER:-pi}}"
id "$KIOSK_USER" >/dev/null 2>&1 || { echo "no such user: $KIOSK_USER" >&2; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UI_SRC="${HERE}/deck-ui"
UI_DST=/opt/flightdeck/deck-ui
CONF=/etc/flightsim/deck.env

FORCE_MODE=0
ADOPT=0
URL=""
while [ $# -gt 0 ]; do
    case "$1" in
        --force-mode) FORCE_MODE=1 ;;
        --adopt-kiosk) ADOPT=1 ;;
        --url) URL="${2:-}"; shift ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
    shift
done

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# ── refuse to stomp an existing wallboard ────────────────────────────────
# This Pi may already run the jobContext wallboard: a Grafana kiosk playlist
# driven by wallboard-kiosk.sh, owning tty1 and the DRM device. That is
# pre-existing infrastructure serving a real use case — starting a second
# compositor would fight it for the display and take the wallboard down.
# Two kiosks cannot share one framebuffer; the choice of which one hosts the
# other has to be made deliberately, from the live configuration.
say "Preflight — is something already driving this display?"
EXISTING=""
for u in $(systemctl list-unit-files --no-legend --no-pager 2>/dev/null | awk '{print $1}' \
           | grep -iE 'kiosk|wallboard' | grep -v '^flightdeck-kiosk'); do
    state="$(systemctl is-enabled "$u" 2>/dev/null || true)"
    [ "$state" = enabled ] || [ "$state" = static ] && EXISTING="${EXISTING}${u} (${state})\n"
done
# pgrep -f matches against whole command lines, including this script's own
# shell and anything that merely mentions the pattern — filter our own process
# tree out or the check reports a kiosk that is really just us looking for one.
running() {
    pgrep -af "$1" 2>/dev/null \
        | awk -v me="$$" -v pp="$PPID" '$1 != me && $1 != pp' \
        | grep -q .
}
if running 'wallboard-kiosk'; then
    EXISTING="${EXISTING}wallboard-kiosk.sh is running right now\n"
fi
if running 'chromium.*(grafana|:3000|playlist)'; then
    EXISTING="${EXISTING}a chromium instance is showing Grafana right now\n"
fi

if [ -n "$EXISTING" ] && [ "$ADOPT" -eq 0 ]; then
    printf '\n\033[31mRefusing to continue.\033[0m An existing kiosk owns this display:\n\n'
    printf "  $EXISTING"
    cat <<'STOP'

That is the jobContext wallboard, and this script would start a second
compositor on tty1 and change the default systemd target underneath it.

Do this first:

  ./pi/inspect-wallboard.sh      # read-only; writes docs/pi-wallboard-survey.md

Commit the survey, decide from it how Flight Deck should host the existing
playlist, and only then re-run with:

  sudo ./pi/setup-display.sh --adopt-kiosk

STOP
    exit 2
fi
if [ -n "$EXISTING" ]; then
    printf '  proceeding with --adopt-kiosk over:\n'
    printf "    $EXISTING"
    printf '  the existing kiosk is NOT disabled by this script — do that yourself\n'
    printf '  once Flight Deck is confirmed to be hosting the playlist.\n'
else
    echo "  nothing else appears to own the display"
fi

# ── packages ─────────────────────────────────────────────────────────────
say "Packages"
need=()
command -v cage >/dev/null 2>&1 || need+=(cage)
if ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1; then
    # Pi OS calls it chromium-browser; plain Debian calls it chromium.
    if apt-cache show chromium-browser >/dev/null 2>&1; then need+=(chromium-browser)
    else need+=(chromium); fi
fi
# cage needs a seat manager when it is not started from a logind session.
command -v seatd >/dev/null 2>&1 || need+=(seatd)

if [ "${#need[@]}" -gt 0 ]; then
    echo "  installing: ${need[*]}"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}"
else
    echo "  already present"
fi

systemctl enable --now seatd.service >/dev/null 2>&1 || true
usermod -aG video,input,render,seat "$KIOSK_USER" 2>/dev/null || \
    usermod -aG video,input "$KIOSK_USER"

# ── /tmp on tmpfs ────────────────────────────────────────────────────────
# The orchestrator writes /tmp/flightsim-intent and takes /tmp/flightsim-boot.lock
# on every trigger. On a thumb drive those are small, frequent writes to flash
# with no wear levelling worth the name. tmpfs removes them from the device
# entirely, and nothing in this project needs them to survive a reboot.
say "/tmp on tmpfs"
if findmnt -n -t tmpfs /tmp >/dev/null 2>&1; then
    echo "  already tmpfs"
else
    systemctl enable /usr/share/systemd/tmp.mount >/dev/null 2>&1 || true
    echo "  enabled tmp.mount — takes effect at the next reboot"
fi

# ── UI files ─────────────────────────────────────────────────────────────
say "UI"
install -d -o "$KIOSK_USER" -g "$KIOSK_USER" /opt/flightdeck "$UI_DST"
install -m 644 -o "$KIOSK_USER" -g "$KIOSK_USER" "$UI_SRC"/* "$UI_DST"/
echo "  installed $(ls -1 "$UI_SRC" | wc -l) file(s) to $UI_DST"

# ── config ───────────────────────────────────────────────────────────────
install -d /etc/flightsim
if [ ! -f "$CONF" ]; then
    cat > "$CONF" <<ENV
# Flight Deck kiosk settings.
# Until deck-api is installed, point at the self-test page.
KIOSK_URL=file://${UI_DST}/selftest.html
ENV
    echo "  wrote $CONF"
fi
if [ -n "$URL" ]; then
    sed -i "s|^KIOSK_URL=.*|KIOSK_URL=${URL}|" "$CONF"
    echo "  KIOSK_URL=${URL}"
fi

# ── launcher ─────────────────────────────────────────────────────────────
# A script rather than a baked ExecStart: the chromium binary name differs
# between Pi OS and Debian, and the flag list is long enough to want comments.
cat > /usr/local/bin/flightdeck-kiosk <<'LAUNCH'
#!/usr/bin/env bash
# Launched by cage. Finds chromium and opens the deck full-screen.
set -eu
# shellcheck disable=SC1091
[ -f /etc/flightsim/deck.env ] && . /etc/flightsim/deck.env
URL="${KIOSK_URL:-about:blank}"

BROWSER=""
for c in chromium-browser chromium; do
    command -v "$c" >/dev/null 2>&1 && { BROWSER="$c"; break; }
done
[ -n "$BROWSER" ] || { echo "no chromium found" >&2; exit 1; }

exec "$BROWSER" \
    --kiosk "$URL" \
    --ozone-platform=wayland \
    --window-size=2560,720 \
    --start-fullscreen \
    --noerrdialogs \
    --disable-infobars \
    --no-first-run \
    --no-default-browser-check \
    --disable-session-crashed-bubble \
    --disable-features=Translate,TranslateUI,MediaRouter \
    --disable-pinch \
    --overscroll-history-navigation=0 \
    --check-for-update-interval=31536000 \
    --password-store=basic \
    --autoplay-policy=no-user-gesture-required \
    --process-per-site
LAUNCH
chmod 755 /usr/local/bin/flightdeck-kiosk

# ── unit ─────────────────────────────────────────────────────────────────
say "Kiosk service"
cat > /etc/systemd/system/flightdeck-kiosk.service <<UNIT
[Unit]
Description=Flight Deck kiosk (cage + chromium on the XENEON Edge)
After=systemd-user-sessions.service seatd.service network.target
Wants=seatd.service

[Service]
User=${KIOSK_USER}
# A real login session, so cage gets a seat and can open the DRM device.
PAMName=login
TTYPath=/dev/tty1
StandardInput=tty-force
StandardOutput=journal
StandardError=journal
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes
Environment=XDG_RUNTIME_DIR=/run/user/%U
Environment=XDG_SESSION_TYPE=wayland
# -d: no client-side decorations. cage exits when the browser exits, and
# Restart brings the pair back together.
ExecStart=/usr/bin/cage -d -- /usr/local/bin/flightdeck-kiosk
Restart=always
RestartSec=5

[Install]
WantedBy=graphical.target
UNIT

systemctl daemon-reload

# Changing the default target reshapes what comes up at boot for everything
# else on this host, so record the old value and say what happened.
PREV_TARGET="$(systemctl get-default 2>/dev/null || echo unknown)"
if [ "$PREV_TARGET" != graphical.target ]; then
    echo "  default target was ${PREV_TARGET}; leaving it alone"
    echo "  (the unit is WantedBy=graphical.target — set it yourself if you want"
    echo "   the kiosk at boot: systemctl set-default graphical.target)"
fi

systemctl enable flightdeck-kiosk.service >/dev/null
systemctl restart flightdeck-kiosk.service
sleep 3
if systemctl is-active --quiet flightdeck-kiosk.service; then
    echo "  running"
else
    echo "  NOT running — journalctl -u flightdeck-kiosk -n 40"
fi

# ── optional: pin the mode ───────────────────────────────────────────────
if [ "$FORCE_MODE" -eq 1 ]; then
    say "Pinning 2560x720@60"
    CMDLINE=/boot/firmware/cmdline.txt
    [ -f "$CMDLINE" ] || CMDLINE=/boot/cmdline.txt
    if [ ! -f "$CMDLINE" ]; then
        echo "  cannot find cmdline.txt — skipping"
    elif grep -q 'video=HDMI-A-1:2560x720' "$CMDLINE"; then
        echo "  already pinned"
    else
        # cmdline.txt must stay exactly one line — a stray newline is an
        # unbootable Pi, so append in place rather than echoing a new line.
        cp "$CMDLINE" "${CMDLINE}.flightdeck.bak"
        sed -i '1s|$| video=HDMI-A-1:2560x720@60|' "$CMDLINE"
        if [ "$(wc -l < "$CMDLINE")" -gt 1 ]; then
            mv "${CMDLINE}.flightdeck.bak" "$CMDLINE"
            echo "  edit would have added a newline — reverted, do it by hand"
        else
            echo "  pinned (backup at ${CMDLINE}.flightdeck.bak) — reboot to apply"
        fi
    fi
fi

say "Next"
cat <<'NEXT'
  The Edge should now show the panel self-test. Touch each of the four
  corner marks and read the verdict:

    viewport            must be 2560 x 720
    input type          must say "touch", not "mouse / pen"
    corners registered  4 / 4
    worst corner offset under ~40 px

  If it says PASS, video and touch are proven. Then:

    sudo ./pi/setup-deck.sh          # deck-api + repoint fauxmo + real UI

  Logs:  journalctl -u flightdeck-kiosk -f
NEXT
