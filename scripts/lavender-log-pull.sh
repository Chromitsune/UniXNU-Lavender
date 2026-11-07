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
case "$OUT" in
  *.txt) LOC_OUT="${OUT%.txt}.ring.loc" ;;
  *) LOC_OUT="${OUT}.ring.loc" ;;
esac

if [ ! -f "$IMAGE" ]; then
  echo "usage: $0 <esp image from the phone> [output]" >&2
  exit 1
fi

if ! command -v mcopy >/dev/null 2>&1; then
  echo "mtools (mcopy) is not installed" >&2
  exit 1
fi

# Extract to a same-directory temporary file: a failed extraction must not
# leave a stale log from an earlier run looking like a successful recovery.
TMP_OUT=$(mktemp "${OUT}.tmp.XXXXXX")
rm -f "$LOC_OUT"
trap 'rm -f "$TMP_OUT"' EXIT
if ! mcopy -o -i "$IMAGE" ::/EFI/UniXNU/bootlog.txt "$TMP_OUT" 2>/dev/null || [ ! -s "$TMP_OUT" ]; then
  TMP_LOC=$(mktemp "${OUT}.loc.tmp.XXXXXX")
  if mcopy -o -i "$IMAGE" ::/EFI/UniXNU/ring.loc "$TMP_LOC" 2>/dev/null && [ -s "$TMP_LOC" ]; then
    mv -f "$TMP_LOC" "$LOC_OUT"
    echo "no bootlog.txt; preserved ring locator at $LOC_OUT" >&2
    python3 - "$LOC_OUT" <<'PYLOC'
import struct, sys
b = open(sys.argv[1], 'rb').read()
if len(b) < 40:
    print(f"ring.loc: truncated ({len(b)} bytes; expected 40)", file=sys.stderr)
    raise SystemExit(1)
magic, version, reserved, ring, total, data = struct.unpack_from('<QIIQQQ', b)
valid = (magic == 0x314C4F56414C414C and version == 1 and reserved == 0 and ring != 0
         and data >= 0x1000 and data & (data - 1) == 0 and total == 0x40 + data)
print(f"ring.loc: bytes={len(b)} version={version} valid={valid} ring={ring:#x} total={total:#x} data={data:#x}", file=sys.stderr)
PYLOC
    echo "DarwinBoot did not publish bootlog.txt; locator alone cannot recover the RAM ring after reset." >&2
  else
    rm -f "$TMP_LOC"
    echo "no bootlog.txt or ring.loc; this ESP has no published XNU ring location" >&2
  fi
  exit 1
fi
mv -f "$TMP_OUT" "$OUT"
trap - EXIT

echo "--- $OUT ($(wc -c < "$OUT") bytes) ---"
# The log is a byte ring: the newest text is at the end, and the ring can hold
# bytes that are not text at all.
LC_ALL=C tr -c '[:print:]\n' '.' < "$OUT" | LC_ALL=C tail -c 4000
echo
