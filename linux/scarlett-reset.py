#!/usr/bin/env python3
# USB-reset all Focusrite devices (vendor 1235) — software replug for the
# Scarlett, which keeps stale firmware state across warm dual-boot reboots.
import fcntl
import glob
import sys

USBDEVFS_RESET = 0x5514
VENDOR = "1235"

reset = 0
for dev in glob.glob("/sys/bus/usb/devices/*/idVendor"):
    try:
        if open(dev).read().strip() != VENDOR:
            continue
        base = dev.rsplit("/", 2)[0] + "/" + dev.split("/")[-2]
        bus = open(base + "/busnum").read().strip().zfill(3)
        num = open(base + "/devnum").read().strip().zfill(3)
        with open(f"/dev/bus/usb/{bus}/{num}", "wb") as fh:
            fcntl.ioctl(fh, USBDEVFS_RESET, 0)
        print(f"reset {bus}/{num}")
        reset += 1
    except OSError as e:
        print(f"skip {dev}: {e}", file=sys.stderr)

sys.exit(0 if reset else 1)
