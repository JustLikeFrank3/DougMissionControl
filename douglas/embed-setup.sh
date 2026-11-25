#!/usr/bin/env bash
# douglas/embed-setup.sh — let Douglas's memory find things by their sense:
# a small embedding model on the CPU (douglas-embed), which douglas-memory
# asks for a vector of each memory and of each question.
#
# Run ON DOUGLAS, from a checkout, after memory-setup.sh:
#     sudo ./douglas/embed-setup.sh            on, with the model in /etc/douglas/embed.env
#     sudo ./douglas/embed-setup.sh off        words only again, as before
#
# The model file is not fetched here: it is put in /srv/models/embed by hand,
# with the owner's yes (which, from where, how big). EMBED_MODEL in
# /etc/douglas/embed.env says which file; EMBED_QUERY and EMBED_DOC in
# /etc/douglas/memory.env say how that model wants to be asked, and EMBED_MIN
# and EMBED_SAME the thresholds measured for it with embed-eval.py.
#
# What it leaves behind:
#   user     douglas-embed (the service: no network, reads /srv/models)
#   unit     douglas-embed.service, and a drop-in for douglas-memory that
#            gives it the group douglas-embed and nothing else
#   config   /etc/douglas/embed.env (written only if missing), and EMBED_*
#            lines in /etc/douglas/memory.env where it has none
# It restarts douglas-memory (a second or two in which a recall waits; the
# voice pipeline goes on without a memory that does not answer in a second).
# The vectors are made in the background after that. `off` takes the lines and
# the drop-in away again and stops the service: recall is words only, and the
# vectors in the database are left unused (a memory's vector still goes with
# it whenever it is changed, forgotten or purged).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
env=/etc/douglas/embed.env
menv=/etc/douglas/memory.env
dropin=/etc/systemd/system/douglas-memory.service.d/embed.conf

if [ "${1:-on}" = off ]; then
    systemctl disable --now douglas-embed.service 2>/dev/null || true
    rm -f "$dropin"
    rmdir /etc/systemd/system/douglas-memory.service.d 2>/dev/null || true
    [ -f "$menv" ] && sed -i "/^# Finding memories by their sense (embed-setup.sh)/d; /^EMBED_[A-Z]*=/d" "$menv"
    systemctl daemon-reload
    systemctl try-restart douglas-memory
    echo "douglas-embed is off: Douglas's memory is searched by its words only."
    exit 0
fi

[ -f /opt/douglas/douglas-memory.py ] || { echo "douglas-memory is not installed: run memory-setup.sh first"; exit 1; }
[ -x /opt/llama.cpp/build/bin/llama-server ] || { echo "no llama-server at /opt/llama.cpp/build/bin"; exit 1; }

getent group douglas-embed >/dev/null || groupadd --system douglas-embed
id douglas-embed >/dev/null 2>&1 \
    || useradd --system --gid douglas-embed --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin douglas-embed
install -d -m 755 /srv/models/embed

if [ ! -f "$env" ]; then
    cat > "$env" <<'CONF'
# douglas-embed: the embedding model douglas-memory finds memories with.
# Restart after editing:  sudo systemctl restart douglas-embed
# (douglas-memory notices a new model by itself and makes every vector again.)
EMBED_MODEL=/srv/models/embed/embeddinggemma-300M-Q8_0.gguf
# CPU threads. Few on purpose: the GPU and most cores are the language model's.
EMBED_THREADS=4
#EMBED_ARGS=
CONF
    chmod 644 "$env"
fi
# shellcheck disable=SC1090
model="$(. "$env"; echo "$EMBED_MODEL")"
[ -f "$model" ] || { echo "no model at $model: put it there first (see the top of this script)"; exit 1; }
chmod 644 "$model"

if ! grep -q '^EMBED_SOCKET=' "$menv"; then
    cat >> "$menv" <<'CONF'
# Finding memories by their sense (embed-setup.sh): measured with embed-eval.py
EMBED_SOCKET=/run/douglas-embed/embed.sock
CONF
fi
for kv in "EMBED_DIM=768" "EMBED_MIN=0.30" "EMBED_SAME=0.97"; do
    grep -q "^${kv%%=*}=" "$menv" || echo "$kv" >> "$menv"
done

install -m 644 "$here/douglas-embed.service" /etc/systemd/system/douglas-embed.service
install -d -m 755 "$(dirname "$dropin")"
printf '# Written by embed-setup.sh: douglas-memory may open the embedding service'"'"'s socket.\n[Unit]\nWants=douglas-embed.service\nAfter=douglas-embed.service\n[Service]\nSupplementaryGroups=douglas-embed\n' > "$dropin"
install -m 755 "$here/douglas-memory.py" /opt/douglas/douglas-memory.py
systemctl daemon-reload
systemctl enable --now douglas-embed.service >/dev/null 2>&1
for _ in $(seq 1 30); do [ -S /run/douglas-embed/embed.sock ] && break; sleep 1; done
[ -S /run/douglas-embed/embed.sock ] || { echo "douglas-embed did not come up - journalctl -u douglas-embed"; exit 1; }
systemctl restart douglas-memory

# Who can reach it: douglas-memory's user with the group, and nobody without.
ask='
import socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(10)
try:
    s.connect(sys.argv[1]); s.sendall(b"GET /v1/models HTTP/1.0\r\n\r\n"); print(s.recv(12).decode())
except PermissionError:
    print("refused")'
mine="$(runuser -u douglas-memory -g douglas-memory -G douglas-embed -- python3 -I -c "$ask" /run/douglas-embed/embed.sock 2>&1 || true)"
voice="$(runuser -u "$(systemctl show douglas-voice -p User --value 2>/dev/null || echo douglas)" -- python3 -I -c "$ask" /run/douglas-embed/embed.sock 2>&1 || true)"
case "$mine" in HTTP/1.?\ 200*) ;; *) echo "douglas-embed did not answer douglas-memory ($mine)"; exit 1 ;; esac
[ "$voice" = refused ] || { echo "the embedding service opens to the voice pipeline ($voice): not right"; exit 1; }
echo "douglas-embed is up and answers douglas-memory only. The vectors are being made in the background:"
echo "  sudo -u douglas-memory python3 -I /opt/douglas/douglas-memory.py check"
