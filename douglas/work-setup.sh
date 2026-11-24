#!/usr/bin/env bash
# douglas/work-setup.sh — install Douglas's workshop on Douglas: repositories
# he may clone from GitHub, read the code of, and have a job done on.
#
# Run ON DOUGLAS, from a checkout:   sudo ./douglas/work-setup.sh
#                                    sudo ./douglas/work-setup.sh off
#
# What it leaves behind:
#   users    douglas-work (the manager: it has the network and, if you give
#            it one, the GitHub token) and douglas-job (the runner, fenced
#            in), and the group douglas-spool they pass files through
#   code     /opt/douglas/douglas-work.py
#   units    douglas-work.socket, douglas-work, douglas-job-fence, douglas-job
#   config   /etc/douglas/work.env and job.env (written only if missing),
#            /etc/douglas/job-fence.nft
#   and      WORK_SOCKET in /etc/douglas/voice.env, if it has none
# The clones are in /var/lib/douglas-work, a job's work tree in
# /var/lib/douglas-job, and what the two pass each other in
# /var/lib/douglas-spool.
#
# It does not restart douglas-voice, which is how he learns of the tool: do
# that when nobody is talking to him (sudo systemctl restart douglas-voice).
# A job that is under way is ended by a re-run. Idempotent otherwise.
# `off` stops and disables it all and takes the tool away from him; the
# clones stay where they are.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
envval() { sed -n "s/^$2=//p" "$1" 2>/dev/null | tail -n1 | sed 's/^"\(.*\)"$/\1/'; }
units="douglas-work.socket douglas-work.service douglas-job-fence.service douglas-job.service"
voice=/etc/douglas/voice.env

if [ "${1:-on}" = off ]; then
    systemctl disable --now douglas-job.service douglas-work.service douglas-work.socket \
        douglas-job-fence.service 2>/dev/null || true
    if [ -f "$voice" ] && grep -q '^WORK_SOCKET=' "$voice"; then
        sed -i '/^# The workshop (douglas-work)/d; /^WORK_SOCKET=/d' "$voice"
        echo "WORK_SOCKET is out of $voice: restart douglas-voice when nobody is talking to him."
    fi
    echo "The workshop is off. The clones are still in /var/lib/douglas-work."
    exit 0
fi

command -v git >/dev/null || { echo "git is not installed (apt install git)"; exit 1; }
command -v nft >/dev/null || { echo "nft is not installed (apt install nftables)"; exit 1; }
id douglas >/dev/null 2>&1 || { echo "there is no douglas user yet: run provision.sh first"; exit 1; }
# A job is fenced in by the kernel (Landlock, with its network rules: Linux 6.7).
python3 -c 'import ctypes, sys; sys.exit(0 if ctypes.CDLL(None).syscall(444, None, 0, 1) >= 4 else 1)' \
    || { echo "this kernel cannot fence a job in (it needs Landlock 4, Linux 6.7 or later): not installed"; exit 1; }

getent group douglas-spool >/dev/null || groupadd --system douglas-spool
for u in douglas-work douglas-job; do
    id "$u" >/dev/null 2>&1 \
        || useradd --system --user-group --home-dir "/var/lib/$u" --shell /usr/sbin/nologin "$u"
done
install -d -o douglas-work -g douglas-spool -m 2770 /var/lib/douglas-spool
install -d -m 755 /opt/douglas /etc/douglas
install -m 755 "$here/douglas-work.py" /opt/douglas/douglas-work.py

env=/etc/douglas/work.env
if [ ! -f "$env" ]; then
    cat > "$env" <<'CONF'
# douglas-work: repositories Douglas may clone, read and have worked on.
# Restart after editing:  sudo systemctl restart douglas-work
#
# The GitHub account a bare name is looked up on ("clone my kettle repo").
# Without it a repository has to be named as owner/name.
WORK_OWNER=
# For private repositories: a fine-grained personal access token, limited to
# the repositories he may read, with "Contents: Read-only" and nothing else.
# He never pushes, so it needs no write access. Put it here with
#     sudo bash -c 'read -rs t && printf "WORK_GITHUB_TOKEN=%s\n" "$t" >> /etc/douglas/work.env'
# (typed or pasted at the prompt, so that it is in no command line and no
# shell history), then restart douglas-work. Without it he sees public
# repositories only. What is cloned with it is treated as private: once he
# has read such code, he fetches nothing from the web for the rest of that
# conversation.
#WORK_GITHUB_TOKEN=
# The largest repository cloned, in MB.
WORK_MAX_MB=500
# How long one job may go on: steps of the model, and seconds of work.
WORK_STEPS=60
WORK_SECONDS=1800
CONF
fi
chown root:douglas-work "$env" && chmod 640 "$env"

port="$(envval /etc/douglas/llama.env LLAMA_PORT)"; port="${port:-8081}"
env=/etc/douglas/job.env
if [ ! -f "$env" ]; then
    cat > "$env" <<CONF
# douglas-job: the runner of Douglas's workshop. No secrets belong in here.
# Restart after editing:  sudo systemctl restart douglas-job
#
# The model server a job thinks with. Change the port here and re-run
# work-setup.sh, which writes the firewall rule to match.
LLM_URL=http://127.0.0.1:$port
# The model server slot jobs keep to, if it has one to spare (see README).
#WORK_SLOT=2
CONF
    chmod 644 "$env"
fi
port="$(envval "$env" LLM_URL | sed -n 's#.*:\([0-9][0-9]*\)/*$#\1#p')"; port="${port:-8081}"
cat > /etc/douglas/job-fence.nft <<CONF
# Written by douglas/work-setup.sh. Whatever runs as douglas-job may open a
# TCP connection to the model server on this machine and nothing else: no
# other port here, no other machine, no other protocol. Traffic of every
# other user is not looked at.
table inet douglas_job
delete table inet douglas_job
table inet douglas_job {
    chain out {
        type filter hook output priority filter; policy accept;
        meta skuid "douglas-job" jump only_the_model
    }
    chain only_the_model {
        oifname "lo" ip daddr 127.0.0.1 tcp dport $port accept
        meta l4proto tcp counter reject with tcp reset
        counter reject
    }
}
CONF
chmod 644 /etc/douglas/job-fence.nft

for u in $units; do
    install -m 644 "$here/$u" "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable douglas-job-fence.service douglas-work.socket douglas-work.service douglas-job.service
systemctl restart douglas-job-fence.service
systemctl start douglas-work.socket
systemctl restart douglas-work.service douglas-job.service

if [ -f "$voice" ] && ! grep -q '^WORK_SOCKET=' "$voice"; then
    printf '\n# The workshop (douglas-work): repositories he may clone, read and have worked on.\nWORK_SOCKET=/run/douglas-work.sock\n' >> "$voice"
    echo "WORK_SOCKET is in $voice now: restart douglas-voice when nobody is talking to him."
fi

sleep 1
for u in douglas-job-fence douglas-work douglas-job; do
    systemctl is-active --quiet "$u" || { echo "$u is not running - journalctl -u $u"; exit 1; }
done
# Asked the way the voice pipeline asks: as its user, through the socket.
if answer="$(runuser -u douglas -- python3 -c '
import json, socket
s = socket.socket(socket.AF_UNIX); s.settimeout(10); s.connect("/run/douglas-work.sock")
s.sendall(b"{\"op\": \"repos\"}\n"); print(json.loads(s.makefile().readline())["text"])')"; then
    echo "douglas-work is up. $answer"
else
    echo "douglas-work did not answer the voice pipeline's user - journalctl -u douglas-work"; exit 1
fi
