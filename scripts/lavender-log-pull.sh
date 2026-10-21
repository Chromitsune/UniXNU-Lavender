#!/usr/bin/env bash
# Pull the kernel's console log off the phone.
#
# The kernel cannot draw and has no serial port, so its console output goes
# into a ring in the kernel's own memory (scripts/patch-xnu-log-ring.py).  The
# next boot reads that ring and leaves it on the ESP as
# \EFI\UniXNU\bootlog.txt, which is what this script digs out.
#
# Two steps, because the phone has to be in the recovery for the first one.
#
# 1. On the phone, in the OrangeFox (TWRP) terminal:
#
#      dd if=/dev/block/by-name/userdata of=/sdcard/esp.img bs=1M count=192
#      # (or: dd if=/dev/block/platform/soc/.../userdata ...)
#
#    That copies the first 192 MiB of the userdata partition, which is where
#    the ESP (and the log) live.  Then pull esp.img off the phone (adb pull,
#    or copy it to the PC).
#
# 2. On the Mac:
#
#      scripts/lavender-log-pull.sh esp.img
#
#    which prints the log and leaves it in bootlog.txt.
#
# The log is also on the phone's screen: the loader prints the tail of it
# after the hand-off, below the bands the kernel paints, so a photograph of
# the screen shows it too.
set -euo pipefail

IMAGE=${1:-esp.img}
OUT=${2:-bootlog.txt}

if [ ! -f "$IMAGE" ]; then
  echo "usage: $0 <esp image from the phone> [output]" >&2
  exit 1
fi

if ! command -v mcopy >/dev/null 2>&1; then
  echo "mtools (mcopy) is not installed" >&2
  exit 1
fi

# The log file, and the file that says where the ring was.
mcopy -i "$IMAGE" ::/EFI/UniXNU/bootlog.txt "$OUT" 2>/dev/null || true
if [ ! -s "$OUT" ]; then
  echo "no bootlog.txt in the image"
  exit 1
fi

echo "--- $OUT ($(wc -c < "$OUT") bytes) ---"
# The log is a byte ring: the newest text is at the end, and the ring can hold
# bytes that are not text at all.
LC_ALL=C tr -c '[:print:]\n' '.' < "$OUT" | LC_ALL=C tail -c 4000
echo
