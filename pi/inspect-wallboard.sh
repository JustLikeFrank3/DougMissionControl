#!/usr/bin/env bash
# inspect-wallboard.sh — capture the Pi's LIVE wallboard configuration before
# Flight Deck touches anything.
#
#   ./pi/inspect-wallboard.sh                 # write docs/pi-wallboard-survey.md
#   ./pi/inspect-wallboard.sh -o /tmp/s.md    # somewhere else
#
# READ-ONLY. It starts nothing, stops nothing, installs nothing and changes
# no configuration. Run it on the Pi, commit what it produces, and only then
# decide how Flight Deck should sit alongside the existing kiosk.
#
# The existing k3s + Prometheus + Grafana stack and its kiosk playlist are
# PRE-EXISTING INFRASTRUCTURE owned by the jobContext project. They serve the
# original wallboard use case and are not Flight Deck's to remove. This script
# exists so that judgement is made from the live configuration rather than
# from assumptions.
#
# It also copies any wallboard-kiosk.sh it finds into pi/wallboard/ so that
# script — currently unversioned and living only on this Pi — gets into git.
set -u

OUT=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o|--out) OUT="${2:-}"; shift ;;
        -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
    shift
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Works two ways: from inside a checkout, or scp'd on its own to the Pi.
# Standalone it writes beside itself rather than guessing at a repo layout
# and failing on a mkdir in someone's /home.
if [ -d "$HERE/../.git" ]; then
    REPO="$(cd "$HERE/.." && pwd)"
    [ -n "$OUT" ] || OUT="$REPO/docs/pi-wallboard-survey.md"
    KEEP="$REPO/pi/wallboard"
else
    [ -n "$OUT" ] || OUT="$HERE/pi-wallboard-survey.md"
    KEEP="$HERE/wallboard"
fi

mkdir -p "$(dirname "$OUT")" "$KEEP"
: > "$OUT"
exec 3>&1
exec 1>>"$OUT"

note() { echo "$*" >&3; }
h()    { printf '\n## %s\n\n' "$*"; }
fence(){ printf '```\n'; }
run()  { printf '$ %s\n' "$*"; fence; eval "$@" 2>&1 | sed 's/[[:space:]]*$//' | head -80; fence; }
none() { printf '_none found_\n'; }

note "Surveying the Pi's live wallboard configuration (read-only)…"

cat <<HEAD
# Pi wallboard survey

Captured by \`pi/inspect-wallboard.sh\` on $(date -Is) from \`$(hostname)\`.
Read-only snapshot of the **pre-existing** k3s / Prometheus / Grafana kiosk
that serves the jobContext wallboard. Flight Deck must coexist with this,
not replace it.
HEAD

# ── 1. what owns the display right now ───────────────────────────────────
h "Who owns the display"

printf '### systemd units that look like a kiosk\n\n'
UNITS="$(systemctl list-unit-files --no-legend --no-pager 2>/dev/null \
        | awk '{print $1}' \
        | grep -iE 'kiosk|wallboard|grafana|chromium|display|x11|lightdm|cage|weston|wayfire' || true)"
if [ -n "$UNITS" ]; then
    fence; printf '%s\n' "$UNITS"; fence
    for u in $UNITS; do
        printf '\n**%s**\n\n' "$u"
        fence
        systemctl show "$u" -p UnitFileState -p ActiveState -p ExecStart -p User -p TTYPath \
            --no-pager 2>/dev/null | sed 's/[[:space:]]*$//'
        fence
    done
else
    none
fi

printf '\n### user units\n\n'
if command -v loginctl >/dev/null 2>&1; then
    run "systemctl --user list-units --no-legend --no-pager 2>/dev/null | head -30"
else
    none
fi

printf '\n### autostart and session entry points\n\n'
for p in "$HOME/.config/autostart" /etc/xdg/autostart "$HOME/.xinitrc" \
         "$HOME/.bash_profile" /etc/lightdm/lightdm.conf; do
    if [ -e "$p" ]; then
        printf '\n**%s**\n\n' "$p"
        fence
        if [ -d "$p" ]; then ls -1 "$p"; else grep -vE '^\s*(#|$)' "$p" 2>/dev/null | head -25; fi
        fence
    fi
done

printf '\n### default target and tty1\n\n'
run "systemctl get-default"
run "who -u 2>/dev/null | head; loginctl list-sessions --no-legend 2>/dev/null | head"
run "ps -eo pid,tty,comm,args --sort=-pcpu 2>/dev/null | grep -iE 'chromium|cage|Xorg|weston|wayfire|grafana' | grep -v grep | head -12"

# ── 2. the wallboard script itself ───────────────────────────────────────
h "wallboard-kiosk.sh"

FOUND=""
for cand in /usr/local/bin/wallboard-kiosk.sh /usr/bin/wallboard-kiosk.sh \
            "$HOME/wallboard-kiosk.sh" "$HOME/bin/wallboard-kiosk.sh" \
            /opt/wallboard/wallboard-kiosk.sh; do
    [ -f "$cand" ] && FOUND="$cand" && break
done
if [ -z "$FOUND" ]; then
    FOUND="$(find / -maxdepth 5 -name 'wallboard-kiosk*' -type f 2>/dev/null | head -1)"
fi

if [ -n "$FOUND" ]; then
    printf 'Found at `%s` — copied into `pi/wallboard/` so it lands in git.\n\n' "$FOUND"
    cp -f "$FOUND" "$KEEP/$(basename "$FOUND")" 2>/dev/null \
        && note "  copied $FOUND -> pi/wallboard/"
    fence; sed 's/[[:space:]]*$//' "$FOUND" | head -120; fence
else
    printf 'Not found on this host. If the kiosk is driven some other way, the\n'
    printf 'units above are the place to look.\n'
    note "  wallboard-kiosk.sh NOT found — check the unit list in the survey"
fi

# ── 3. grafana ───────────────────────────────────────────────────────────
h "Grafana"

GRAF="${GRAFANA_URL:-http://127.0.0.1:3000}"
AUTH=""
[ -n "${GRAFANA_TOKEN:-}" ] && AUTH="-H \"Authorization: Bearer ${GRAFANA_TOKEN}\""

printf 'Probing `%s` (set `GRAFANA_URL` / `GRAFANA_TOKEN` if different).\n\n' "$GRAF"
run "curl -sf --max-time 4 $AUTH '$GRAF/api/health'"

printf '\n### dashboards\n\n'
run "curl -sf --max-time 6 $AUTH '$GRAF/api/search?type=dash-db&limit=50' \
     | tr ',' '\n' | grep -E '\"(uid|title)\"' | head -60"

printf '\n### playlists — what the kiosk actually rotates\n\n'
run "curl -sf --max-time 6 $AUTH '$GRAF/api/playlists'"

printf '\n### anonymous access (does the kiosk need a login?)\n\n'
run "curl -so /dev/null -w 'GET / -> %{http_code}\n' --max-time 4 '$GRAF/'"

# ── 4. prometheus / k3s ──────────────────────────────────────────────────
h "Prometheus and k3s"

if command -v kubectl >/dev/null 2>&1; then
    run "kubectl get nodes -o wide 2>&1 | head -6"
    run "kubectl get pods -A 2>&1 | grep -iE 'graf|prom|loki|promtail|state-metrics|node-exp' | head -20"
    run "kubectl get svc -A 2>&1 | grep -iE 'graf|prom|loki' | head -12"
else
    printf '`kubectl` not on PATH — k3s may still be running; check the unit list.\n'
fi

printf '\n### prometheus retention and storage\n\n'
run "ps -eo args 2>/dev/null | grep '[p]rometheus' | tr ' ' '\n' | grep -E '^--' | head -20"

# ── 5. headroom ──────────────────────────────────────────────────────────
h "Headroom"

run "free -m"
run "df -h / /tmp 2>/dev/null"
run "uptime"
printf '\n### the ten largest resident processes\n\n'
run "ps -eo rss,comm --sort=-rss 2>/dev/null | head -11"

if [ -r /sys/class/thermal/thermal_zone0/temp ]; then
    printf '\nSoC temperature: %s °C\n' "$(( $(cat /sys/class/thermal/thermal_zone0/temp) / 1000 ))"
fi
if command -v vcgencmd >/dev/null 2>&1; then
    printf '\nThrottling: `%s`\n' "$(vcgencmd get_throttled 2>/dev/null)"
fi

# ── 6. what Flight Deck must decide ──────────────────────────────────────
h "Questions this survey should answer"

cat <<'ASK'
Answer these from the sections above before changing any kiosk behaviour:

1. **What launches the browser today** — a systemd unit, an autostart entry,
   or `wallboard-kiosk.sh` from a session script? That is the seam Flight Deck
   has to slot into.
2. **Does the kiosk own tty1 / the DRM device?** If so, Flight Deck's own
   compositor cannot start alongside it; one of them has to become the host.
3. **Does Grafana allow anonymous viewing?** If not, the EVALS surface needs a
   token or a service account, and that decision belongs here rather than in
   the UI.
4. **Which playlist UIDs exist**, and which one does `wallboard-kiosk.sh`
   choose for each OS? Flight Deck must preserve that mapping exactly.
5. **How much headroom is actually left** with the stack already running?
   That decides whether Flight Deck's browser can be an extra process or has
   to be the same one.
ASK

# Restore stdout, but keep fd 3 open — note() still writes to it below.
exec 1>&3
note ""
note "Wrote  $OUT"
[ -n "$(ls -A "$KEEP" 2>/dev/null)" ] && note "Copied $KEEP/"
note ""
note "Nothing was changed. Commit the survey (and any recovered wallboard-kiosk.sh)"
note "before running setup-display.sh on this host."
exec 3>&-
