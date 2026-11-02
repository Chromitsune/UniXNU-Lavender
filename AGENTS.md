# UniXNU-Lavender agent notes

## Device-safety rules

- Do not flash a device unless the user explicitly authorizes it in the current task.
- Do not write to `LOGFS`: its format/ownership is not qualified. Do not guess raw MMIO addresses.
- Treat a PSCI `SYSTEM_RESET` as a software-requested system reset, **not** as a DRAM-retaining warm reset. Validate the full XNU → Secure Monitor → Xiaomi bootloader → UEFI path on hardware before claiming RAM retention.
- Before any partition write, identify the partition by GPT/BlockIO metadata and preserve unrelated fields. The panic recovery path writes only the standard bootloader message in `misc`; this runtime behavior remains unverified on this phone.

## Panic recovery state

See [`docs/panic-recovery-status.md`](docs/panic-recovery-status.md) for the implementation, reset-retention evidence, test results, current flashed payload hashes, and next hardware test. The key contract is that `ring.loc` is only a physical RAM locator; `bootlog.txt` is produced by DarwinBoot on the next boot only if it can validate and read the ring.

## Build and test

- `scripts/test-panic-recovery.py --kernel <kernel.macho>` runs host-only BCB/ring contract tests and patches only a temporary kernel copy.
- `edk2-firmware-build/build-darwinboot.sh darwinboot` builds the EFI driver; it does not flash anything.
- A full payload build requires the user's private `UNIXNU_UNIQUE_CHIP_ID`; do not invent or commit it.
