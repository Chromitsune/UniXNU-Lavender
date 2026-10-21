#!/usr/bin/env bash
# QEMU rehearsal for the phone ESP: boots the exact payload the phone boots
# (own UEFI = UniXNU.efi as BOOTAA64.EFI + DarwinBoot + kernel + ADT) under
# AAVMF and lets the framebuffer be screenshotted over QMP.
#
# usage: scripts/rehearsal-qemu.sh [payload.img] [vars.fd copy dir]
#
# The framebuffer dump then shows the checkpoint ladder as the phone's panel
# would. The phone ADT is accepted for the pre-MMU ladder questions: everything
# past the MMU enable needs the QEMU-shaped ADT (make-dt.py) instead.
set -euo pipefail

PAYLOAD=${1:-/Users/rebuke/aOS/UniXNU/out/lavender/payload/payload.img}
DIR=${2:-/tmp/esp-lavender}
CODE=/opt/homebrew/share/qemu/edk2-aarch64-code.fd

mkdir -p "$DIR"
cp "$PAYLOAD" "$DIR/payload.img"
[[ -e "$DIR/code.fd" ]] || cp "$CODE" "$DIR/code.fd"
truncate -s 64M "$DIR/vars.fd"   # AAVMF vars: empty is fine for a first boot

pkill -x qemu-system-aarch64 2>/dev/null || true
sleep 1
qemu-system-aarch64 -M virt -cpu cortex-a76 -m 1024 -smp 2 \
  -drive if=pflash,unit=0,file="$DIR/code.fd",readonly=on,format=raw \
  -drive if=pflash,unit=1,file="$DIR/vars.fd",format=raw \
  -drive if=none,id=esp,file="$DIR/payload.img",format=raw \
  -device virtio-blk-device,drive=esp \
  -device ramfb -serial null -no-reboot -daemonize -display none \
  -qmp unix:"$DIR/qmp.sock",server,nowait

echo "QEMU running; QMP at $DIR/qmp.sock; use:"
echo "  python3 $DIR/sdump.py $DIR/qmp.sock   # framebuffer -> $DIR/fb.ppm"
