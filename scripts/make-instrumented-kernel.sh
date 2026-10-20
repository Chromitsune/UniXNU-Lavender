#!/usr/bin/env bash
# Build an instrumented copy of the built kernel.
#
# The instrumented kernel paints a coloured band per early-boot milestone, so
# the panel shows exactly how far the kernel got.  It also carries the panic
# screen, so a panic prints its message instead of leaving a black panel.
#
# usage: make-instrumented-kernel.sh [output.macho]
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TREE=${UNIXNU_TREE:-/Users/rebuke/aOS/UniXNU/out/lavender/tree}
OUT=${1:-/tmp/kernel-instrumented.macho}
KERNEL="$TREE/build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/mach.development.bcm2837.unstripped.noctf"

if [ ! -f "$KERNEL" ]; then
  echo "no built kernel at $KERNEL" >&2
  exit 1
fi

cp "$KERNEL" "$OUT"
python3 "$ROOT/scripts/patch-xnu-entry-probe.py" "$OUT" --panic-screen --no-reset

# The early boot: the MMU enable site belongs to the panic screen probe, so
# the ladder starts after it.
python3 "$ROOT/scripts/patch-xnu-boot-ladder.py" "$OUT" --function _start_first_cpu --band 2 \
  --site 0xD518C000:msr_VBAR \
  --site 0xF9400696:ldr_boot_args \
  --site 0xB9401801:mach_header_flags \
  --site 0xD5033B9F:tables_ready \
  --site 0xD5182041:msr_TCR \
  --site 0xD5182020:msr_TTBR1

# The C init: the boot arguments are read from the kernel's own copy.
python3 "$ROOT/scripts/patch-xnu-boot-ladder.py" "$OUT" --function _arm_init --band 8 \
  --args-address 0xfffffff007167d18 \
  --site 0x97fe50ee:PE_get_default \
  --site 0x97fe4de1:PE_parse_boot_argn \
  --site 0xD500419F:msr_PAN \
  --site 0x97e8ecc7:arm_vm_init \
  --site 0x97e9024c:patch_low_glo \
  --site 0x97ff3cbc:kernel_startup

echo "instrumented kernel: $OUT"
