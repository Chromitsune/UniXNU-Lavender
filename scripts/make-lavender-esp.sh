#!/usr/bin/env bash
# Build the complete ESP image the phone boots from.
#
# `unixnu build payload` stages the kernel, its device tree and unixnu.toml,
# but the phone also needs the UniXNU EFI application (the UEFI runs it from
# startup.nsh) and the DarwinBoot loader (the payload's unixnu.toml declares
# it as a driver, and it installs the loader protocol the kernel is handed
# over with).  Flashing an ESP without those two leaves the phone with no
# UniXNU menu at all.
#
# usage: make-lavender-esp.sh [kernel.macho]
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
UNIXNU=${UNIXNU_ROOT:-$ROOT/../UniXNU}
KERNEL=${1:-$UNIXNU/out/lavender/kernel/kernel.macho}
PAYLOAD=$UNIXNU/out/lavender/payload/payload.img
BOOTAA64=${UNIXNU_BOOTAA64:-$UNIXNU/package/Build/Out/AARCH64-DEBUG/UniXNU.efi}
DARWINBOOT=${UNIXNU_DARWINBOOT:-$ROOT/../edk2-firmware-build/workspace/Build/DarwinBoot/RELEASE_GCC5/AARCH64/DarwinBoot.efi}
UNIQUE_CHIP_ID=${UNIXNU_UNIQUE_CHIP_ID:-}
if [ -z "$UNIQUE_CHIP_ID" ]; then
  echo "set UNIXNU_UNIQUE_CHIP_ID to a stable nonzero 64-bit hex platform ID before building" >&2
  exit 1
fi
if ! grep -aFq 'LAVLOCRINGDESC1' "$KERNEL"; then
  echo "warning: kernel has no console-ring descriptor; no XNU bootlog will be recovered" >&2
  echo "         build one with scripts/make-instrumented-kernel.sh and pass that output here" >&2
fi

for file in "$KERNEL" "$BOOTAA64" "$DARWINBOOT"; do
  if [ ! -f "$file" ]; then
    echo "missing $file" >&2
    exit 1
  fi
done
python3 "$ROOT/scripts/check-kernel-darwin-version.py" "$KERNEL" --board "$ROOT/board.toml"

cd "$UNIXNU"
rm -f out/lavender/payload/payload.img out/lavender/payload/payload.json
./unixnu build -D "$ROOT" payload --kernel "$KERNEL" --unique-chip-id "$UNIQUE_CHIP_ID"

# The EFI application the UEFI starts, and the loader the payload declares.
mmd -i "$PAYLOAD" ::/EFI/BOOT
mcopy -i "$PAYLOAD" "$BOOTAA64" ::/EFI/BOOT/BOOTAA64.EFI
mcopy -i "$PAYLOAD" "$DARWINBOOT" ::/EFI/UniXNU/Payloads/DarwinBoot.efi

echo "--- ESP contents ---"
mdir -i "$PAYLOAD" ::/EFI/BOOT
mdir -i "$PAYLOAD" ::/EFI/UniXNU/Payloads
echo "flash with: fastboot flash userdata $PAYLOAD"
