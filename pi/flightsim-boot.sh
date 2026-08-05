#!/bin/bash
# flightsim-boot.sh — bring the dual-boot workstation up in a requested OS
# from whatever state it is in. Runs ON THE PI (installed to
# /usr/local/bin by pi/setup.sh), fired by the fauxmo devices when an
# Alexa routine turns one on:
#
#   flightsim-boot.sh [windows|linux] [bg|run]     (default: windows)
#
# State detection borrows two Prometheus exporters that a separate
# project (a Grafana wallboard) installs on the workstation — only one OS
# runs at a time, so exactly one of them answers:
#   :9106 answers -> Windows is up (that project's gpu-exporter.ps1)
#   :9105 answers -> Linux is up (its ollama-exporter.py)
# Without that stack, point WIN_PORT/LINUX_PORT at anything that answers
# HTTP under one OS only — e.g. the boot agent's own :9107/status.
#
# target windows: Linux up -> ssh forced-command key (grub-reboot + reboot);
#   off -> WOL, chaining through the ssh path if GRUB lands in Linux.
# target linux: Windows up -> token-guarded boot-agent on :9107 reboots it
#   (GRUB saved default IS Linux, so a plain reboot lands there); off ->
#   WOL boots straight to the Linux default. NOTE: this leg assumes the
#   saved default is Linux — if you flip the default to Windows for faster
#   cold sim starts, the linux target's Windows-up leg cannot work.
#
# "bg" arg: re-exec detached so fauxmo's on() returns immediately.
set -u

CONF=/etc/flightsim/boot.env
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"

WS_LAN="${WS_LAN:-192.168.1.50}"          # wired NIC, DHCP-by-MAC — same IP either OS
WS_MAC="${WS_MAC:-AA:BB:CC:DD:EE:FF}"      # that NIC's MAC (WOL target)
WS_BROADCAST="${WS_BROADCAST:-192.168.1.255}"
WIN_PORT="${WIN_PORT:-9106}"
LINUX_PORT="${LINUX_PORT:-9105}"
# Direct-link address is Linux-only config on the workstation, but we ssh
# only when :9105 says Linux is up, so it is always valid when used.
LINUX_SSH="${LINUX_SSH:-user@192.168.100.1}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/flightsim_ed25519}"
WIN_AGENT_PORT="${WIN_AGENT_PORT:-9107}"
WIN_AGENT_TOKEN="${WIN_AGENT_TOKEN:-}"     # set in boot.env; see boot-agent.ps1
LINUX_AGENT_PORT="${LINUX_AGENT_PORT:-9108}"
LINUX_AGENT_TOKEN="${LINUX_AGENT_TOKEN:-}" # set in boot.env; see linux/boot-agent.py
POLL_SECS="${POLL_SECS:-300}"              # give WOL + a chained double boot time
# Why this Windows boot was requested: "sim" (flight deck: greeting +
# MSFS) or "plain" (greeting only). Set per fauxmo device via env, read
# back by jarvis-greeting.ps1 over ssh at logon.
FLIGHT_INTENT="${FLIGHT_INTENT:-sim}"
INTENT_FILE=/tmp/flightsim-intent

TARGET=windows
case "${1:-}" in windows|linux) TARGET="$1"; shift ;; esac

log() { logger -t flightsim-boot -- "[$TARGET] $*"; echo "$(date '+%H:%M:%S') [$TARGET] $*"; }

if [ "${1:-}" = "bg" ]; then
    nohup "$0" "$TARGET" run >>"$HOME/flightsim-boot.log" 2>&1 &
    exit 0
fi

# One run at a time — a second trigger while a boot is in flight must not
# fire a second WOL/reboot volley.
exec 9>/tmp/flightsim-boot.lock
flock -n 9 || { log "already running — ignoring duplicate trigger"; exit 0; }

win_up()   { curl -sf --max-time 3 "http://${WS_LAN}:${WIN_PORT}/"   >/dev/null; }
linux_up() { curl -sf --max-time 3 "http://${WS_LAN}:${LINUX_PORT}/" >/dev/null; }
pingable() { ping -c1 -W1 "$WS_LAN" >/dev/null 2>&1; }

send_wol() {
    python3 - "$WS_MAC" "$WS_BROADCAST" <<'PY'
import socket, sys
mac = bytes.fromhex(sys.argv[1].replace(":", "").replace("-", ""))
pkt = b"\xff" * 6 + mac * 16
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
for port in (9, 7):
    s.sendto(pkt, (sys.argv[2], port))
PY
}

boot_to_windows() {
    # Prefer the Linux-side agent when a token is configured; it avoids the
    # ssh key dependency entirely. The agent refuses an unauthenticated
    # /reboot, so with no token set we skip straight to ssh rather than
    # firing a request that could only ever come back 403.
    if [ -n "$LINUX_AGENT_TOKEN" ] && curl -sf --max-time 3 \
        "http://${WS_LAN}:${LINUX_AGENT_PORT}/reboot?token=${LINUX_AGENT_TOKEN}" \
        >/dev/null 2>&1; then
        return 0
    fi
    # The key is command=-restricted on the workstation to
    # `sudo /usr/local/bin/boot-to-windows` — whatever we exec, that runs.
    ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=5 \
        -o StrictHostKeyChecking=accept-new "$LINUX_SSH" boot
}

boot_to_linux() {
    # Windows boot-agent (windows/boot-agent.ps1): plain reboot,
    # which lands in the GRUB saved default = Linux.
    [ -n "$WIN_AGENT_TOKEN" ] || { log "WARN: WIN_AGENT_TOKEN unset in $CONF"; return 1; }
    curl -sf --max-time 5 "http://${WS_LAN}:${WIN_AGENT_PORT}/reboot?token=${WIN_AGENT_TOKEN}" >/dev/null
}

if [ "$TARGET" = windows ]; then
    target_up() { win_up; }
    other_up()  { linux_up; }
    kick()      { boot_to_windows; }
else
    target_up() { linux_up; }
    other_up()  { win_up; }
    kick()      { boot_to_linux; }
fi

log "trigger received — probing workstation state"

# Record the intent for the Windows logon greeting to read (sim vs plain).
[ "$TARGET" = windows ] && echo "$FLIGHT_INTENT $(date +%s)" > "$INTENT_FILE"

if target_up; then
    if [ "$TARGET" = windows ] && [ "$FLIGHT_INTENT" != plain ] && [ -n "$WIN_AGENT_TOKEN" ]; then
        # Already in Windows but a launch profile was asked for — fire the
        # greeting + launch remotely instead of doing nothing.
        log "windows already up — launching '$FLIGHT_INTENT' via boot-agent"
        curl -sf --max-time 5 "http://${WS_LAN}:${WIN_AGENT_PORT}/launch?token=${WIN_AGENT_TOKEN}" >/dev/null \
            || log "WARN: launch request failed"
    else
        log "$TARGET already up — nothing to do"
    fi
    exit 0
fi

kicked=0
if other_up; then
    log "other OS is up — requesting reboot into $TARGET"
    if kick; then kicked=1; else log "WARN: reboot request failed"; fi
elif pingable; then
    log "host answers ping but no exporter yet (mid-boot?) — watching"
else
    log "no response — sending WOL to ${WS_MAC}"
    send_wol; sleep 2; send_wol
fi

deadline=$(( $(date +%s) + POLL_SECS ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    sleep 10
    if target_up; then
        log "$TARGET is up — deck online"
        exit 0
    fi
    if [ "$kicked" -eq 0 ] && other_up; then
        # Cold boot landed in the other OS — chain through.
        log "other OS came up after WOL — requesting reboot into $TARGET"
        if kick; then kicked=1; else log "WARN: reboot request failed"; fi
    fi
done

log "gave up after ${POLL_SECS}s — machine not in $TARGET (check BIOS WOL / logon task)"
exit 1
