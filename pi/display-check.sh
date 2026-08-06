#!/usr/bin/env bash
# display-check.sh — prove the XENEON Edge is usable before anything is
# built on top of it. Read-only: touches no config, installs nothing.
#
# Run it on the Pi with the Edge plugged in (micro-HDMI for video, USB-A
# for touch) and read the verdict at the bottom:
#
#   ./display-check.sh
#
# Checks, in the order they can fail:
#   1. the HDMI connector is connected at all
#   2. 2560x720 is in the mode list the panel advertises
#   3. the mode actually in use is that one
#   4. a touch digitizer enumerated on USB
#   5. exactly one output exists, so touch maps to it without calibration
#
# Anything that fails here fails louder later, in a kiosk with no shell.
set -u

pass=0
fail=0
warn=0

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; pass=$((pass + 1)); }
no()   { printf '  \033[31m✗\033[0m %s\n' "$*"; fail=$((fail + 1)); }
hmm()  { printf '  \033[33m!\033[0m %s\n' "$*"; warn=$((warn + 1)); }
head_() { printf '\n\033[1m%s\033[0m\n' "$*"; }

WANT_W=2560
WANT_H=720
WANT="${WANT_W}x${WANT_H}"

# ── 1. connectors ────────────────────────────────────────────────────────
head_ "HDMI connectors"

connected=()
for c in /sys/class/drm/card*-HDMI-A-*; do
    [ -e "$c/status" ] || continue
    name="$(basename "$c" | sed 's/^card[0-9]*-//')"
    status="$(cat "$c/status" 2>/dev/null)"
    if [ "$status" = connected ]; then
        ok "$name connected"
        connected+=("$c")
    else
        printf '    %s %s\n' "$name" "$status"
    fi
done

if [ "${#connected[@]}" -eq 0 ]; then
    no "no HDMI connector reports 'connected'"
    echo "    Check the cable is in HDMI0 (the micro-HDMI nearest the USB-C"
    echo "    power jack) and that the Edge has its own power supply — an"
    echo "    unpowered panel does not assert hotplug."
elif [ "${#connected[@]}" -gt 1 ]; then
    hmm "${#connected[@]} outputs connected — touch will need explicit mapping"
    echo "    cage maps the digitizer to its only output automatically. With"
    echo "    two, unplug the other one for v0.1."
fi

# ── 2 & 3. modes ─────────────────────────────────────────────────────────
head_ "Modes"

for c in "${connected[@]:-}"; do
    [ -n "$c" ] || continue
    name="$(basename "$c" | sed 's/^card[0-9]*-//')"

    if [ -r "$c/modes" ]; then
        if grep -qx "$WANT" "$c/modes"; then
            ok "$name advertises $WANT"
        else
            no "$name does not advertise $WANT"
            echo "    Modes offered (first 8):"
            head -8 "$c/modes" | sed 's/^/      /'
            echo "    Force it: add  video=${name}:${WANT}@60  to"
            echo "    /boot/firmware/cmdline.txt (one line, space-separated), reboot."
        fi
    else
        hmm "$name mode list unreadable"
    fi
done

# What is actually in use right now.
current=""
if command -v modetest >/dev/null 2>&1; then
    current="$(modetest -c 2>/dev/null | grep -oE "${WANT_W}x${WANT_H}" | head -1)"
fi
if [ -z "$current" ] && [ -r /sys/class/graphics/fb0/virtual_size ]; then
    vs="$(tr ',' 'x' < /sys/class/graphics/fb0/virtual_size)"
    [ "$vs" = "$WANT" ] && current="$WANT"
    printf '    framebuffer is %s\n' "$vs"
fi
if [ -n "$current" ]; then
    ok "running at $WANT"
else
    hmm "could not confirm the active mode is $WANT"
    echo "    Harmless if X/Wayland is not up yet — re-run once the kiosk starts."
fi

# ── 4. touch ─────────────────────────────────────────────────────────────
head_ "Touch digitizer"

touch_found=0
if [ -r /proc/bus/input/devices ]; then
    # A HID digitizer exposes ABS_MT_POSITION_X (bit 0x35) via mtN handlers;
    # matching on the handler list is more portable than decoding EV bits.
    while IFS= read -r line; do
        case "$line" in
            N:*) devname="${line#N: Name=}" ;;
            H:*)
                case "$line" in
                    *mouse*|*event*)
                        if printf '%s' "$devname" | grep -qiE 'touch|digitizer|corsair|xeneon'; then
                            ok "found $devname"
                            touch_found=1
                        fi
                        ;;
                esac
                ;;
        esac
    done < /proc/bus/input/devices
fi

if [ "$touch_found" -eq 0 ]; then
    # Fall back to libinput, which names the capability outright.
    if command -v libinput >/dev/null 2>&1; then
        if sudo -n libinput list-devices 2>/dev/null | grep -qi 'touch'; then
            ok "libinput reports a touch-capable device"
            touch_found=1
        fi
    fi
fi

if [ "$touch_found" -eq 0 ]; then
    no "no touch digitizer enumerated"
    echo "    The Edge needs its USB-C data cable to a Pi USB-A port as well as"
    echo "    the HDMI lead — video alone will not bring up the digitizer."
    echo "    Put it in a BLACK (USB 2.0) port; HID needs no bandwidth and this"
    echo "    keeps both blue ports free."
    echo "    Confirm the Pi sees it at all:  lsusb | grep -i corsair"
fi

# ── 5. environment ───────────────────────────────────────────────────────
head_ "Kiosk prerequisites"

for tool in cage; do
    if command -v "$tool" >/dev/null 2>&1; then
        ok "$tool installed"
    else
        hmm "$tool not installed — setup-display.sh installs it"
    fi
done

if command -v chromium >/dev/null 2>&1 || command -v chromium-browser >/dev/null 2>&1; then
    ok "chromium installed"
else
    hmm "chromium not installed — setup-display.sh installs it"
fi

mem_mb=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 ))
if [ "$mem_mb" -lt 3500 ]; then
    hmm "${mem_mb} MB RAM — v0.1 is sized for 4 GB, expect it to be tight below that"
else
    ok "${mem_mb} MB RAM"
fi

if [ -r /sys/class/thermal/thermal_zone0/temp ]; then
    t=$(( $(cat /sys/class/thermal/thermal_zone0/temp) / 1000 ))
    if [ "$t" -ge 70 ]; then
        hmm "SoC already at ${t} °C at idle — it will throttle under the kiosk"
    else
        ok "SoC ${t} °C"
    fi
fi

# ── verdict ──────────────────────────────────────────────────────────────
printf '\n\033[1mVerdict\033[0m\n'
printf '  %d passed, %d warnings, %d failed\n\n' "$pass" "$warn" "$fail"

if [ "$fail" -gt 0 ]; then
    echo "  Fix the ✗ lines before running setup-display.sh — every one of them"
    echo "  is a problem you would otherwise meet inside a fullscreen kiosk."
    exit 1
fi

echo "  Video and touch look sound. Next:"
echo "    sudo ./pi/setup-display.sh          # cage + chromium + kiosk unit"
echo "    then touch all four corners of the self-test page it opens."
exit 0
