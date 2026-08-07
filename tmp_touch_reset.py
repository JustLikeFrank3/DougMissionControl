#!/usr/bin/env python3
"""USBDEVFS_RESET the wch.cn touchscreen, then report whether it re-enumerates."""
import fcntl
import subprocess
import sys
import time

USBDEVFS_RESET = 21780  # _IO('U', 20)

out = subprocess.run(["lsusb", "-d", "27c0:0859"], capture_output=True, text=True).stdout.strip()
if not out:
    sys.exit("touchscreen not on the bus")
parts = out.split()
bus, dev = parts[1], parts[3].rstrip(":")
path = f"/dev/bus/usb/{bus}/{dev}"
print(f"resetting {path} ({out})")
with open(path, "wb") as f:
    try:
        fcntl.ioctl(f, USBDEVFS_RESET, 0)
        print("ioctl reset issued")
    except OSError as e:
        # ENODEV after reset means the device re-enumerated: success.
        print(f"ioctl returned {e} (re-enumeration is expected)")

time.sleep(3)
out2 = subprocess.run(["lsusb", "-d", "27c0:0859"], capture_output=True, text=True).stdout.strip()
print(f"after: {out2 or 'GONE'}")
