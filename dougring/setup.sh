#!/usr/bin/env bash
# dougring/setup.sh — install DougRing on node1 (the Pi), the house's execution
# authority. Douglas never holds the Ring app's credentials or the tokens.
#
# Run ON NODE1, from a copy of the repo:   sudo ./dougring/setup.sh
#                                         sudo ./dougring/setup.sh funnel      (public door: needs the owner's yes)
#                                         sudo ./dougring/setup.sh funnel-off
#                                         sudo ./dougring/setup.sh webhooks-on (Ring's console requires a Webhook URL)
#                                         sudo ./dougring/setup.sh webhooks-off
#                                         sudo ./dougring/setup.sh off         (service and door gone; tokens kept)
#
# What it leaves behind:
#   user     dougring (no login, no home): the only user that holds Ring tokens
#   group    dougring-api: who may open the private door (Mission Control)
#   code     /opt/dougring/dougring.py
#   unit     dougring.service: public listener 127.0.0.1:9130, private door
#            /run/dougring/api.sock (0660 dougring:dougring-api)
#   state    /var/lib/dougring/state.json (0600 dougring)
#   needs    /etc/dougring/ring-app.env (0600 root), put there by
#            dougring/install-credentials.py from the workstation
#
# The public door is Tailscale Funnel on this machine's port 8443, paths under
# /ring/ only, proxied to 127.0.0.1:9130 and nothing else. node1's :443 (the deck's phone page) stays
# tailnet-only: Funnel publishes a whole port, so the two never share one.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
FUNNEL_PORT=8443
PUBLIC=http://127.0.0.1:9130

case "${1:-install}" in
funnel)
    systemctl is-active --quiet dougring || { echo "dougring is not running: install it first"; exit 1; }
    # --set-path: tailscaled itself refuses every path outside /ring/ before
    # DougRing sees it. It strips the prefix, so the target carries it back.
    tailscale funnel --bg --https="$FUNNEL_PORT" --set-path /ring "$PUBLIC/ring"
    tailscale funnel status
    exit 0 ;;
funnel-off)
    tailscale funnel --https="$FUNNEL_PORT" off || true
    tailscale serve --https="$FUNNEL_PORT" off 2>/dev/null || true
    tailscale funnel status || true
    exit 0 ;;
off)
    tailscale funnel --https="$FUNNEL_PORT" off 2>/dev/null || true
    tailscale serve --https="$FUNNEL_PORT" off 2>/dev/null || true
    systemctl disable --now dougring 2>/dev/null || true
    rm -f /etc/systemd/system/dougring.service
    systemctl daemon-reload
    echo "DougRing is stopped and its public door is shut. Tokens remain in /var/lib/dougring until removed by hand."
    exit 0 ;;
webhooks-on|webhooks-off)
    # Signed events from Ring are checked and acknowledged; only "the app was
    # removed" is acted on (the tokens are dropped). Nothing else happens on
    # an event until events are built (docs/DOUGRING.md, phase 4).
    d=/etc/systemd/system/dougring.service.d
    if [ "$1" = webhooks-on ]; then
        install -d -m 755 "$d"
        printf '[Service]\nEnvironment=DOUGRING_WEBHOOKS=1\n' > "$d/webhooks.conf"
    else
        rm -f "$d/webhooks.conf"
    fi
    systemctl daemon-reload
    systemctl restart dougring
    sleep 2
    echo "GET /ring/webhook -> $(curl -s -o /dev/null -w '%{http_code}' "$PUBLIC/ring/webhook")"
    exit 0 ;;
install) ;;
*) echo "usage: $0 [install|funnel|funnel-off|webhooks-on|webhooks-off|off]"; exit 2 ;;
esac

getent group dougring-api >/dev/null || groupadd --system dougring-api
getent passwd dougring >/dev/null \
    || useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin dougring
# Mission Control (deck-api) is the one thing that opens the private door.
deck_user="$(systemctl show -p User --value deck-api 2>/dev/null || true)"
if [ -n "$deck_user" ] && ! id -nG "$deck_user" | grep -qw dougring-api; then
    usermod -a -G dougring-api "$deck_user"
    systemctl try-restart deck-api || true
fi
install -d -m 755 /opt/dougring
install -m 644 "$here/dougring.py" /opt/dougring/dougring.py
install -m 644 "$here/dougring.service" /etc/systemd/system/dougring.service
install -d -m 700 -o root -g root /etc/dougring
rm -f /etc/systemd/system/dougring.service.d/group.conf
systemctl daemon-reload
if [ ! -s /etc/dougring/ring-app.env ]; then
    echo "No credentials yet at /etc/dougring/ring-app.env: run dougring/install-credentials.py from the workstation, then this again."
    systemctl enable dougring >/dev/null 2>&1
    exit 0
fi
systemctl enable dougring >/dev/null 2>&1
systemctl restart dougring
sleep 2
systemctl is-active dougring
code=$(curl -s -o /dev/null -w '%{http_code}' "$PUBLIC/ring/home")
echo "public listener: /ring/home -> $code"
