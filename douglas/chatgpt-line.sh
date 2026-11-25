#!/usr/bin/env bash
# douglas/chatgpt-line.sh — ChatGPT talks to Douglas himself.
#
# Run ON DOUGLAS, from a checkout:   sudo ./douglas/chatgpt-line.sh
#                                    sudo ./douglas/chatgpt-line.sh off
#
# ChatGPT's connector (douglas-chatgpt, the one thing of his that is
# published to the internet) used to hand what ChatGPT said to the bare
# model with a persona. After this it hands it to the voice pipeline, on its
# line for other programs, as ChatGPT's: answered by Douglas himself, with
# his own instructions and what he remembers.
#
# What it does: makes a token for that line if there is none
# (DOUGLAS_CHATGPT_TOKEN in /etc/douglas/voice.env, and the same token with
# the line's address in /etc/douglas/chatgpt.env; both files are root's
# alone, and the token is shown nowhere), installs the connector from this
# checkout and restarts it. The pipeline is not restarted: it learns of the
# token when it next is (sudo systemctl restart douglas-voice, when nobody
# is talking to him), and until then ChatGPT is told he is not answering.
#
# What the pipeline holds that line to, whatever the connector sends: no
# tool is run but a look in his memory; nothing is noted down or forgotten;
# no yes is taken; the conversation is ChatGPT's own, apart from the
# house's. The address ChatGPT uses, and its secret, are as they were.
#
# `off` takes the token out of both files: ChatGPT talks to the bare model
# with the persona again, as before. Idempotent.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
voice=/etc/douglas/voice.env
conn=/etc/douglas/chatgpt.env
mode="${1:-on}"
case "$mode" in on|off) ;; *) echo "usage: $0 [off]"; exit 1 ;; esac
[ -f "$conn" ] || { echo "ChatGPT's connector is not set up here: there is no $conn (see douglas-chatgpt.py)"; exit 1; }
[ -f "$voice" ] || { echo "there is no voice pipeline here ($voice): run provision.sh first"; exit 1; }

# The token: made by one process and written to the two files. Shown nowhere.
python3 -I - "$voice" "$conn" "$mode" <<'PY'
import secrets, sys
voice, conn, mode = sys.argv[1:4]


def read(path):
    return open(path).read().splitlines()


def write(path, lines):
    while lines and not lines[-1].strip():
        lines.pop()
    open(path, "w").write("\n".join(lines) + "\n")


was = read(voice)
v = [l for l in was if not l.startswith(("DOUGLAS_CHATGPT_TOKEN=", "# ChatGPT's line (douglas-chatgpt)"))]
c = [l for l in read(conn) if not l.startswith(("DOUGLAS_LINE_URL=", "DOUGLAS_LINE_TOKEN=", "# Douglas himself:"))]
if mode == "on":
    old = next((l.split("=", 1)[1].strip() for l in was if l.startswith("DOUGLAS_CHATGPT_TOKEN=")), "")
    token = old if len(old) >= 20 else secrets.token_urlsafe(32)
    port = next((l.split("=", 1)[1].strip() for l in was if l.startswith("VOICE_PTT_PORT=")), "") or "10800"
    v += ["", "# ChatGPT's line (douglas-chatgpt): what comes in with this token is ChatGPT's, not the owner's.",
          f"DOUGLAS_CHATGPT_TOKEN={token}"]
    c += ["", "# Douglas himself: the pipeline's line for other programs, and this connector's token for it.",
          f"DOUGLAS_LINE_URL=http://127.0.0.1:{port}/v1", f"DOUGLAS_LINE_TOKEN={token}"]
    print("the token for ChatGPT's line is " + ("as it was" if old == token else "made (not shown)"))
else:
    print("the token for ChatGPT's line is out of both files")
write(voice, v)
write(conn, c)
PY
chown root:root "$voice" "$conn" && chmod 600 "$voice" "$conn"
install -d -m 755 /opt/douglas
install -m 755 "$here/douglas-chatgpt.py" /opt/douglas/douglas-chatgpt.py
install -m 644 "$here/douglas-chatgpt.service" /etc/systemd/system/douglas-chatgpt.service
systemctl daemon-reload
systemctl restart douglas-chatgpt
sleep 1
systemctl is-active --quiet douglas-chatgpt || { echo "douglas-chatgpt did not come back - journalctl -u douglas-chatgpt"; exit 1; }
journalctl -u douglas-chatgpt -n 1 --no-pager -o cat

if [ "$mode" = on ]; then
    # Does the pipeline know the token yet? It does once it has been restarted.
    python3 -I - "$voice" <<'PY' || true
import sys, urllib.error, urllib.request
kept = dict(l.split("=", 1) for l in open(sys.argv[1]).read().splitlines() if "=" in l and not l.startswith("#"))
url = f"http://127.0.0.1:{kept.get('VOICE_PTT_PORT') or '10800'}/v1/models"
try:
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + kept.get("DOUGLAS_CHATGPT_TOKEN", "")})
    with urllib.request.urlopen(req, timeout=5):
        print("The pipeline knows ChatGPT's token: what ChatGPT says now reaches Douglas himself.")
except urllib.error.HTTPError as e:
    print(f"The pipeline does not know ChatGPT's token yet (it answered {e.code}): "
          "sudo systemctl restart douglas-voice, when nobody is talking to him.")
except OSError:
    print("The pipeline did not answer on its port: is douglas-voice running?")
PY
else
    echo "ChatGPT talks to the bare model again. The line's token is gone from the pipeline when douglas-voice is next restarted."
fi
