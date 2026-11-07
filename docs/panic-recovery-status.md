# Lavender panic-recovery status

Updated 2026-11-01. This is a project memory/operations record, not a hardware qualification report.

## Reset and DRAM-retention finding

- Lavender's generated ADT declares `/psci` method `smc` (`board.toml`, `psci_method`). `ArmCpu` uses this conduit for PSCI `CPU_ON`; the existing reset probe and the panic renderer use PSCI `SYSTEM_RESET`, function ID `0x84000009`, via `smc #0`.
- EDK2's `ArmSmcPsciResetSystemLib` sends `SYSTEM_RESET` for its cold-reset path; its `ResetWarm()` maps to `ResetCold()` (`ArmSmcPsciResetSystemLib.c`, `ResetCold`/`ResetWarm`). Thus the word “warm” is not a retention guarantee here.
- No evidence establishes that the SDM660 Secure Monitor, Xiaomi ABL, and UEFI preserve DRAM across the complete reset path. Earlier forced Power-button resets lost the ring, but those were not the new PSCI path. **Retention remains unverified.**

## Implemented flow

1. The panic renderer appends its formatted panic message and register/page diagnostics to the existing 64 KiB ring, cleans the ring to the point of coherency, displays for approximately 20 seconds using the architectural counter, then invokes PSCI `SYSTEM_RESET` over the board's SMC conduit.
2. On the next UEFI entry, DarwinBoot reads `ring.loc`, validates geometry/header/memory mapping, reserves pages classified as conventional memory before archive writes where possible, and attempts to archive a valid nonempty ring to `\\EFI\\UniXNU\\bootlog.txt`.
3. If a prior locator existed (or the locator could not be opened for an error other than not-found), DarwinBoot displays the recovery/archive status, writes and verifies a standard `boot-recovery` BCB in the GPT `misc` partition using UEFI BlockIO, then requests a reset so ABL can boot OF. If BCB access/write fails, it stops on the diagnostic screen instead of launching XNU again.

The flow deliberately does **not** write to `LOGFS`. The only runtime partition write is the BCB in `misc`; it is guarded by GPT name lookup, read/modify/write of the standard 2048-byte message, preservation of reserved bytes, BlockIO flush, and readback. ABL/OrangeFox behavior is not yet hardware-verified.

In this bring-up configuration, any prior ring locator is treated as a failed prior XNU run and routes to OF; intentional XNU reboots will also route to OF until a reliable panic-vs-normal-reboot marker exists. A hard hang that never reaches `panic()` will not invoke this handler.

## Validation performed

- `build-darwinboot.sh darwinboot` completed successfully; the local RELEASE `DarwinBoot.efi` is 53,248 bytes.
- `scripts/test-panic-recovery.py --kernel /Users/rebuke/hack-ios/UniXNU/out/lavender/kernel/kernel.macho` passed: BCB layout/reserved-byte preservation, locator geometry, ring wrap calculation, and temporary-copy ring/panic code patching. The generated test ring has 64 KiB data plus a 64-byte header; the panic renderer fits in an unreferenced executable padding run.
- `git diff --check`, Python compilation, and shell syntax checks passed.
- No `LOGFS` writes were performed. No device partition was written before the explicitly authorized flash below.

## Device payload flash (user-authorized)

The user explicitly authorized flashing on 2026-11-01. To preserve the existing device ADT/private platform seed without reconstructing it, the new kernel and DarwinBoot were inserted into a copy of the prior 192 MiB payload image. The existing ADT/kernel args were retained. The new image was verified as 201,326,592 bytes; the patched kernel passed the Darwin 22.6.0 board check; it contains one `LAVLOCRINGDESC1` descriptor and `LAVPANICRECOVERY1`; the fresh test image has no stale `ring.loc` or `bootlog.txt`.

- Test payload SHA-256: `6d64f3332d9b1b90e9baa1997a7e19041f637c6b543695560ed8a767a3d09a41`
- Patched kernel SHA-256: `2b2791c1e4cc07565be3999811e6872d528bb2a57d00d22a9b438cf755d25331`
- DarwinBoot SHA-256: `94d7c05af17aba144e1901546c9fa4e4a81a3defbc4bb10bcad76f26c648d2ab`
- Fastboot reported `userdata` partition size `0x1B46FF7E00`; flashing the 192 MiB image completed successfully and the device was rebooted.
- **Do not call this recovery verified.** The pending evidence is the next screen status and, if OF starts, a pulled `bootlog.txt` containing the panic marker.

## Hardware test plan

1. Record whether the panic screen displays and remains for about 20 seconds before the SMC reset.
2. On the following UEFI pass, record the exact `LOG SAVED`, `NO PRIOR LOG`, or `LOG ARCHIVE FAILED` reason, plus whether the BCB reset reaches OF.
3. In OF, pull the ESP log and verify the `[PANIC]` record plus panic text/register diagnostics. A valid `bootlog.txt` proves RAM survived long enough through the complete reset/bootloader path; a missing magic/unmapped-range result means it did not (or UEFI reused the range before reading).
4. If SMC reset fails or XNU hard-hangs without `panic()`, do not infer anything about DRAM retention; retain the phone state and investigate with a separately authorized test.

### 2026-11-01 first on-device run after authorized flash

- `fastboot flash userdata payload-panic-recovery.img` completed successfully (192 MiB sent/written), then `fastboot reboot` returned OKAY.
- After a 60-second delay, neither ADB nor fastboot enumerated the phone. No screen observation, UEFI status, OrangeFox session, or `bootlog.txt` was available to us. This is **inconclusive**: it does not prove the reset failed, the ring was lost, or that OF was reached. Do not force another reset until the user can inspect the screen/state.


### 2026-11-06 read-only log retrieval attempt

- Host enumeration found the device in Xiaomi ABL fastboot (`product: lavender`); ADB listed no devices. Read-only `fastboot getvar` showed the existing `userdata` partition size and unlocked state.
- `scripts/lavender-log-pull-usb.sh out/lavender/bootlog.txt` stopped before reading because no usable ADB recovery session was present. No reboot, reset, flash, or partition write was issued.
- No local `bootlog.txt` was found. The safe next step is for the owner to inspect the screen and put the phone into OrangeFox/recovery with ADB enabled; then the pull script reads only the first 192 MiB of `userdata` and extracts the ESP log.
- The iOSPlus10 live UDP logger, `ctl`, and persistent UFS bootlog are Negroni-specific and were not transplanted. A host-only panic symbolizer and generic IPSW/IM4P helpers were added instead.


### 2026-11-07 OrangeFox log pull

- After the owner placed the phone in recovery, ADB identified one Lavender device. `scripts/lavender-log-pull-usb.sh out/lavender/bootlog.txt` read the first 192 MiB of `userdata` (read-only). No `bootlog.txt` was present; `ring.loc` was preserved as `out/lavender/bootlog.ring.loc`.
- The 40-byte locator validates: magic/version/reserved fields are correct, ring address `0xEBEADF00`, total `0x10040`, data capacity `0x10000` (64 KiB). This confirms the locator persisted on the ESP. It does **not** prove that RAM survived the reset or that DarwinBoot read the ring; the ring bytes are not in `ring.loc`.
- No device reset, flash, partition write, or raw-memory read was performed. The missing `bootlog.txt` leaves the archive failure cause unresolved; the exact UEFI recovery-screen status was not captured.
