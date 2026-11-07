#!/usr/bin/env bash
# Replace only the DarwinBoot EFI driver in a captured 192 MiB Lavender ESP image.
# This is an offline image operation; it never flashes or opens a device.
set -euo pipefail
umask 077
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
INPUT=${1:-}
OUTPUT=${2:-$ROOT/out/lavender/payload-status.img}
DARWINBOOT=${3:-$ROOT/../edk2-firmware-build/workspace/Build/DarwinBoot/RELEASE_GCC5/AARCH64/DarwinBoot.efi}
EXPECTED_BYTES=201326592
ESP_DRIVER='::/EFI/UniXNU/Payloads/DarwinBoot.efi'
ESP_LOC='::/EFI/UniXNU/ring.loc'

if [[ -z "$INPUT" || ! -f "$INPUT" ]]; then
  echo "usage: $0 <captured-192MiB-ESP.img> [output.img] [DarwinBoot.efi]" >&2
  exit 2
fi
for tool in mcopy; do
  command -v "$tool" >/dev/null 2>&1 || { echo "$tool (mtools) is required" >&2; exit 1; }
done
[[ -f "$DARWINBOOT" ]] || { echo "missing DarwinBoot EFI: $DARWINBOOT" >&2; exit 1; }
[[ ! -e "$OUTPUT" ]] || { echo "refusing to overwrite existing image: $OUTPUT" >&2; exit 1; }
BYTES=$(wc -c < "$INPUT" | tr -d ' ')
[[ "$BYTES" == "$EXPECTED_BYTES" ]] || { echo "expected $EXPECTED_BYTES-byte ESP prefix, got $BYTES" >&2; exit 1; }
mkdir -p "$(dirname "$OUTPUT")"
TMP="$OUTPUT.tmp.$$"
TMP_LOC=$(mktemp "${TMP}.loc.XXXXXX")
TMP_NEWLOC=$(mktemp "${TMP}.newloc.XXXXXX")
TMP_DRIVER=$(mktemp "${TMP}.driver.XXXXXX")
cleanup() { rm -f "$TMP" "$TMP_LOC" "$TMP_NEWLOC" "$TMP_DRIVER"; }
trap cleanup EXIT

mcopy -o -i "$INPUT" "$ESP_LOC" "$TMP_LOC" 2>/dev/null || { echo "input ESP has no ring.loc; refusing to discard prior-boot locator" >&2; exit 1; }
python3 - "$TMP_LOC" <<'PYLOC'
import struct, sys
b=open(sys.argv[1],'rb').read()
if len(b)!=40: raise SystemExit(f"ring.loc must be 40 bytes, got {len(b)}")
magic,version,reserved,ring,total,data=struct.unpack('<QIIQQQ',b)
if magic!=0x314c4f56414c414c or version!=1 or reserved!=0 or not ring or total!=0x40+data or data<0x1000 or data&(data-1):
    raise SystemExit("ring.loc header is invalid; refusing ESP update")
print(f"preserving valid ring.loc: ring={ring:#x}, total={total:#x}, data={data:#x}")
PYLOC

cp "$INPUT" "$TMP"
mcopy -o -i "$TMP" "$DARWINBOOT" "$ESP_DRIVER"
mcopy -o -i "$TMP" "$ESP_LOC" "$TMP_NEWLOC" 2>/dev/null || { echo "patched ESP lost ring.loc" >&2; exit 1; }
cmp -s "$TMP_LOC" "$TMP_NEWLOC" || { echo "patched ESP changed ring.loc" >&2; exit 1; }
mcopy -o -i "$TMP" "$ESP_DRIVER" "$TMP_DRIVER" 2>/dev/null || { echo "patched ESP has no DarwinBoot driver at expected path" >&2; exit 1; }
cmp -s "$DARWINBOOT" "$TMP_DRIVER" || { echo "patched driver bytes do not match build output" >&2; exit 1; }
OUT_BYTES=$(wc -c < "$TMP" | tr -d ' ')
[[ "$OUT_BYTES" == "$EXPECTED_BYTES" ]] || { echo "patched image changed size: $OUT_BYTES" >&2; exit 1; }
mv "$TMP" "$OUTPUT"
trap - EXIT
rm -f "$TMP_LOC" "$TMP_NEWLOC" "$TMP_DRIVER"
sha256=$(python3 - "$OUTPUT" <<'PYHASH'
import hashlib,sys
h=hashlib.sha256()
with open(sys.argv[1],'rb') as f:
    for b in iter(lambda:f.read(4<<20),b''): h.update(b)
print(h.hexdigest())
PYHASH
)
echo "patched offline ESP: $OUTPUT ($OUT_BYTES bytes)"
echo "DarwinBoot SHA-256: $(shasum -a 256 "$DARWINBOOT" | awk '{print $1}')"
echo "ESP SHA-256: $sha256"
echo "No device was accessed or flashed."
