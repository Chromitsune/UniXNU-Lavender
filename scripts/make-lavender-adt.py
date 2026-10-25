#!/usr/bin/env python3
"""Generate the minimal lavender hardware Apple-format DT for the first boot.

The tree mirrors the proven QEMU entry fixture structure (same required nodes
and properties the source XNU boot converged on) with real pinned Redmi Note 7
(lavender, SDM660) values taken from the phone's own stock compiled FDT:

  - GICv3 distributor 0x17a00000/0x10000, redistributor 0x17b00000/0x100000
    (arm-io child offsets under an APSS range at 0x17000000), stride 0x20000
    (8 cores in the 1 MiB redistributor region)
  - CPU timebase 19200000 Hz (stock FDT timer clock-frequency = 0x124f800)
  - DRAM base 0x80000000; the size comes from the stock memory node
    (0x80000000 + 0x7e4c0000 = 0x17e4c0000) and is cross-checked at build time.
  - Storage: qcom,sdhci-msm-v5 host controller 0xc0c4000/0x1000 (eMMC; the
    sheng board's UFS controller does not exist on this SoC).

This is first-boot bring-up data: the platform has no matching platform expert
yet, so the kernel is expected to reach the video console and report the
genuine missing-driver panic on screen. No Apple device identity is claimed
unless --iphone-identity is passed, and no property is invented beyond these
pinned sources.
"""
from pathlib import Path
import argparse
import os
import struct
import zlib

ROOT = Path(__file__).resolve().parents[1]

DRAM_BASE = 0x80000000
DRAM_SIZE_EXPECTED = 0x17E4C0000   # stock FDT memory node sum (banks 0x80000000+0x80000000, 0x100000000+0x7e4c0000)
ARMIO_BASE = 0x17000000            # APSS peripheral block containing the GIC
ARMIO_SIZE = 0x1C00000            # covers the distributor and the 1 MiB redistributor region
GIC_DIST = 0x17A00000
GIC_DIST_SIZE = 0x10000
GIC_REDIST = 0x17B00000
GIC_REDIST_SIZE = 0x100000
GIC_REDIST_STRIDE = 0x20000        # 1 MiB / 8 cores
TIMEBASE = 19200000
EMMC_HC = 0xC0C4000                # qcom,sdhci-msm-v5, sdhc1, 8-bit, HS400_1p8v
EMMC_HC_SIZE = 0x1000


def node(name, properties=(), children=()):
    properties = [('name', name.encode() + b'\0'), *properties]
    result = struct.pack('<II', len(properties), len(children))
    for key, data in properties:
        result += struct.pack('<32sI', key.encode(), len(data))
        result += data + bytes(-len(data) % 4)
    return result + b''.join(children)


def empty_nvram_proxy():
    """A fresh volatile CHRP bank for upstream IODTNVRAM, with no controller."""
    def header(name, size):
        raw = bytearray(struct.pack('<BBH12s', 0x70, 0, size // 16, name))
        checksum = sum(raw)
        while checksum > 255:
            checksum = (checksum & 255) + (checksum >> 8)
        raw[1] = checksum
        return raw

    image = bytearray(8192)
    image[:16] = header(b'nvram', 32)
    image[32:48] = header(b'common', 16 + 2048)
    free_at = 32 + 16 + 2048
    image[free_at:free_at + 16] = header(b'free', len(image) - free_at)
    struct.pack_into('<I', image, 16, zlib.adler32(image[20:]))
    return [('nvram-bank-size', struct.pack('<I', len(image))),
            ('nvram-bank-count', struct.pack('<I', 1)),
            ('nvram-current-bank', struct.pack('<I', 0)),
            ('nvram-proxy-data', bytes(image))]


NVRAM_PROXY_LEN = 8192

# IODTNVRAM (the original security extension that owns the SEP NVRAM) reads
# /options/p_len while it starts.  A missing property comes back as ENODATA,
# which the kext treats as fatal ("panic: optional not set"), so the tree has
# to carry the length of the volatile NVRAM area.  Without it the kernel dies
# in the security-extension load, long before any of our own code runs.
NVRAM_OPTIONS = [('p_len', struct.pack('<Q', NVRAM_PROXY_LEN))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'build/lavender/boot/kernel.macho.adt')
    parser.add_argument('--dram-size', type=lambda value: int(value, 0), required=True,
                        help='Actual observed DRAM size, e.g. 0x17e4c0000')
    parser.add_argument('--owned-memory-size', type=lambda value: int(value, 0), default=None,
                        help='Explicit DarwinBoot contiguous window request, 256 MiB..8 GiB in 2 MiB steps')
    parser.add_argument('--root-match-volume-uuid', default=None,
                        help='/chosen/root-matching: select the root by the UUID of an IOMedia (the standard source mechanism)')
    parser.add_argument('--unique-chip-id', type=lambda value: int(value, 16), default=None,
                        help='/chosen/unique-chip-id (8 bytes) and --chip-id (4 bytes) seed XNU IOPlatformUUID; software identity seeds, not Apple silicon IDs')
    parser.add_argument('--chip-id', type=lambda value: int(value, 16), default=None)
    parser.add_argument('--iphone-identity', action='store_true',
                        help='/product and /buttons describe an iPhone X-class device without a Home button (MobileGestalt reads these from the tree); '
                             'values follow the iPhone10,3 (D22AP) tree, the panel geometry is lavender\'s')
    parser.add_argument('--cpu-regs', default='0',
                        help='Comma-separated physical MPIDR affinity values (the raw value each core reads from MPIDR_EL1, '
                             'verified against it at startup by the ArmCpu driver and passed verbatim to PSCI CPU_ON). '
                             'SDM660: 4x Cortex-A53 (Aff1=0) 0x0..0x3 and 4x Cortex-A73 (Aff1=1) 0x100..0x103 (stock FDT /cpus). '
                             'The running boot CPU comes first. More than one adds /psci')
    parser.add_argument('--psci-method', choices=['smc', 'hvc'], default='smc',
                        help='/psci method used by the secondary-CPU start (the stock FDT declares smc)')
    parser.add_argument('--packed-data-ramdisk', action='store_true', help='Hand off validated DBRDISK1 bytes to the native sparse block driver, without a dense memdev alias')
    parser.add_argument('--data-ramdisk', action='store_true',
                        help='/chosen/memory-map RAMDisk placeholder; the loader fills in the real range for a scratch Data seed')
    parser.add_argument('--trust-cache', action='store_true', help='Zeroed TrustCache sidecar handoff placeholder')
    args = parser.parse_args()
    if (args.unique_chip_id is None) != (args.chip_id is None):
        parser.error('--unique-chip-id and --chip-id must be supplied together')
    if args.unique_chip_id is not None and not (0 < args.unique_chip_id < 1 << 64 and 0 < args.chip_id < 1 << 32):
        parser.error('--unique-chip-id must fit 8 bytes and --chip-id 4 bytes, both nonzero')
    if args.owned_memory_size is not None and (
            not 0x10000000 <= args.owned_memory_size <= 0x200000000
            or args.owned_memory_size % 0x200000):
        parser.error('Owned memory must be 256 MiB to 8 GiB, aligned to 2 MiB')
    if args.dram_size > DRAM_SIZE_EXPECTED:
        parser.error(f'--dram-size exceeds the stock FDT memory node sum ({DRAM_SIZE_EXPECTED:#x}); the 4 GiB device cannot hold more')

    try:
        cpu_regs = [int(value, 0) for value in args.cpu_regs.split(',')]
    except ValueError:
        parser.error('--cpu-regs must be integers')
    if (not cpu_regs or cpu_regs[0] != 0 or len(set(cpu_regs)) != len(cpu_regs) or len(cpu_regs) > 8
            or any(reg < 0 or reg > 0xffff for reg in cpu_regs)):
        parser.error('--cpu-regs: up to eight distinct physical MPIDR affinity values (mask 0xffff: Aff0|Aff1<<8), boot CPU first '
                     'and equal to 0 (the core the firmware starts)')

    # Stock FDT: cpu@0..3 = one cluster (Cortex-A53, "silver"), cpu@100..103 = the other
    # (Cortex-A73, "gold"). The running boot CPU is cpu@0.
    core_compatible = [
        b'arm,cortex-a53\0' if reg < 0x100 else b'arm,cortex-a73\0'
        for reg in cpu_regs
    ]
    cpus = [node('cpu%d' % index, [('state', b'running\0' if index == 0 else b'waiting\0'),
                                   ('reg', struct.pack('<I', reg)),
                                   ('device_type', b'cpu\0'), ('compatible', compat),
                                   # The original AppleARMCPU kext validates the CPU
                                   # configuration against Apple's own device tree
                                   # layout: /cpus/max_cpus, a per-core cpu-id and an
                                   # interrupts list whose first entry is the AIC IPI
                                   # PPI.  Without them it panics with "CPU
                                   # configuration in device tree is invalid".
                                   ('cpu-id', struct.pack('<I', index)),
                                   ('interrupts', struct.pack('<3I', 0, 0, 4)),
                                   ('timebase-frequency', struct.pack('<I', TIMEBASE))])
            for index, (reg, compat) in enumerate(zip(cpu_regs, core_compatible))]
    tree = node('device-tree', [
        # hw.product/hw.machine/hw.target come from the root 'model'/'target-type' (IODTPlatformExpert);
        # libMobileGestalt prefix-matches hw.product against iPhone/iPod/iPad, so the iPhone X identity needs both here.
        ('model', b'iPhone10,3\0' if args.iphone_identity else b'lavender-first-boot\0'),
        *([('target-type', b'D22\0'), ('target-sub-type', b'D22AP\0')] if args.iphone_identity else []),
        # Root compatible strings of the stock board DT (xiaomi,lavender / qcom,sdm660).
        ('compatible', b'xiaomi,lavender\0qcom,sdm660\0'),
        ('#address-cells', struct.pack('<I', 2)),
        ('#size-cells', struct.pack('<I', 2)),
    ], [
        node('chosen', [('random-seed', os.urandom(64)),
                        # Without these XNU never publishes IOPlatformUUID and gethostuuid() blocks forever
                        # for callers (configd) that pass a zero timeout.
                        *([('unique-chip-id', struct.pack('<Q', args.unique_chip_id)), ('chip-id', struct.pack('<I', args.chip_id))]
                          if args.unique_chip_id is not None else []),
                        # This boots the normal target OS. Volatile Data is
                        # described separately by ephemeral-storage below.
                        ('osenvironment', b'main\0'),
                        # Security status as on a dev-fused, undemoted board (tuple 0,0,1,1), as read by MobileGestalt: mobileactivationd's
                        # DeviceType then takes its native "Hactivation" branch (Activated without an activation record; no SEP or
                        # baseband). Values established on hardware by github.com/dipa-studio/ios-sweet (tools/mkadt.py).
                        *([('certificate-production-status', struct.pack('<I', 0)), ('effective-production-status-ap', struct.pack('<I', 0)),
                           ('certificate-security-mode', struct.pack('<I', 1)), ('effective-security-mode-sep', struct.pack('<I', 1))]
                          if args.iphone_identity else []),
                        # This property describes the actual volatile Data RAM disk.
                        *([('ephemeral-storage', struct.pack('<I', 1))] if (args.data_ramdisk or args.packed_data_ramdisk) else []),
                        *( [('darwinboot-memory-size', struct.pack('<Q', args.owned_memory_size))]
                           if args.owned_memory_size else [] ),
                        ('dram-base', struct.pack('<Q', DRAM_BASE)),
                        ('dram-size', struct.pack('<Q', args.dram_size)),
                        # Image4 hash algorithm. The target iPhone X's DT specifies sha2-384 in /chosen; this
                        # selects the algorithm only and asserts nothing about Apple silicon or fuses.
                        ('crypto-hash-method', b'sha2-384\0'),
                        *( [('root-matching', ('<dict><key>IOProviderClass</key><string>IOMedia</string>'
                                               '<key>IOPropertyMatch</key><dict><key>UUID</key><string>' +
                                               args.root_match_volume_uuid.upper() + '</string></dict></dict>\0').encode())]
                           if args.root_match_volume_uuid else [] ),
                        # Strict policy for the original AMFI: platform code only. This keeps AMFI
                        # enforcing; it does not relax it.
                        ('amfi-only-platform-code', struct.pack('<I', 1)),
                        *empty_nvram_proxy()],
             [node('memory-map', ([("PackedRAMDisk" if args.packed_data_ramdisk else "RAMDisk", bytes(16))] if (args.data_ramdisk or args.packed_data_ramdisk) else []) +
                                 ([('TrustCache', bytes(16))] if args.trust_cache else [])),
              # Read by the original AppleImage4 at start-up; present and empty here.
              node('manifest-properties')]),
        node('cpus', [('max_cpus', struct.pack('<I', len(cpus)))], children=cpus),
        *([node('psci', [('compatible', b'arm,psci-1.0\0'), ('method', args.psci_method.encode() + b'\0')])] if len(cpus) > 1 else []),
        # Suspend/resume is not implemented for this board. Use the original
        # root-domain policy input rather than allowing the CPU to park forever.
        node('defaults', [('sleep-disabled', struct.pack('<I', 1))]),
        node('options', NVRAM_OPTIONS),
        # The original AppleImage4 panics without a product node. Name and model are the
        # board DT's own strings.
        *( [node('buttons', [('compatible', b'buttons\0'), ('device_type', b'buttons\0'),
                             # No 'menu' entry: the phone has no Home button. 2 = the non-mechanical answer
                             # BSPlatform passes through (volume-down + seeded AssistiveTouch replaces Home later).
                             ('button-names', b'hold\0voldown\0volup\0'),
                             ('home-button-type', struct.pack('<I', 2))])] if args.iphone_identity else [] ),
        node('product', [('product-name', b'iPhone X\0' if args.iphone_identity else b'Xiaomi Redmi Note 7\0'),
                         ('model', b'xiaomi,lavender\0'),
                         *( [('product-description', b'iPhone X\0'),
                             # Display geometry stays the phone's own; MobileGestalt recovery is a post-boot
                             # toy, not critical-path (see notes/lavender-port-plan.md). 2436 = the 19.5:9
                             # screen height in pixels, 3x = the iPhone X scale.
                             ('artwork-device-idiom', b'phone\0'),
                             ('artwork-device-subtype', struct.pack('<I', 2436)),
                             ('artwork-scale-factor', struct.pack('<I', 3)),
                             # ios-sweet measured that P3 here makes SpringBoard take the extended-gamut
                             # path on an sRGB panel; the iPhone X tree says P3, the panel is sRGB.
                             ('artwork-display-gamut', b'sRGB\0'),
                             ('compatible-device-fallback', b'iPhone10,6\0'),
                             ('thin-bezel', struct.pack('<I', 1))] if args.iphone_identity else [] )]),
        node('arm-io', [('device_type', b'arm-io\0'),
                        ('#address-cells', struct.pack('<I', 2)),
                        ('#size-cells', struct.pack('<I', 2)),
                        # The window has to cover the redistributor frames as well:
                        # the GICv3 redistributor sits at ARMIO_BASE + 0xB00000 and is
                        # 1 MiB long, so the range has to reach 0x1B00000.
                        ('ranges', struct.pack('<QQQ', 0, ARMIO_BASE, ARMIO_SIZE))],
             [node('intc', [('compatible', b'arm,gic-v3\0'),
                            ('reg', struct.pack('<4Q',
                                                GIC_DIST - ARMIO_BASE, GIC_DIST_SIZE,
                                                GIC_REDIST - ARMIO_BASE, GIC_REDIST_SIZE)),
                            ('redistributor-stride', struct.pack('<Q', GIC_REDIST_STRIDE))]),
              # eMMC (0xc0c4000) lies below the arm-io window, so a child reg offset would be
              # negative. The node exists only for class matching; the planned NativeEmmc
              # driver (qcom,sdhci-msm-v5) takes its physical windows from the board DT
              # constants in its own header. There is no UFS controller on this SoC.
              node('emmc', [('compatible', b'qcom,sdhci-msm-v5\0')])]),
    ])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(tree)
    print(f'lavender ADT: {len(tree)} bytes, dram {args.dram_size:#x}, timebase {TIMEBASE}')


if __name__ == '__main__':
    main()
