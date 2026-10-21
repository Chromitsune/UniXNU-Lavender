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
KERNEL=${UNIXNU_KERNEL:-/Users/rebuke/aOS/UniXNU/out/lavender/kernel/kernel.macho}

if [ ! -f "$KERNEL" ]; then
  echo "no built kernel at $KERNEL" >&2
  exit 1
fi

cp "$KERNEL" "$OUT"
# The panic screen is deliberately left out: it runs with the MMU on, where the
# panel is not mapped by the kernel's own tables yet (the early frame buffer
# mapping patch is disabled, it used to destroy the bootstrap page tables).
python3 "$ROOT/scripts/patch-xnu-entry-probe.py" "$OUT" --no-reset \
  --boot-args 0xfffffff007167d18 --panic-cave 0xfffffff007a1e610 \
  --post-mmu-band

# The early boot: the MMU enable site belongs to the panic screen probe, so
# the ladder starts after it.
python3 "$ROOT/scripts/patch-xnu-boot-ladder.py" "$OUT" --function _start_first_cpu --band 2 \
  --site 0xD518C000:msr_VBAR \
  --site 0xF9400696:ldr_boot_args \
  --site 0xB9401801:mach_header_flags \
  --site 0xD5033B9F:tables_ready \
  --site 0xD5182041:msr_TCR \
  --site 0xD5182020:msr_TTBR1

# The C init ladder is optional: the call sites move with every kernel build.
if [ "${UNIXNU_C_LADDER:-0}" = 1 ]; then
  python3 "$ROOT/scripts/patch-xnu-boot-ladder.py" "$OUT" --function _arm_init --band 8 \
    --args-address 0xfffffff007167d18 \
    --site-at 0xfffffff0079afafc:PE_parse_boot_argn \
    --site-at 0xfffffff0079afb20:PE_get_default \
    --site-at 0xfffffff0079afb74:arm_vm_init || echo "C ladder sites moved with the kernel build; skipped"
fi

# The console ring: the kernel appends every console byte to a ring in the
# tail of its own window, and the next boot reads it back and prints it.
# Applied after the ladders, because it checks that its caves are still in
# zero padding.
python3 "$ROOT/scripts/patch-xnu-log-ring.py" "$OUT"

echo "instrumented kernel: $OUT"
