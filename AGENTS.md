# UniXNU-Lavender agent notes

## Device-safety rules

- Do not flash a device unless the user explicitly authorizes it in the current task.
- Do not write to `LOGFS`: its format/ownership is not qualified. Do not guess raw MMIO addresses.
- Treat a PSCI `SYSTEM_RESET` as a software-requested system reset, **not** as a DRAM-retaining warm reset. Validate the full XNU → Secure Monitor → Xiaomi bootloader → UEFI path on hardware before claiming RAM retention.
- Before any partition write, identify the partition by GPT/BlockIO metadata and preserve unrelated fields. The panic recovery path writes only the standard bootloader message in `misc`; this runtime behavior remains unverified on this phone.

## Panic recovery state

See [`docs/panic-recovery-status.md`](docs/panic-recovery-status.md) for the implementation, reset-retention evidence, test results, current flashed payload hashes, and next hardware test. The key contract is that `ring.loc` is only a physical RAM locator; `bootlog.txt` is produced by DarwinBoot on the next boot only if it can validate and read the ring.

## Build and test

- `scripts/make-lavender-esp.sh [kernel.macho]` makes a temporary kernel copy, instruments the RAM ring/panic hook when `panic_recovery = true`, then packages the EFI binaries. It requires the user's private `UNIXNU_UNIQUE_CHIP_ID`; never substitute a guessed ID.
- `scripts/test-panic-recovery.py --kernel <kernel.macho>` runs host-only BCB/ring contract tests and patches only a temporary kernel copy.
- `edk2-firmware-build/build-darwinboot.sh darwinboot` builds the EFI driver; it does not flash anything.
- A full payload build requires the user's private `UNIXNU_UNIQUE_CHIP_ID`; do not invent or commit it.

## Host logging and image utilities

- After recovery ADB is available, `scripts/lavender-log-pull-usb.sh out/lavender/bootlog.txt` reads the ESP prefix only; it never writes to the phone. Symbolize a retrieved log with `scripts/lavender-panic-symbolize.py` and the exact matching kernel (`--kernel` or `UNIXNU_KERNEL`).
- Lavender does not implement the Negroni CDC-ECM log/UDP command service or its persistent UFS bootlog. Do not run iOSPlus10 `ctl`, `bootlog`, or `flash` commands on this device.
- `tools/lavender-image-tools/` contains only host-side IPSW/IM4P helpers. It does not flash or access devices; run `scripts/test-lavender-image-tools.sh` for its local tests.
- On a previous `ring.loc`, DarwinBoot now writes `ring-status.txt` before the recovery BCB and updates it with the BCB result. In this bring-up profile, any previous locator routes to recovery, including intentional XNU reboots; do not assume normal reboot semantics.
- To preserve the current ESP (including `ring.loc`) while replacing only DarwinBoot, pull with `LAVENDER_KEEP_ESP=1` and use `scripts/patch-lavender-esp-darwinboot.sh`. This creates a local image only; it does not flash.
