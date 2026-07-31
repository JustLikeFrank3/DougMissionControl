#!/bin/bash
# flightsim-boot.sh — bring the dual-boot workstation up in Windows from
# whatever state it is in. Runs ON THE PI (installed to /usr/local/bin by
# scripts/flightsim/pi-setup.sh), fired by the fauxmo "flight sim" device
# when the Alexa routine turns it on.
#
# State detection reuses the wallboard's exporters (only one OS runs at a
# time — see k8s/monitoring/pi/prometheus-pi.yaml):
#   :9106 answers  -> Windows is up (gpu-exporter.ps1)   -> nothing to do
#   :9105 answers  -> Linux is up (ollama-exporter.py)   -> ssh the
#                     forced-command key: grub-reboot Windows + reboot
#   no ping        -> powered off -> WOL magic packet; if GRUB's default
#                     lands it in Linux, the poll loop chains through the
#                     ssh path once :9105 appears
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
POLL_SECS="${POLL_SECS:-300}"              # give WOL + a chained double boot time

log() { logger -t flightsim-boot -- "$*"; echo "$(date '+%H:%M:%S') $*"; }

if [ "${1:-}" = "bg" ]; then
    nohup "$0" run >>"$HOME/flightsim-boot.log" 2>&1 &
    exit 0
fi

# One run at a time — a second "Alexa, flight sim bootup" while a boot is
# in flight must not fire a second WOL/ssh volley.
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
    # The key is command=-restricted on the workstation to
    # `sudo /usr/local/bin/boot-to-windows` — whatever we exec, that runs.
    ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=5 \
        -o StrictHostKeyChecking=accept-new "$LINUX_SSH" boot
}

log "trigger received — probing workstation state"

if win_up; then
    log "Windows already up — nothing to do"
    exit 0
fi

ssh_done=0
if linux_up; then
    log "Linux is up — requesting grub-reboot into Windows"
    if boot_to_windows; then ssh_done=1; else log "WARN: ssh reboot request failed"; fi
elif pingable; then
    log "host answers ping but no exporter yet (mid-boot?) — watching"
else
    log "no response — sending WOL to ${WS_MAC}"
    send_wol; sleep 2; send_wol
fi

deadline=$(( $(date +%s) + POLL_SECS ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    sleep 10
    if win_up; then
        log "Windows is up — flight deck online"
        exit 0
    fi
    if [ "$ssh_done" -eq 0 ] && linux_up; then
        # Cold boot landed in Linux (GRUB default) — chain through.
        log "Linux came up after WOL — requesting grub-reboot into Windows"
        if boot_to_windows; then ssh_done=1; else log "WARN: ssh reboot request failed"; fi
    fi
done

log "gave up after ${POLL_SECS}s — machine not in Windows (check BIOS WOL / logon task)"
exit 1
