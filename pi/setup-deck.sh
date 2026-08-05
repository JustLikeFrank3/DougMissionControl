#!/usr/bin/env bash
# setup-deck.sh — install deck-api and point everything at it.
#
#   sudo ./pi/setup-deck.sh                # install, repoint fauxmo, use it
#   sudo ./pi/setup-deck.sh --no-fauxmo    # leave fauxmo calling the script
#
# Run this ON THE PI, after pi/setup-display.sh has proved video and touch.
#
# What it does:
#   * installs deck_api.py + the deck UI to /opt/flightdeck
#   * mints DECK_TOKEN into /etc/flightsim/boot.env (only if absent)
#   * installs and starts the deck-api systemd unit on :8088
#   * repoints fauxmo's on_cmd at deck-api, so a voice-triggered boot shows
#     up on the panel instead of being invisible to it
#   * repoints the kiosk from the self-test page at the real UI
#
# flightsim-boot.sh is NOT modified. deck-api shells out to it exactly as
# fauxmo always has, and follows its journald output to learn the phases.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

DECK_USER="${SUDO_USER:-${DECK_USER:-pi}}"
id "$DECK_USER" >/dev/null 2>&1 || { echo "no such user: $DECK_USER" >&2; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/opt/flightdeck
CONF=/etc/flightsim/boot.env
DECK_CONF=/etc/flightsim/deck.env
FAUXMO_CONF=/opt/fauxmo/config.json
PORT=8088

REPOINT=1
SWAP_AUTOSTART=0
while [ $# -gt 0 ]; do
    case "$1" in
        --no-fauxmo)      REPOINT=0 ;;
        --swap-autostart) SWAP_AUTOSTART=1 ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
    shift
done

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# ── files ────────────────────────────────────────────────────────────────
say "Installing"
install -d "$DEST" "$DEST/deck-ui"
install -m 755 "${HERE}/deck-api/deck_api.py" "$DEST/deck_api.py"
install -m 644 "${HERE}/deck-ui"/* "$DEST/deck-ui/"
sed -i 's/\r$//' "$DEST/deck_api.py"   # a CRLF checkout breaks the shebang
echo "  $DEST"

# ── config ───────────────────────────────────────────────────────────────
install -d /etc/flightsim
if [ ! -f "$CONF" ]; then
    echo "  WARNING: $CONF missing — run pi/setup.sh first, or deck-api will"
    echo "  fall back to the example addresses and probe the wrong host."
    touch "$CONF"
fi

# Loopback needs no token; anything else does. Same posture as boot-agent.ps1.
if ! grep -q '^DECK_TOKEN=' "$CONF"; then
    printf '\n# deck-api: required for non-loopback callers (the kiosk is loopback)\nDECK_TOKEN=%s\nDECK_PORT=%s\n' \
        "$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)" "$PORT" >> "$CONF"
    echo "  minted DECK_TOKEN in $CONF"
else
    echo "  DECK_TOKEN already set — kept"
fi

# journalctl -t flightsim-boot is how phases 2-4 are observed.
if ! id -nG "$DECK_USER" | grep -qw systemd-journal; then
    usermod -aG systemd-journal "$DECK_USER"
    echo "  added $DECK_USER to systemd-journal"
fi

# ── service ──────────────────────────────────────────────────────────────
say "deck-api service"
cat > /etc/systemd/system/deck-api.service <<UNIT
[Unit]
Description=Flight Deck API (boot triggers, state, SSE)
After=network-online.target
Wants=network-online.target

[Service]
User=${DECK_USER}
# The orchestrator needs \$HOME/.ssh/flightsim_ed25519 for the Windows leg.
Environment=DECK_UI_DIR=${DEST}/deck-ui
ExecStart=/usr/bin/python3 ${DEST}/deck_api.py
Restart=always
RestartSec=5
# State lives in tmpfs — this Pi boots off a thumb drive and nothing here
# needs to survive a reboot.
RuntimeDirectory=flightdeck
RuntimeDirectoryMode=0755
NoNewPrivileges=yes
ProtectSystem=full
PrivateTmp=no

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable deck-api.service >/dev/null
systemctl restart deck-api.service
sleep 2
if systemctl is-active --quiet deck-api.service; then
    echo "  running on :${PORT}"
else
    echo "  NOT running — journalctl -u deck-api -n 40" >&2
    exit 1
fi

if curl -sf -m 5 "http://127.0.0.1:${PORT}/api/state" >/dev/null; then
    echo "  /api/state answers"
else
    echo "  WARNING: /api/state did not answer"
fi

# ── fauxmo ───────────────────────────────────────────────────────────────
if [ "$REPOINT" -eq 1 ] && [ -f "$FAUXMO_CONF" ]; then
    say "Repointing fauxmo at deck-api"
    cp -n "$FAUXMO_CONF" "${FAUXMO_CONF}.pre-deck.bak" || true

    PORT="$PORT" python3 - "$FAUXMO_CONF" <<'PY'
import json, os, re, sys

path = sys.argv[1]
port = os.environ["PORT"]
with open(path) as fh:
    cfg = json.load(fh)

def intent_of(dev):
    """Recover the intent from whatever the on_cmd currently says."""
    cmd = dev.get("on_cmd", "")
    m = re.search(r'FLIGHT_INTENT=(\w+)', cmd)
    if m:
        return m.group(1)
    if re.search(r'\blinux\b', cmd):
        return "code"
    return "plain"

changed = 0
for plugin in cfg.get("PLUGINS", {}).values():
    for dev in plugin.get("DEVICES", []):
        cmd = dev.get("on_cmd", "")
        if "/api/boot" in cmd:
            continue                      # already repointed — idempotent
        intent = intent_of(dev)
        dev["on_cmd"] = (
            "curl -sf -m 5 -X POST -H 'Content-Type: application/json' "
            f"""-d '{{"intent":"{intent}"}}' http://127.0.0.1:{port}/api/boot"""
        )
        changed += 1

with open(path, "w") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
print(f"  rewrote {changed} device(s); backup at {path}.pre-deck.bak")
PY

    systemctl restart fauxmo.service || true
    echo "  fauxmo restarted"
elif [ "$REPOINT" -eq 1 ]; then
    echo "  (no $FAUXMO_CONF — skipping fauxmo repoint)"
fi

# ── kiosk ────────────────────────────────────────────────────────────────
say "Kiosk"
if [ -f "$DECK_CONF" ]; then
    sed -i "s|^KIOSK_URL=.*|KIOSK_URL=http://127.0.0.1:${PORT}/|" "$DECK_CONF"
else
    echo "KIOSK_URL=http://127.0.0.1:${PORT}/" > "$DECK_CONF"
fi
echo "  KIOSK_URL=http://127.0.0.1:${PORT}/"

# On a host that already runs a compositor (Raspberry Pi OS: lightdm ->
# labwc), Flight Deck belongs INSIDE that session as another Wayland client,
# launched from ~/.config/autostart — not as a second compositor. Swap the
# autostart entry rather than installing a systemd kiosk unit.
AUTOSTART="/home/${DECK_USER}/.config/autostart"
if [ -d "$AUTOSTART" ] && [ "$SWAP_AUTOSTART" -eq 1 ]; then
    install -m 755 "${HERE}/wallboard/flightdeck-kiosk.sh" /usr/local/bin/flightdeck-kiosk.sh
    sed -i 's/\r$//' /usr/local/bin/flightdeck-kiosk.sh
    install -m 644 -o "$DECK_USER" -g "$DECK_USER" \
        "${HERE}/wallboard/flightdeck.desktop" "${AUTOSTART}/flightdeck.desktop"
    echo "  installed /usr/local/bin/flightdeck-kiosk.sh + flightdeck.desktop"

    # Disable rather than delete: the original stays one mv away.
    for old in "${AUTOSTART}"/*.desktop; do
        [ -e "$old" ] || continue
        case "$(basename "$old")" in flightdeck.desktop) continue ;; esac
        if grep -qiE 'wallboard-kiosk|playlists/play' "$old" 2>/dev/null; then
            mv "$old" "${old}.flightdeck-disabled"
            echo "  disabled $(basename "$old") -> $(basename "$old").flightdeck-disabled"
        fi
    done
    echo "  log out and back in (or reboot) to pick up the new session entry"
elif [ -d "$AUTOSTART" ]; then
    cat <<NOTE
  autostart directory found at $AUTOSTART but left alone.
  Re-run with --swap-autostart to have Flight Deck take over the kiosk:
      sudo ./pi/setup-deck.sh --swap-autostart
  It installs flightdeck-kiosk.sh, adds flightdeck.desktop, and renames the
  existing wallboard entry to *.flightdeck-disabled (reversible with mv).
NOTE
else
    systemctl restart flightdeck-kiosk.service 2>/dev/null && echo "  kiosk restarted" \
        || echo "  (no autostart dir and no kiosk unit — see pi/setup-display.sh)"
fi

say "Done"
cat <<NEXT
  The panel should now show the deck. Try it without touching anything:

    curl -X POST -H 'Content-Type: application/json' \\
         -d '{"intent":"plain"}' http://127.0.0.1:${PORT}/api/boot

  and watch the phase track advance. Then say "Alexa, flight sim bootup"
  and confirm the same track lights up for a voice trigger.

  Logs:  journalctl -u deck-api -f
         journalctl -t flightsim-boot -f     # the orchestrator itself
  State: cat /run/flightdeck/state.json | python3 -m json.tool
NEXT
