# UniXNU-lavender

Device repository for the **Xiaomi Redmi Note 7** (`lavender`, Qualcomm SDM660)
on [UniXNU](https://github.com/unixnu/UniXNU) — iPadOS/iOS on a 2019-era Android
phone, loaded through the phone's own bootloader and storage.

## Board realities (all pinned from the phone itself)

| Item | Value |
| --- | --- |
| SoC | SDM660: 4x Cortex-A73 (0x100..0x103) + 4x Cortex-A53 (0x000..0x003), PSCI smc |
| Interrupt controller | GICv3: dist 0x17a00000/0x10000, redist 0x17b00000/0x100000, stride 0x20000 |
| Arch timer | 19200000 Hz (stock FDT `clock-frequency = 0x124f800`) |
| DRAM | 4 GB: banks 0x80000000/2 GiB + 0x100000000/0x7e4c0000 -> usable 0x17e4c0000 |
| Storage | eMMC (SK Hynix 128 GB) on qcom,sdhci-msm-v5 @ 0xc0c4000, 8-bit, HS400_1p8v — **no UFS** |
| Panel | NT36672A video mode 1080x2340; splash 0x9d400000/0x2300000 |
| Touch | Novatek NVT @ 0x62 on i2c@c175000 |
| Userdata | first_lba 15466496 .. last_lba 244285406, 512-byte LBA |
| Boot loader | Xiaomi ABL (no `fastboot boot`; `flash boot` proven), boot img = AOSP v0 header |
| Firmware volume | Open EDK2 core + the phone's own stock XBL drivers (carved from `xbl.elf`) — see `edk2-firmware-build/` |

The UEFI firmware, the UniXNU boot-chain (menu, kits, driver injection, DT,
initrd, entry variables) and the fixture payload already **pass on the device**
(`UNIXNU-HELLO: RESULT PASS 5 passed 0 failed`, 2026-10-19).

## Status / roadmap

1. [x] Phase 1 — QEMU framework smoke (UniXNU base)
2. [x] Phase 2 — UEFI firmware carved from the phone's XBL, boots on device
   (Xiaomi ABL accepts our AOSP-v0 boot image)
3. [x] Phase 2.5 — UniXNU boot chain green on device (ESP on `userdata`,
   full 5-check payload pass)
4. [ ] Phase 3 — **this repository**: `board.toml` + `lavender_defconfig` +
   `scripts/make-lavender-adt.py` (scaffolded; ADT tool mirrors the sheng
   contract with SDM660 values)
5. [ ] Phase 4 — real XNU: needs full Xcode (`iphoneos` SDK) on the build Mac;
   kernel build via `./unixnu build kernel`, prelink against the unchanged
   iPad7,1 iPadOS 17.7.11 kernelcache, then DarwinBoot handover on the phone
6. [ ] Phase 5 — storage: `NativeEmmc` kernel driver (qcom,sdhci-msm-v5,
   4K pages); until then the RAM block root carries bring-up
7. [ ] Phase 6 — display/touch: NT36672A panel + Novatek touch (SEVNative ->
   NativeMobileDisplay path), then milestone-2 identity work
   (iPhone9,1 / iOS 15.8, synthetic Home = volume-down + seeded AssistiveTouch)

## Files

```
board.toml                     board + ipsw + userdata + kernel + firmware + payload + bootimg
configs/lavender_defconfig     kernel configuration (UFS -> eMMC swap noted inline)
scripts/make-lavender-adt.py   Apple-format device tree generator (SDM660-pinned values)
scripts/seed-data-skip-setup.py
package/unixnu.toml            boot menu entries for the device package (kits/lavender)
```

## Bring-up commands (device)

```sh
# firmware build (macOS shim toolchain; see ../notes/lavender-port-plan.md)
#   -> RedmiNote7Pkg -> make-bootimg.py -> fastboot flash boot uefi.img
# ESP: UniXNU stage-esp with this repo's package/, mformat/mcopy -> flash userdata
fastboot flash userdata unixnu-esp.img      # retry once — first attempt can fail transiently
fastboot flash boot uefi-boot.img
fastboot reboot                              # Vol-+Power = fastboot, Vol+Power = OrangeFox safety net
```
