#!/usr/bin/env bash
# douglas/update.sh — bring Douglas's own programs up to this checkout.
#
# Run ON DOUGLAS, from a checkout, after a `git pull`:
#
#     sudo ./douglas/update.sh
#     sudo FORCE=1 ./douglas/update.sh        # though a job is under way, or he is mid-turn
#     ./douglas/update.sh --settings FILE     # only bring a settings file up to date, and say what changed
#
# For a machine that provision.sh has already set up. provision.sh is the
# whole build: run again it also fetches llama.cpp's newest commit and
# rebuilds it, and upgrades Open WebUI and the speech packages. That is not
# what "I pulled a fix" asks for. This does the small part:
#
#   * the voice pipeline and his voice: douglas-voice.py and douglas-tts.py
#     into /opt/douglas, and douglas-voice's unit
#   * the workshop, the agent, his memory and the switches, each by its own
#     installer, and only those that are installed here already
#   * settings in /etc/douglas/voice.env that an older version wrote and a
#     newer one reads differently (see settings_up below): only a value that
#     is still the old default is changed, and each change is said
#   * douglas-voice restarted, once, at the end
#
# It does not touch the model server, Open WebUI, the speech services, the
# eyes or the image server, and it installs nothing that was not here. What
# answers on the network with a token of its own (douglas-mcp, ChatGPT's
# line) is left as it is: their own scripts are the way to change those.
#
# The Pi is a machine of its own: there, `sudo ./pi/setup-deck.sh`.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
step() { printf '\n== %s\n' "$*"; }
warn() { printf '!! %s\n' "$*" >&2; }
# A value from a systemd EnvironmentFile, without sourcing it as shell.
envval() { sed -n "s/^$2=//p" "$1" 2>/dev/null | tail -n1 | sed 's/^"\(.*\)"$/\1/'; }

# Settings an older version wrote that a newer one reads differently. Only a
# value that is still what the older version wrote is changed: one the owner
# set is the owner's. Says each change; says nothing where there is none.
settings_up() {
    local f="$1" v
    [ -f "$f" ] || { echo "no settings file at $f"; return 1; }
    # He kept six exchanges. He keeps what fits the model's window now, and
    # the count is not read any more.
    if grep -q '^VOICE_HISTORY_TURNS=' "$f"; then
        if grep -q '^VOICE_HISTORY_CHARS=' "$f"; then
            sed -i '/^VOICE_HISTORY_TURNS=/d' "$f"
        else
            sed -i 's/^VOICE_HISTORY_TURNS=.*/VOICE_HISTORY_CHARS=24000/' "$f"
        fi
        echo "  VOICE_HISTORY_TURNS is no longer read: VOICE_HISTORY_CHARS=24000 is in its place"
    fi
    # A conversation ended after five minutes of quiet, then an hour. It goes
    # on until a new one is begun now, and is kept.
    v="$(envval "$f" VOICE_HISTORY_IDLE_S)"
    if [ "$v" = 300 ] || [ "$v" = 3600 ]; then
        sed -i 's/^VOICE_HISTORY_IDLE_S=.*/VOICE_HISTORY_IDLE_S=0/' "$f"
        echo "  VOICE_HISTORY_IDLE_S was $v: it is 0 now (a conversation does not end for having gone quiet)"
    fi
    # A reply stopped at 400 tokens, in the middle of a sentence.
    v="$(envval "$f" LLM_MAX_TOKENS)"
    if [ "$v" = 400 ]; then
        sed -i 's/^LLM_MAX_TOKENS=.*/LLM_MAX_TOKENS=1500/' "$f"
        echo "  LLM_MAX_TOKENS was 400: it is 1500 now (room for a whole answer)"
    fi
}

if [ "${1:-}" = "--settings" ]; then
    [ -n "${2:-}" ] || { echo "usage: $0 --settings FILE"; exit 2; }
    settings_up "$2"
    exit
fi
[ $# -eq 0 ] || { echo "usage: sudo $0   (or: $0 --settings FILE)"; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }

voice=/etc/douglas/voice.env
if [ ! -f "$voice" ] || [ ! -f /opt/douglas/douglas-voice.py ]; then
    echo "Douglas is not set up on this machine yet (no $voice, or no /opt/douglas/douglas-voice.py)."
    echo "That is ./douglas/provision.sh, once. This script only brings what is installed up to date."
    exit 1
fi

# Not in the middle of something, unless told to. A job's work so far is
# kept on its branch when the workshop is restarted, but the job is over.
jobs_dir="$(sed -n 's/.*WORK_JOBS=\([^ ]*\).*/\1/p' /etc/systemd/system/douglas-work.service 2>/dev/null | head -n1)"
jobs_dir="${jobs_dir:-/var/lib/douglas-spool}"
if [ "${FORCE:-0}" != 1 ]; then
    if compgen -G "$jobs_dir/*/job.json" >/dev/null 2>&1; then
        echo "A job is under way in the workshop. Updating restarts the workshop and ends it."
        echo "Wait for it, have him stop it, or run again with FORCE=1."
        exit 1
    fi
    state_file="$(envval "$voice" VOICE_STATE_FILE)"; state_file="${state_file:-/run/douglas/voice.json}"
    for _ in $(seq 30); do
        state="$(python3 -I -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get("state") or "")
except Exception: print("")' "$state_file")"
        case "$state" in listening|thinking|speaking) sleep 2 ;; *) state=""; break ;; esac
    done
    if [ -n "$state" ]; then
        echo "Someone is talking to him ($state), and has been for a minute. Run again when he is idle, or with FORCE=1."
        exit 1
    fi
fi

at="$(git -C "$here" log -1 --format='%h %s' 2>/dev/null || true)"
step "updating to ${at:-this checkout}"

step "douglas-voice"
install -m 755 "$here/douglas-voice.py" /opt/douglas/douglas-voice.py
install -m 755 "$here/douglas-tts.py" /opt/douglas/douglas-tts.py
install -m 644 "$here/douglas-voice.service" /etc/systemd/system/douglas-voice.service
systemctl daemon-reload
settings_up "$voice"
chown root:root "$voice" && chmod 600 "$voice"

failed=""
# Each by its own installer, as provision.sh runs them and in its order, and
# only where it is here already: an update installs nothing new.
part() {    # part NAME FILE-THAT-SAYS-IT-IS-INSTALLED SCRIPT
    if [ -f "$2" ]; then
        step "$1"
        "$here/$3" || { warn "$1 was not updated ($3 says why)"; failed="$failed $1"; }
    else
        echo
        echo "-- $1 is not installed here: left alone (./douglas/$3 installs it)"
    fi
}
part "douglas-work" /opt/douglas/douglas-work.py work-setup.sh
part "douglas-agent" /opt/douglas/douglas-agent.py setup.sh
part "douglas-memory" /opt/douglas/douglas-memory.py memory-setup.sh
part "douglas-switch" /opt/douglas/douglas-switch.py switch-setup.sh

step "restarting douglas-voice"
systemctl restart douglas-voice || { warn "douglas-voice did not start - journalctl -u douglas-voice"; failed="$failed douglas-voice"; }
systemctl is-active --quiet douglas-tts 2>/dev/null && { systemctl restart douglas-tts || warn "douglas-tts did not restart"; }

step "status"
sleep 3
port="$(envval "$voice" VOICE_PTT_PORT)"; port="${port:-10800}"
for u in douglas-voice douglas-work douglas-job douglas-agent douglas-memory douglas-switch; do
    systemctl cat "$u" >/dev/null 2>&1 || continue
    if systemctl is-active --quiet "$u"; then
        printf '  up    %s\n' "$u"
    else
        printf '  down  %s   (journalctl -u %s)\n' "$u" "$u"
        failed="$failed $u"
    fi
done
if curl -fsS -m 5 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
    echo "  the voice pipeline answers on :$port"
else
    warn "the voice pipeline does not answer on :$port yet - journalctl -u douglas-voice -n 40"
    failed="$failed douglas-voice:$port"
fi
journalctl -u douglas-voice -n 60 --no-pager 2>/dev/null | grep -m1 'conversations kept' | sed 's/^/  /' || true

echo
if [ -n "$failed" ]; then
    echo "Not all of it went through:$failed"
    exit 1
fi
echo "Douglas is up to date. The panel and the phone page are the Pi's: there, sudo ./pi/setup-deck.sh"
