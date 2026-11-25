#!/usr/bin/env bash
# douglas/webui-setup.sh — Douglas as a model in Open WebUI, beside the bare one.
#
# Run ON DOUGLAS, from a checkout:   sudo ./douglas/webui-setup.sh
#                                    sudo ./douglas/webui-setup.sh off
#                                    sudo ./douglas/webui-setup.sh back
#
# Open WebUI used to talk to the model server and nothing else: the bare
# model, with none of Douglas's instructions, tools or memory. After this it
# lists a second model, `douglas`, which is the voice pipeline's line for
# other programs (/v1 on its own port). A message there is a turn like one
# typed on the phone: the same conversation, the same memory, the same rules.
# The bare model stays in the list, for long texts and for anything that is
# not a word to Douglas.
#
# What it does:
#   - makes a token for that line if there is none: DOUGLAS_API_TOKEN in
#     /etc/douglas/voice.env, which is root's alone. It opens the line and
#     nothing else of the pipeline's, and the line answers on this machine
#     only.
#   - gives Open WebUI a user of its own, `open-webui`. It ran as `douglas`,
#     the user of the model server and the speech and picture services, and
#     its database could be read by every user of the machine: every
#     conversation in it, and from now on a token that speaks to Douglas.
#     Its folder becomes its own (700) and its settings file root's alone.
#   - adds the connection to Open WebUI: http://127.0.0.1:<port>/v1 with the
#     token. Open WebUI keeps its connections in its database once it has
#     run and reads its settings file only before that, so the connection is
#     written to whichever of the two applies. Two rows of that database are
#     read and written, nothing else in it is looked at, and a copy of it is
#     kept first (webui.db.before-douglas, beside it).
#
# Open WebUI is stopped for this, for some seconds. The pipeline is not
# restarted: it learns of the token when it next is (sudo systemctl restart
# douglas-voice, when nobody is talking to him), and until then Open WebUI
# lists the bare model alone. Idempotent.
#
# `off` takes the connection out of Open WebUI and the token out of
# voice.env: he is no longer a model there, and the line is closed when the
# pipeline is next restarted. `back` does that and puts Open WebUI under the
# user it ran as before.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
unit=/etc/systemd/system/open-webui.service
env=/etc/douglas/open-webui.env
voice=/etc/douglas/voice.env
data=/var/lib/open-webui
kept=/etc/douglas/.before-webui-user
mode="${1:-on}"
case "$mode" in on|off|back) ;; *) echo "usage: $0 [off|back]"; exit 1 ;; esac
[ -f "$unit" ] && [ -f "$env" ] || { echo "Open WebUI is not installed here: run provision.sh first"; exit 1; }
[ -f "$voice" ] || { echo "there is no voice pipeline here ($voice): run provision.sh first"; exit 1; }
port="$(sed -n 's/^VOICE_PTT_PORT=//p' "$voice" | tail -n1)"
url="http://127.0.0.1:${port:-10800}/v1"

# The token: made and written by one process, shown nowhere.
if [ "$mode" = on ]; then
    python3 -I - "$voice" <<'PY'
import secrets, sys
path = sys.argv[1]
lines = open(path).read().splitlines()
if not any(l.startswith("DOUGLAS_API_TOKEN=") and len(l) > 40 for l in lines):
    lines = [l for l in lines if not l.startswith(("DOUGLAS_API_TOKEN=", "# A line for the owner's other programs"))]
    while lines and not lines[-1].strip():
        lines.pop()
    lines += ["", "# A line for the owner's other programs (Open WebUI): Douglas as a model, at /v1 on the pipeline's port.",
              f"DOUGLAS_API_TOKEN={secrets.token_urlsafe(32)}"]
    open(path, "w").write("\n".join(lines) + "\n")
    print("a token for the line is in voice.env now (not shown): restart douglas-voice when nobody is talking to him")
PY
    chown root:root "$voice" && chmod 600 "$voice"
fi

systemctl stop open-webui 2>/dev/null || true
# Whatever happens below, it is not left stopped.
trap 'systemctl start open-webui 2>/dev/null || true' EXIT

# A copy of the database as it was before any of this, once.
if [ -f "$data/webui.db" ] && [ ! -f "$data/webui.db.before-douglas" ]; then
    python3 -I - "$data/webui.db" "$data/webui.db.before-douglas" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY
    chmod 600 "$data/webui.db.before-douglas"
fi

# Whose it is.
if [ "$mode" = back ]; then
    [ -d "$kept" ] || { echo "nothing was kept from before ($kept is not there): nothing to put back"; exit 1; }
    install -m 644 "$kept/open-webui.service" "$unit"
    was="$(sed -n 's/^User=//p' "$unit" | tail -n1)"; was="${was:-douglas}"
    chown -hR "$was:$(id -gn "$was")" "$data"
    chmod 755 "$data"
    chown "root:$(id -gn "$was")" "$env" && chmod 640 "$env"
elif [ "$mode" = on ]; then
    getent passwd open-webui >/dev/null \
        || useradd --system --user-group --no-create-home --home-dir "$data" --shell /usr/sbin/nologin open-webui
    if [ ! -d "$kept" ]; then                    # the unit as it was, for `back`
        install -d -m 700 "$kept"
        cp -p "$unit" "$kept/open-webui.service"
    fi
    install -m 644 "$here/open-webui.service" "$unit"
    install -d -m 700 -o open-webui -g open-webui "$data"
    chown -hR open-webui:open-webui "$data"      # -h: never what a link in there points at
    chmod 700 "$data"
    find "$data" -maxdepth 1 -type f -exec chmod 600 {} +
    chown root:root "$env" && chmod 600 "$env"
fi
systemctl daemon-reload

# The connection. The token is read from one root-only file and written to
# the other place by this one process.
python3 -I "$here/webui-connect.py" "$data/webui.db" "$voice" "$env" "$url" "$mode" \
    || { echo "the connection was not changed"; exit 1; }

if [ "$mode" != on ]; then
    sed -i "/^# A line for the owner's other programs/d; /^DOUGLAS_API_TOKEN=/d" "$voice"
    echo "DOUGLAS_API_TOKEN is out of $voice: the line is closed when douglas-voice is next restarted."
fi

who="$(sed -n 's/^User=//p' "$unit" | tail -n1)"
if [ "$mode" != back ] && [ "$who" = open-webui ] && [ -e "$data" ]; then
    chown -hR open-webui:open-webui "$data"      # what the steps above wrote there as root
    find "$data" -maxdepth 1 -type f -exec chmod 600 {} +
fi
systemctl start open-webui
wport="$(sed -n 's/^OPEN_WEBUI_PORT=//p' "$env" | tail -n1)"
for _ in $(seq 1 90); do
    curl -fsS -m 2 -o /dev/null "http://127.0.0.1:${wport:-8080}/health" 2>/dev/null && break
    sleep 1
done
curl -fsS -m 2 -o /dev/null "http://127.0.0.1:${wport:-8080}/health" \
    || { echo "Open WebUI did not come back - journalctl -u open-webui (and '$0 back' puts it as it was)"; exit 1; }
pid="$(systemctl show open-webui -p MainPID --value)"
echo "Open WebUI is up again, as $(stat -c %U "/proc/${pid:-0}" 2>/dev/null || echo "a user that could not be read")."

if [ "$mode" = on ]; then
    # From the side of the user it used to share: neither its database nor
    # its settings can be read any more.
    if id douglas >/dev/null 2>&1; then
        for f in "$data/webui.db" "$env" "$voice"; do
            if runuser -u douglas -- test -r "$f" 2>/dev/null; then
                echo "the douglas user can still read $f: not right"; exit 1
            fi
        done
        echo "The model server's user can read neither Open WebUI's database nor its settings."
    fi
    # And the line itself, if the pipeline has been restarted since the token was made.
    python3 -I - "$voice" "$url" <<'PY' || true
import json, sys, urllib.error, urllib.request
token = next((l.split("=", 1)[1].strip() for l in open(sys.argv[1]).read().splitlines()
              if l.startswith("DOUGLAS_API_TOKEN=")), "")
try:
    req = urllib.request.Request(sys.argv[2] + "/models", headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=5) as r:
        names = [m.get("id") for m in json.loads(r.read()).get("data", [])]
    print("The line answers: Open WebUI lists " + " and ".join(map(str, names)) + " beside the bare model.")
except urllib.error.HTTPError as e:
    print(f"The pipeline does not know the token yet (it answered {e.code}): "
          "sudo systemctl restart douglas-voice, when nobody is talking to him.")
except OSError:
    print("The pipeline did not answer on its port: is douglas-voice running?")
PY
fi
