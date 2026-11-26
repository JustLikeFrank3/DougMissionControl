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
#            in, with no network but its own loopback), and the group
#            douglas-spool they pass files through
#   code     /opt/douglas/douglas-work.py
#   units    douglas-work.socket, douglas-work, douglas-job
#   config   /etc/douglas/work.env (written only if missing)
#   and      WORK_SOCKET in /etc/douglas/voice.env, if it has none
# The clones are in /var/lib/douglas-work, what the two pass each other in
# /var/lib/douglas-spool, and a job's work tree in the runner's private
# /var/tmp, which is gone when the job is.
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
units="douglas-work.socket douglas-work.service douglas-job.service"
voice=/etc/douglas/voice.env

if [ "${1:-on}" = off ]; then
    systemctl disable --now douglas-job.service douglas-work.service douglas-work.socket \
        2>/dev/null || true
    if [ -f "$voice" ] && grep -q '^WORK_SOCKET=' "$voice"; then
        sed -i '/^# The workshop (douglas-work)/d; /^WORK_SOCKET=/d' "$voice"
        echo "WORK_SOCKET is out of $voice: restart douglas-voice when nobody is talking to him."
    fi
    echo "The workshop is off. The clones are still in /var/lib/douglas-work."
    exit 0
fi

command -v git >/dev/null || { echo "git is not installed (apt install git)"; exit 1; }
id douglas >/dev/null 2>&1 || { echo "there is no douglas user yet: run provision.sh first"; exit 1; }
# gh, GitHub's own command line: what he reads a pull request with (any of
# them, its diff, what was written on it). The manager runs it, with the
# token from work.env, to read and for nothing else. Without it the rest
# works as before, and he says that gh is not installed.
if ! command -v gh >/dev/null; then
    apt-get install -y gh >/dev/null 2>&1 \
        || echo "gh could not be installed (apt install gh): he will not be able to read pull requests with it"
fi
# A job is fenced in by the kernel (Landlock 6 or later: Linux 6.12).
python3 -c 'import ctypes, sys; sys.exit(0 if ctypes.CDLL(None).syscall(444, None, 0, 1) >= 6 else 1)' \
    || { echo "this kernel cannot fence a job in (it needs Landlock 6, Linux 6.12 or later): not installed"; exit 1; }

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
# For private repositories, and for pushing: a fine-grained personal access
# token, limited to the repositories he may work with. To read private ones
# it needs "Contents: Read-only" and nothing else. To push a job's branch and
# open a pull request for it (WORK_PUSH, below) it needs "Contents: Read and
# write" and "Pull requests: Read and write", and no more. Put it here with
#     sudo bash -c 'read -rs t && printf "WORK_GITHUB_TOKEN=%s\n" "$t" >> /etc/douglas/work.env'
# (typed or pasted at the prompt, so that it is in no command line and no
# shell history), then restart douglas-work. Without it he sees public
# repositories only. What is cloned with it is treated as private: once he
# has read such code, he fetches nothing from the web for the rest of that
# conversation.
#WORK_GITHUB_TOKEN=
# May he push a job's branch to GitHub and open a pull request for it? 1 for
# yes. Even then nothing is pushed unless someone asks him to, hears read
# back what would go, and answers yes: one push, of that job's branch as it
# stood when it was read back. Never another branch, never on his own idea.
#WORK_PUSH=1
# Where a repository's work goes, when that is not its default branch:
# "owner/name=branch", one or more, with spaces between. The code is read
# from that branch, a job starts from it, and its pull request is opened
# against it. (A repository whose rule is "branch, pull request, qa, main"
# wants its name and =qa here.)
#WORK_BASE=
# The largest repository cloned, in MB.
WORK_MAX_MB=500
# How long one job may go on: steps of the model, and seconds of work.
WORK_STEPS=100
WORK_SECONDS=3600
# 0: a job's changes are not read over a second time before it ends.
#WORK_REVIEW=1
# The model server a job thinks with. A job has no network of its own:
# douglas-work asks on its behalf.
#LLM_URL=http://127.0.0.1:8081
# The model server slot jobs keep to, if it has one to spare (see README).
#WORK_SLOT=2
CONF
fi
chown root:douglas-work "$env" && chmod 640 "$env"

# What an earlier version of this installed and no longer needs: the runner
# had the model server's port and a firewall rule to keep it to that. It has
# no network at all now.
if [ -e /etc/systemd/system/douglas-job-fence.service ]; then
    systemctl disable --now douglas-job-fence.service 2>/dev/null || true
    rm -f /etc/systemd/system/douglas-job-fence.service
fi
nft delete table inet douglas_job 2>/dev/null || true
# What job.env said that was not the default now belongs in work.env: the
# manager is the one that asks the model.
if [ -f /etc/douglas/job.env ]; then
    for key in LLM_URL WORK_SLOT; do
        value="$(envval /etc/douglas/job.env "$key")"
        if [ -n "$value" ] && [ "$value" != "http://127.0.0.1:8081" ] && ! grep -q "^$key=" /etc/douglas/work.env; then
            printf '%s=%s\n' "$key" "$value" >> /etc/douglas/work.env
        fi
    done
fi
rm -f /etc/douglas/job-fence.nft /etc/douglas/job.env
# The runner's work trees live in its unit's private /var/tmp now, which
# systemd clears; it has no directory of its own any more.
rm -rf /var/lib/douglas-job

for u in $units; do
    install -m 644 "$here/$u" "/etc/systemd/system/$u"
done
# The door is for the voice pipeline's own group. Where the pipeline has not
# been given a user of its own yet (douglas/voice-user.sh), it is still the
# shared one's, as it used to be.
if ! getent group douglas-voice >/dev/null; then
    sed -i 's/^SocketGroup=douglas-voice$/SocketGroup=douglas/' /etc/systemd/system/douglas-work.socket
    sed -i 's/^Environment=WORK_CLIENTS=douglas-voice$/Environment=WORK_CLIENTS=douglas/' /etc/systemd/system/douglas-work.service
fi
systemctl daemon-reload
systemctl enable douglas-work.socket douglas-work.service douglas-job.service
systemctl start douglas-work.socket
systemctl restart douglas-work.service douglas-job.service

if [ -f "$voice" ] && ! grep -q '^WORK_SOCKET=' "$voice"; then
    printf '\n# The workshop (douglas-work): repositories he may clone, read and have worked on.\nWORK_SOCKET=/run/douglas-work.sock\n' >> "$voice"
    echo "WORK_SOCKET is in $voice now: restart douglas-voice when nobody is talking to him."
fi

sleep 1
for u in douglas-work douglas-job; do
    systemctl is-active --quiet "$u" || { echo "$u is not running - journalctl -u $u"; exit 1; }
done
# Asked the way the voice pipeline asks: as its user, through the socket.
voice_user="$(systemctl show douglas-voice -p User --value 2>/dev/null)"
if answer="$(runuser -u "${voice_user:-douglas}" -- python3 -c '
import json, socket
s = socket.socket(socket.AF_UNIX); s.settimeout(10); s.connect("/run/douglas-work.sock")
s.sendall(b"{\"op\": \"repos\"}\n"); print(json.loads(s.makefile().readline())["text"])')"; then
    echo "douglas-work is up. $answer"
else
    echo "douglas-work did not answer the voice pipeline's user - journalctl -u douglas-work"; exit 1
fi
