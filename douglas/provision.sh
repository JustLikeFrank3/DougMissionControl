#!/usr/bin/env bash
# douglas/provision.sh — take a fresh Ubuntu Server 24.04 to a working Douglas.
#
# Run ON DOUGLAS (the GEEKOM A9 Mega), from a checkout:
#
#     sudo ./douglas/provision.sh            # then reboot once, then run it again
#
# What it leaves behind, all as systemd units:
#     tailscaled          joined to the tailnet as `douglas`
#     llama-server        llama.cpp (Vulkan/RADV) on :8081, --metrics, OpenAI API
#     llama-eyes          a small vision model on :8082, so Douglas can be shown
#                         a picture (optional: enabled once its model is on disk
#                         and voice.env's EYES_URL points at it; a brain that
#                         sees for itself needs none)
#     douglas-draw        an image model on :8083, so Douglas can draw one
#                         (optional: built and enabled once its model is on disk)
#     open-webui          chat on :8080 (and https://douglas.<tailnet>.ts.net)
#     wyoming-whisper     faster-whisper STT on :10300
#     wyoming-piper       Piper TTS on :10200 (the fallback voice)
#     douglas-tts         Kokoro, the voice he speaks with, on :10210 (enabled
#                         once its model is on disk)
#     douglas-voice       DougVoice's pipeline, PTT on :10800
#     douglas-agent       the deck's window into all of it, :9111 (./setup.sh)
#
# Idempotent: every config file under /etc/douglas is written only if it is
# missing, so re-running upgrades code and units and keeps your settings.
# The GTT change needs a reboot; the first run says so and the second run
# (after it) is the one whose health check means something.
#
# Knobs (environment, all optional):
#     TS_AUTHKEY=tskey-...   join the tailnet unattended (else a login URL is printed)
#     GTT_GB=110             unified memory the GPU may map for a model
#     LLAMA_REF=master       llama.cpp git ref to build
#     SD_REF=master-1117-0d71f96   stable-diffusion.cpp git ref to build (the image server)
#     REBUILD_LLAMA=1        rebuild llama.cpp even if the ref has not moved
#     MESA_PPA=1             add ppa:kisak/kisak-mesa if RADV is too old for gfx1151
#     OPEN_WEBUI_HTTPS=0     skip `tailscale serve` (phones need HTTPS for the mic)
#     SKIP_APT=1             skip apt (re-runs on a box that is already set up)
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }

GTT_GB="${GTT_GB:-110}"
LLAMA_REF="${LLAMA_REF:-master}"
SD_REF="${SD_REF:-master-1117-0d71f96}"
OPEN_WEBUI_HTTPS="${OPEN_WEBUI_HTTPS:-1}"
reboot_needed=0
step() { printf '\n== %s\n' "$*"; }
warn() { printf '!! %s\n' "$*" >&2; }
# A value from a systemd EnvironmentFile, without sourcing it as shell.
envval() { sed -n "s/^$2=//p" "$1" | tail -n1 | sed 's/^"\(.*\)"$/\1/'; }

. /etc/os-release
[ "${VERSION_ID:-}" = "24.04" ] || warn "written for Ubuntu 24.04, this is ${PRETTY_NAME:-unknown}"

# ── identity ─────────────────────────────────────────────────────────────
step "hostname douglas"
if [ "$(hostname)" != douglas ]; then
    hostnamectl set-hostname douglas
    sed -i 's/^127\.0\.1\.1\s.*/127.0.1.1 douglas/' /etc/hosts
    grep -q '^127\.0\.1\.1' /etc/hosts || echo '127.0.1.1 douglas' >> /etc/hosts
fi

# ── packages ─────────────────────────────────────────────────────────────
if [ "${SKIP_APT:-0}" != 1 ]; then
    step "packages (HWE kernel, Mesa/Vulkan, build tools)"
    export DEBIAN_FRONTEND=noninteractive
    if [ "${MESA_PPA:-0}" = 1 ]; then
        apt-get install -y software-properties-common
        add-apt-repository -y ppa:kisak/kisak-mesa
    fi
    apt-get update
    # Strix Halo (gfx1151) needs a newer kernel than 24.04's GA 6.8 for its
    # GPU and a Mesa new enough for RADV to know it: the HWE stack has both.
    apt-get install -y \
        linux-generic-hwe-24.04 \
        mesa-vulkan-drivers vulkan-tools libvulkan-dev glslc \
        spirv-headers glslang-tools \
        build-essential cmake git curl ca-certificates pkg-config \
        libcurl4-openssl-dev libssl-dev \
        python3 python3-venv python3-pip \
        lm-sensors
fi

# ── tailnet ──────────────────────────────────────────────────────────────
step "tailscale"
if ! command -v tailscale >/dev/null; then
    curl -fsSL https://tailscale.com/install.sh | sh
fi
systemctl enable --now tailscaled
if tailscale status >/dev/null 2>&1; then
    tailscale set --hostname=douglas
else
    tailscale up --hostname=douglas ${TS_AUTHKEY:+--auth-key="$TS_AUTHKEY"} \
        || warn "tailscale up failed - run 'sudo tailscale up --hostname=douglas' by hand"
fi

# ── unified memory for the GPU ───────────────────────────────────────────
# The GPU maps system RAM through GTT, which the kernel caps at half of it by
# default. Raise TTM's page limit (and amdgpu's GTT size, for kernels that
# still read it) so ~GTT_GB of the 128 GB can hold one model. Set the BIOS
# UMA frame buffer to its minimum: carved-out VRAM is RAM GTT cannot have.
step "GTT/TTM limit: ${GTT_GB} GiB"
pages=$(( GTT_GB * 1024 * 1024 / 4 ))          # 4 KiB pages
gtt_args="amdgpu.gttsize=$(( GTT_GB * 1024 )) ttm.pages_limit=${pages} ttm.page_pool_size=${pages}"
want="GRUB_CMDLINE_LINUX_DEFAULT=\"\$GRUB_CMDLINE_LINUX_DEFAULT ${gtt_args}\""
cfg=/etc/default/grub.d/90-douglas-gtt.cfg
if [ "$(cat "$cfg" 2>/dev/null)" != "$want" ]; then
    install -d /etc/default/grub.d
    echo "$want" > "$cfg"
    update-grub
fi
grep -q "ttm.pages_limit=${pages}" /proc/cmdline || reboot_needed=1

# ── the service user and its places ──────────────────────────────────────
step "user douglas, /srv/models, /etc/douglas"
id douglas >/dev/null 2>&1 || useradd --system --home-dir /var/lib/douglas \
    --shell /usr/sbin/nologin --groups render,video douglas
usermod -aG render,video douglas
install -d -o douglas -g douglas -m 755 /srv/models
install -d -m 755 /etc/douglas /opt/douglas

# ── llama.cpp ────────────────────────────────────────────────────────────
# Vulkan (RADV) rather than ROCm: on gfx1151 it matches or beats ROCm for
# token generation, is in Ubuntu's own archive, and has no 20 GB toolkit to
# keep in step with the kernel. Swap later by rebuilding with -DGGML_HIP=ON.
step "llama.cpp @ ${LLAMA_REF} (Vulkan)"
src=/opt/llama.cpp
[ -d "$src/.git" ] || git clone https://github.com/ggml-org/llama.cpp "$src"
git -C "$src" fetch --tags --force origin
git -C "$src" checkout -q --detach "origin/${LLAMA_REF}" 2>/dev/null \
    || git -C "$src" checkout -q --detach "${LLAMA_REF}"
built="$src/build/.douglas-built-rev"
rev="$(git -C "$src" rev-parse HEAD)"
if [ "${REBUILD_LLAMA:-0}" = 1 ] || [ ! -x "$src/build/bin/llama-server" ] \
        || [ "$(cat "$built" 2>/dev/null)" != "$rev" ]; then
    cmake -S "$src" -B "$src/build" -DCMAKE_BUILD_TYPE=Release \
        -DGGML_VULKAN=ON -DLLAMA_CURL=ON -DLLAMA_BUILD_TESTS=OFF
    cmake --build "$src/build" --config Release -j "$(nproc)" --target llama-server llama-cli llama-bench
    echo "$rev" > "$built"
fi
if command -v vulkaninfo >/dev/null && ! vulkaninfo --summary 2>/dev/null | grep -qi 'radv\|radeon'; then
    warn "Vulkan sees no Radeon yet - normal before the first reboot onto the HWE kernel;"
    warn "after it, if still missing, re-run with MESA_PPA=1"
fi

env=/etc/douglas/llama.env
[ -f "$env" ] || cat > "$env" <<'CONF'
# llama-server (llama.cpp). Restart after editing:
#     sudo systemctl restart llama-server
#
# The GGUF to serve. Nothing is downloaded for you; for example
#     sudo -u douglas curl -L -o /srv/models/gpt-oss-120b-mxfp4.gguf <url>
# or use -hf in LLAMA_ARGS instead (llama.cpp fetches it into ~douglas/.cache).
LLAMA_MODEL=/srv/models/model.gguf
LLAMA_HOST=0.0.0.0
LLAMA_PORT=8081
# -ngl 999      every layer on the GPU (it is all one memory anyway)
# --load-mode none  load into GTT up front; mmap'd weights page in slowly here
#               (llama.cpp master replaced --no-mmap with --load-mode)
# --jinja       the model's own chat template, so reasoning stays out of content
# --alias       the name /v1/models and Open WebUI show
LLAMA_ARGS="-ngl 999 --load-mode none --jinja -c 32768 --parallel 2 --alias douglas"
CONF

env=/etc/douglas/eyes.env
[ -f "$env" ] || cat > "$env" <<'CONF'
# llama-eyes: a small vision model beside Douglas's own, for when his own
# reads only text. A picture sent to him is put into words here first.
# Restart after editing:  sudo systemctl restart llama-eyes
#
# If his own model can see (llama-server was given its --mmproj in
# llama.env), he needs none of this: set EYES_URL=http://127.0.0.1:8081 in
# voice.env and leave this unit off.
#
# Otherwise: nothing is downloaded for you. Any vision model llama.cpp serves
# will do, at around 10 GB with its projector. Put the model and its mmproj
# file in /srv/models/eyes, name them below, set
# EYES_URL=http://127.0.0.1:8082 in voice.env and re-run provision.sh: it
# enables the unit once the model is there and voice.env points at it.
EYES_MODEL_FILE=/srv/models/eyes/model.gguf
EYES_MMPROJ=/srv/models/eyes/mmproj.gguf
# Loopback: only the voice pipeline on this machine talks to it.
EYES_HOST=127.0.0.1
EYES_PORT=8082
# --image-min-tokens  some vision models read small pictures poorly below
#                     about 1024
# --image-max-tokens  how much of the context one picture may take; a large
#                     photo is scaled down to fit
EYES_ARGS="-ngl 999 --load-mode none --jinja -c 8192 --parallel 1 --image-min-tokens 1024 --image-max-tokens 2048 --alias eyes"
CONF

# ── a hand: an image model, built only once there is one to run ──────────
env=/etc/douglas/draw.env
[ -f "$env" ] || cat > "$env" <<'CONF'
# douglas-draw: an image model, so that Douglas can make a picture when
# asked. Restart after editing:  sudo systemctl restart douglas-draw
#
# Nothing is downloaded for you. FLUX.1 schnell (Black Forest Labs, Apache
# 2.0; its text encoders are Google's T5-XXL and OpenAI's CLIP-L) fits beside
# a 70 GB model and draws a 1024x1024 picture in about 25 seconds on this
# machine's Radeon. It is four files, about 19 GB:
#     flux1-schnell-q8_0.gguf
#         from https://huggingface.co/leejet/FLUX.1-schnell-gguf
#     t5xxl-Q8_0.gguf, clip_l.safetensors, ae.safetensors
#         from https://huggingface.co/second-state/FLUX.1-schnell-GGUF
# Put them in /srv/models/flux1-schnell and re-run provision.sh: it builds
# stable-diffusion.cpp and enables the unit once the model is there. Then set
# IMAGE_URL=http://127.0.0.1:8083 in voice.env and restart douglas-voice.
DRAW_MODEL=/srv/models/flux1-schnell/flux1-schnell-q8_0.gguf
DRAW_VAE=/srv/models/flux1-schnell/ae.safetensors
DRAW_CLIP_L=/srv/models/flux1-schnell/clip_l.safetensors
DRAW_T5XXL=/srv/models/flux1-schnell/t5xxl-Q8_0.gguf
# Loopback: only the voice pipeline on this machine talks to it.
DRAW_HOST=127.0.0.1
DRAW_PORT=8083
# Four steps and no guidance is how FLUX.1 schnell is meant to be run.
DRAW_ARGS="--steps 4 --cfg-scale 1.0 -W 1024 -H 1024"
CONF
chown root:douglas "$env" && chmod 640 "$env"
draw="$(envval "$env" DRAW_MODEL)"
if [ -f "$draw" ]; then
    # The same choice as for llama.cpp, and the same backend: Vulkan.
    step "stable-diffusion.cpp @ ${SD_REF} (Vulkan)"
    src=/opt/stable-diffusion.cpp
    [ -d "$src/.git" ] || git clone --recursive https://github.com/leejet/stable-diffusion.cpp "$src"
    git -C "$src" fetch --tags --force origin
    git -C "$src" checkout -q --detach "${SD_REF}" 2>/dev/null \
        || git -C "$src" checkout -q --detach "origin/${SD_REF}"
    git -C "$src" submodule update --init --recursive
    built="$src/build/.douglas-built-rev"
    rev="$(git -C "$src" rev-parse HEAD)"
    if [ ! -x "$src/build/bin/sd-server" ] || [ "$(cat "$built" 2>/dev/null)" != "$rev" ]; then
        cmake -S "$src" -B "$src/build" -DCMAKE_BUILD_TYPE=Release -DSD_VULKAN=ON
        cmake --build "$src/build" --config Release -j "$(nproc)" --target sd-server sd-cli
        echo "$rev" > "$built"
    fi
fi

# ── speech ───────────────────────────────────────────────────────────────
step "Wyoming faster-whisper and Piper"
[ -x /opt/wyoming-whisper/bin/python ] || python3 -m venv /opt/wyoming-whisper
/opt/wyoming-whisper/bin/pip install -q --upgrade 'wyoming-faster-whisper>=3.9,<4'
[ -x /opt/wyoming-piper/bin/python ] || python3 -m venv /opt/wyoming-piper
/opt/wyoming-piper/bin/pip install -q --upgrade 'wyoming-piper>=2.5,<3'

env=/etc/douglas/speech.env
[ -f "$env" ] || cat > "$env" <<'CONF'
# Wyoming speech services for DougVoice. Restart after editing:
#     sudo systemctl restart wyoming-whisper wyoming-piper
# Whisper runs on the CPU (16 Zen 5 cores): ctranslate2 has no Vulkan.
WHISPER_PORT=10300
WHISPER_MODEL=small
WHISPER_LANGUAGE=en
WHISPER_COMPUTE=int8
WHISPER_ARGS="--beam-size 1"
# Any voice from https://huggingface.co/rhasspy/piper-voices; it is
# downloaded on first use into /var/lib/wyoming/piper.
PIPER_PORT=10200
PIPER_VOICE=en_GB-alan-medium
PIPER_ARGS=
CONF

# His voice. Piper is quick and sounds like a machine reading; Kokoro is a
# small neural voice that sounds like a person and still runs on the CPU.
# Piper stays: it is what the pipeline falls back to if douglas-tts is down.
step "Kokoro voice"
[ -x /opt/kokoro/bin/python ] || python3 -m venv /opt/kokoro
/opt/kokoro/bin/pip install -q --upgrade --only-binary=:all: 'kokoro-onnx>=0.6,<0.7'

env=/etc/douglas/tts.env
[ -f "$env" ] || cat > "$env" <<'CONF'
# Douglas's speaking voice: Kokoro, served by douglas-tts. Restart after editing:
#     sudo systemctl restart douglas-tts
TTS_BIND=127.0.0.1
TTS_LISTEN_PORT=10210
KOKORO_MODEL=/srv/models/kokoro/kokoro-v1.0.onnx
KOKORO_VOICES=/srv/models/kokoro/voices-v1.0.bin
# Any voice in the model: bm_lewis, bm_george, bm_fable, bm_daniel (British),
# am_michael, am_fenrir, am_puck, am_onyx, am_eric, am_liam (American), and more.
KOKORO_VOICE=bm_lewis
# Pace: 1.0 is the voice's own; 0.9 is a little slower.
KOKORO_SPEED=1.0
CONF
chown root:douglas "$env" && chmod 640 "$env"

# The model: two files, 337 MB, from the kokoro-onnx project's release. The
# release publishes no checksums, so these are the ones this was tested with.
kokoro_release=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
fetch_kokoro() {            # file, sha256
    [ -s "$1" ] && return 0
    install -d -m 755 "$(dirname "$1")"
    if curl -fsSL --retry 3 -o "$1.part" "$kokoro_release/$(basename "$1")" \
            && echo "$2  $1.part" | sha256sum -c --quiet -; then
        mv "$1.part" "$1" && chmod 644 "$1"
    else
        rm -f "$1.part"
        warn "could not fetch $(basename "$1") - Douglas will speak with Piper until it is there"
    fi
}
fetch_kokoro "$(envval "$env" KOKORO_MODEL)" 7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5
fetch_kokoro "$(envval "$env" KOKORO_VOICES)" bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d

# ── Open WebUI ───────────────────────────────────────────────────────────
step "Open WebUI"
if [ ! -x /opt/open-webui/bin/open-webui ]; then
    python3 -m venv /opt/open-webui
    # CPU torch first, or pip pulls the multi-GB CUDA build this box cannot use.
    /opt/open-webui/bin/pip install -q torch --index-url https://download.pytorch.org/whl/cpu
fi
/opt/open-webui/bin/pip install -q --upgrade open-webui

env=/etc/douglas/open-webui.env
if [ ! -f "$env" ]; then
    secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    cat > "$env" <<CONF
# Open WebUI. The first account created in the browser becomes the admin.
OPEN_WEBUI_PORT=8080
OPENAI_API_BASE_URL=http://127.0.0.1:8081/v1
OPENAI_API_KEY=none
ENABLE_OLLAMA_API=false
WEBUI_NAME=Douglas
WEBUI_SECRET_KEY=$secret
CONF
    chmod 600 "$env"
fi
chown root:douglas /etc/douglas/open-webui.env && chmod 640 /etc/douglas/open-webui.env

# ── the voice pipeline ───────────────────────────────────────────────────
step "douglas-voice"
install -m 755 "$here/douglas-voice.py" /opt/douglas/douglas-voice.py
install -m 755 "$here/douglas-tts.py" /opt/douglas/douglas-tts.py
env=/etc/douglas/voice.env
if [ ! -f "$env" ]; then
    ptt_token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
    cat > "$env" <<CONF
# douglas-voice. Restart after editing:  sudo systemctl restart douglas-voice
# DougVoice's wyoming-satellite (always-streaming: no wake word, no VAD).
VOICE_SAT_HOST=dougvoice
VOICE_SAT_PORT=10700
# Push-to-talk. DougVoice's button POSTs here too, so it listens on the
# tailnet and wants the token:  POST /ptt?token=<VOICE_PTT_TOKEN>
VOICE_PTT_HOST=0.0.0.0
VOICE_PTT_PORT=10800
VOICE_PTT_TOKEN=$ptt_token
STT_HOST=127.0.0.1
STT_PORT=10300
STT_LANGUAGE=en
TTS_HOST=127.0.0.1
# His voice: Kokoro (douglas-tts on :10210), with Piper on :10200 behind it
# for the day that service is down. Which voice is set in one place, tts.env;
# TTS_VOICE here would override it for this pipeline only.
TTS_PORT=10210
#TTS_VOICE=
TTS_FALLBACK_PORT=10200
LLM_URL=http://127.0.0.1:8081
LLM_MAX_TOKENS=400
# Lookups (weather, news, Wikipedia, reading a web page) are on. Each one
# sends its own words to a public service; DOUGLAS_TOOLS=0 turns them off.
#DOUGLAS_TOOLS=0
# Where "the weather" means when no place is named, and US units for it.
#DOUGLAS_HOME=Springfield, Illinois
#DOUGLAS_UNITS=us
# His eyes: his own model if it can see (:8081), or the vision model
# llama-eyes serves (:8082, see eyes.env). Unset, he cannot be shown a
# picture and says so.
#EYES_URL=http://127.0.0.1:8082
# His hand: the image model douglas-draw serves (see draw.env). Unset, he
# cannot draw and says so.
#IMAGE_URL=http://127.0.0.1:8083
# The owner's own records, on their jobContext server. JOBCONTEXT_AREAS
# names the parts he may reach: "stories" is the owner's stories and writing
# samples and nothing else; unset, he reaches all of it, a job search
# included. He reads them when asked. A change is read back by the pipeline
# and sent only after a yes, and nothing can be deleted. The key is a whole
# one (the server has no narrower kind), which is why this file is
# root:douglas 640 and why the address must be https. JOBCONTEXT_VIA=phone
# keeps the records to the phone page, off the microphone anyone in the room
# can press.
#JOBCONTEXT_URL=https://jobcontext.example/mcp
#JOBCONTEXT_TOKEN=
#JOBCONTEXT_AREAS=stories
#JOBCONTEXT_VIA=phone
#DOUGLAS_SYSTEM_PROMPT=You are Douglas, ...
VOICE_MAX_LISTEN_S=20
VOICE_HISTORY_TURNS=6
VOICE_HISTORY_IDLE_S=300
VOICE_STATE_FILE=/run/douglas/voice.json
TRANSCRIPT_FILE=/var/lib/douglas/transcript.jsonl
CONF
fi
chown root:douglas "$env" && chmod 640 "$env"

# ── units ────────────────────────────────────────────────────────────────
step "systemd units"
units="llama-server open-webui wyoming-whisper wyoming-piper douglas-voice"
for u in $units llama-eyes douglas-tts douglas-draw; do
    install -m 644 "$here/$u.service" "/etc/systemd/system/$u.service"
done
systemctl daemon-reload
# The eyes are optional: without their model the unit would only flap, and
# with a brain that sees for itself (EYES_URL pointing at it) they are ten
# gigabytes of memory nobody looks through.
eyes="$(envval /etc/douglas/eyes.env EYES_MODEL_FILE)"
eyes_url="$(envval /etc/douglas/voice.env EYES_URL)"
eyes_on=""
if [ -f "$eyes" ] && [ "${eyes_url##*:}" = "$(envval /etc/douglas/eyes.env EYES_PORT)" ]; then
    units="$units llama-eyes"
    eyes_on=1
elif [ -f "$eyes" ]; then
    echo "  llama-eyes left off: voice.env's EYES_URL (${eyes_url:-unset}) does not point at it"
else
    echo "  no vision model at $eyes - see /etc/douglas/eyes.env if Douglas's own model cannot see"
fi
# So is the hand: without an image model and the program that runs it there
# is nothing to start.
if [ -f "$draw" ] && [ -x /opt/stable-diffusion.cpp/build/bin/sd-server ]; then
    units="$units douglas-draw"
else
    echo "  no image model at $draw - Douglas cannot draw (see /etc/douglas/draw.env)"
fi
# So is the neural voice: without its model he speaks with Piper.
kokoro="$(envval /etc/douglas/tts.env KOKORO_MODEL)"
if [ -f "$kokoro" ] && [ -f "$(envval /etc/douglas/tts.env KOKORO_VOICES)" ]; then
    units="douglas-tts $units"
else
    echo "  no Kokoro model at $kokoro - Douglas speaks with Piper (see /etc/douglas/tts.env)"
fi
for u in $units; do
    systemctl enable "$u"
    systemctl restart "$u" || warn "$u did not start - journalctl -u $u"
done
model="$(envval /etc/douglas/llama.env LLAMA_MODEL)"
[ -f "$model" ] || warn "no model at $model yet - llama-server will retry until there is (see /etc/douglas/llama.env)"

if [ "$OPEN_WEBUI_HTTPS" = 1 ]; then
    # Browsers only allow the microphone on HTTPS, so phones talk to Open WebUI
    # through tailscale serve (needs HTTPS certificates enabled for the tailnet).
    tailscale serve --bg --https=443 http://127.0.0.1:8080 >/dev/null 2>&1 \
        || warn "tailscale serve failed - enable HTTPS in the tailnet's admin console, or OPEN_WEBUI_HTTPS=0"
fi

# ── douglas-agent, and point it at the pipeline ──────────────────────────
step "douglas-agent"
"$here/setup.sh" || warn "douglas-agent setup failed"
ptt_token="$(envval /etc/douglas/voice.env VOICE_PTT_TOKEN)"
ptt_port="$(envval /etc/douglas/voice.env VOICE_PTT_PORT)"; ptt_port="${ptt_port:-10800}"
ptt_url="http://127.0.0.1:${ptt_port}/ptt${ptt_token:+?token=$ptt_token}"
agent_env=/etc/douglas/agent.env
if ! grep -q '^VOICE_PTT_URL=' "$agent_env"; then
    sed -i '/^#VOICE_PTT_URL=/d' "$agent_env"
    echo "VOICE_PTT_URL=$ptt_url" >> "$agent_env"
    systemctl restart douglas-agent
fi

# ── what is up ───────────────────────────────────────────────────────────
step "status"
sleep 3
eyes_port=""
[ -n "$eyes_on" ] && eyes_port="$(envval /etc/douglas/eyes.env EYES_PORT) llama-eyes"
draw_port=""
[ -f "$draw" ] && draw_port="$(envval /etc/douglas/draw.env DRAW_PORT) douglas-draw"
tts_port=""
[ -f "$kokoro" ] && tts_port="$(envval /etc/douglas/tts.env TTS_LISTEN_PORT) douglas-tts"
for p in "8081 llama-server" ${eyes_port:+"$eyes_port"} ${draw_port:+"$draw_port"} "8080 open-webui" "10300 wyoming-whisper" \
         "10200 wyoming-piper" ${tts_port:+"$tts_port"} "10800 douglas-voice" "9111 douglas-agent"; do
    set -- $p
    if (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; then
        printf '  up    :%-5s %s\n' "$1" "$2"
    else
        printf '  down  :%-5s %s   (journalctl -u %s; first starts download models)\n' "$1" "$2" "$2"
    fi
done
echo
echo "Chat:    http://douglas:8080  (phone: https://$(tailscale status --json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' \
    2>/dev/null || echo 'douglas.<tailnet>.ts.net'))"
echo "PTT:     POST http://douglas:${ptt_port}/ptt?token=<VOICE_PTT_TOKEN in /etc/douglas/voice.env>"
if [ "$reboot_needed" = 1 ]; then
    echo
    echo "REBOOT NEEDED for the GTT limit (and the HWE kernel): sudo reboot, then re-run this script."
fi
