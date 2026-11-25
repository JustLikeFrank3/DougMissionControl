#!/usr/bin/env bash
# douglas/voice-user.sh — give the voice pipeline a user of its own.
#
# Run ON DOUGLAS, from a checkout:   sudo ./douglas/voice-user.sh
#                                    sudo ./douglas/voice-user.sh back
#
# The pipeline used to run as `douglas`, the user that the model server, the
# chat page and the speech and picture services run as too. So any of those
# six could write into the record of what was said (which is what Douglas's
# memories are made from), read the pipeline's settings and every key in
# them, open the door to his workshop, and read the pipeline's environment
# out of /proc. None of them has any business with any of it.
#
# After this the pipeline is `douglas-voice`, and what is its own is its
# own:
#   /var/lib/douglas             the folder, the record (still world-readable:
#                                douglas-agent reads it for the panel), his
#                                pictures and his tone file
#   /etc/douglas/voice.env       root's alone (systemd reads it for the unit),
#                                and so are the copies of it kept beside it
#   /etc/douglas/instructions.d, /etc/douglas/reference
#                                the owner's instruction files, for the
#                                pipeline's group and nobody else
#   /run/douglas-work.sock       the workshop's door, for the pipeline's group
# The other six are not touched, nor restarted: they keep `douglas`, and the
# shader cache in /var/lib/douglas/.cache that the model server and the
# picture server write stays theirs.
#
# It restarts douglas-voice (the conversation in hand is forgotten, as on any
# restart; what he remembers is not) and, to remake its door, douglas-work: a
# job under way is ended, so it refuses while one is running. Idempotent.
# `back` puts it all as it was.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
state=/var/lib/douglas
unit=/etc/systemd/system/douglas-voice.service
door=/etc/systemd/system/douglas-work.socket
shop=/etc/systemd/system/douglas-work.service
kept=/etc/douglas/.before-voice-user
mode="${1:-on}"
new=douglas-voice
[ "$mode" = back ] && new=douglas
id douglas >/dev/null 2>&1 || { echo "there is no douglas user yet: run provision.sh first"; exit 1; }

# A job that is under way would be ended by remaking the workshop's door.
if [ -S /run/douglas-work.sock ] && systemctl is-active --quiet douglas-work; then
    busy="$(runuser -u "$(stat -c %U "$state")" -- python3 -I -c '
import json, socket
s = socket.socket(socket.AF_UNIX); s.settimeout(10); s.connect("/run/douglas-work.sock")
s.sendall(b"{\"op\": \"status\"}\n"); print(json.loads(s.makefile().readline()).get("state") or "")' 2>/dev/null || true)"
    [ "$busy" != running ] || { echo "a job is under way in the workshop: run this when it is over"; exit 1; }
fi

if [ "$mode" = back ]; then
    [ -d "$kept" ] || { echo "nothing was kept from before ($kept is not there): nothing to put back"; exit 1; }
    install -m 644 "$kept/douglas-voice.service" "$unit"
    [ -f "$kept/douglas-work.socket" ] && install -m 644 "$kept/douglas-work.socket" "$door"
    [ -f "$kept/douglas-work.service" ] && install -m 644 "$kept/douglas-work.service" "$shop"
else
    getent passwd douglas-voice >/dev/null \
        || useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin douglas-voice
    if [ ! -d "$kept" ]; then                    # the two units as they were, for `back`
        install -d -m 700 "$kept"
        cp -p "$unit" "$kept/douglas-voice.service"
        [ -f "$door" ] && cp -p "$door" "$kept/douglas-work.socket"
        [ -f "$shop" ] && cp -p "$shop" "$kept/douglas-work.service"
    fi
    install -m 644 "$here/douglas-voice.service" "$unit"
    # The workshop's door, and the workshop's own list of whom it answers.
    if [ -f "$door" ]; then
        install -m 644 "$here/douglas-work.socket" "$door"
        install -m 644 "$here/douglas-work.service" "$shop"
    fi
fi

systemctl stop douglas-voice 2>/dev/null || true
# The folder and what the pipeline keeps in it. Not the shader cache, which
# is the model server's and the picture server's.
install -d -m 755 "$state"
chown "$new:$new" "$state"
for f in transcript.jsonl tone.md pictures; do
    [ -e "$state/$f" ] && chown -R "$new:$new" "$state/$f"
done
install -d -o douglas -g douglas -m 700 "$state/.cache"
# His settings, and the copies of them that were kept: every key he holds is
# in there. systemd reads the file for the unit as root; nobody else needs to.
for f in /etc/douglas/voice.env /etc/douglas/voice.env.*; do
    [ -f "$f" ] || continue
    if [ "$mode" = back ]; then chown root:douglas "$f" && chmod 640 "$f"; else chown root:root "$f" && chmod 600 "$f"; fi
done
# The owner's instruction files.
for d in /etc/douglas/instructions.d /etc/douglas/reference; do
    [ -d "$d" ] && chgrp -R "$new" "$d"
done

systemctl daemon-reload
if [ -f "$door" ]; then                          # a door cannot be remade while the service behind it is up
    systemctl stop douglas-work.service 2>/dev/null || true
    systemctl restart douglas-work.socket
    systemctl start douglas-work.service 2>/dev/null || true
fi
systemctl start douglas-voice
sleep 2
systemctl is-active --quiet douglas-voice || { echo "douglas-voice did not come up - journalctl -u douglas-voice"; exit 1; }
[ "$mode" = back ] && { rm -rf "$kept"; echo "The voice pipeline runs as douglas again, as before."; exit 0; }

# Seen from the other side: as the user the six other services run as.
try() { runuser -u douglas -- "$@" >/dev/null 2>&1 && echo open || echo shut; }
pid="$(systemctl show douglas-voice -p MainPID --value)"
said="record $(try sh -c "test -w $state/transcript.jsonl"), folder $(try sh -c "test -w $state"), settings $(try cat /etc/douglas/voice.env), instructions $(try ls /etc/douglas/instructions.d), environment $(try cat "/proc/$pid/environ"), workshop $( [ -S /run/douglas-work.sock ] && try python3 -I -c 'import socket; socket.socket(socket.AF_UNIX).connect("/run/douglas-work.sock")' || echo shut)"
case "$said" in *open*) echo "something of the pipeline's is still open to the other services: $said"; exit 1 ;; esac
# And from the pipeline's own side: the workshop answers it.
if [ -S /run/douglas-work.sock ]; then
    runuser -u douglas-voice -- python3 -I -c '
import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(10); s.connect("/run/douglas-work.sock")
s.sendall(b"{\"op\": \"repos\"}\n"); sys.exit(0 if json.loads(s.makefile().readline()).get("ok") else 1)' \
        || { echo "the workshop does not answer the pipeline under its new user - journalctl -u douglas-work"; exit 1; }
fi
runuser -u douglas -- test -r "$state/transcript.jsonl" || { echo "the record is not readable any more: the panel would lose the conversation"; exit 1; }
runuser -u douglas -- test -w "$state/.cache" || { echo "the model server's shader cache is not its own any more"; exit 1; }
echo "The voice pipeline runs as douglas-voice. To the other services its record, folder, settings, instructions, environment and workshop door are all shut ($said)."
