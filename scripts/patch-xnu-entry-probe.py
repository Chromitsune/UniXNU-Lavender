#!/usr/bin/env python3
"""Put boot-progress probes into a COPY of the source XNU kernel (never the original).

There is no serial console on the device and the kernel cannot draw before its
own video console exists, so the early boot stages are reported by side
channels. The stage code lives in the zero padding after the kernel entry
(_start is one `b`) and displaced instructions are re-executed there:

  stage 1 (kernel entry, MMU off)       blue bar, then continues
  stage 2 (just before the MMU is on)   magenta bar, then continues
  stage 3 (right after the MMU is on)   waits --reset-delay seconds, then PSCI
                                        SYSTEM_RESET. A reboot by itself after
                                        that time shows the CPU got through the
                                        MMU enable; the frame buffer is not
                                        mapped by then, so time is the signal.
  post-MMU band (--post-mmu-band)       a white bar painted just after the MMU is
                                        on: the cave turns the MMU off at its
                                        physical alias, paints the physical frame
                                        buffer, turns the MMU on again and
                                        continues. Seeing the bar means the CPU
                                        executed with translation enabled.
  panic screen (--panic-screen)         panic() and its common panic funnel draw
                                        formatted diagnostics, append them to
                                        the kernel ring, wait 20 s, then issue
                                        PSCI SYSTEM_RESET via SMC. The board
                                        must provide a ring descriptor.
                                        --no-reset leaves out stages 3 and 4;
                                        it does not disable panic recovery.
  stage 4 (--reset-after SYMBOL)        the same wait and reset, but placed after
                                        the call to SYMBOL in --in FUNCTION (the
                                        Nth call, --occurrence). It replaces
                                        stage 3, so one run asks one question:
                                        did the kernel get past that call?

The bars are the 1/16th bands of the frame buffer below the green one that
DarwinBoot paints (band index = stage). Each patch site is checked against its
expected original instruction first, and the tool refuses a kernel that does
not match. Offsets are specific to the kernel built by build-xnu-4k-baseline.sh.
"""
import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ENTRY_VA = 0xFFFFFFF00B9D0488        # LC_UNIXTHREAD pc, `b start_first_cpu`
START_FIRST_CPU = 0xFFFFFFF00B9D205C
MMU_ENABLE_VA = 0xFFFFFFF00B9D2330   # msr SCTLR_EL1, x0
AFTER_MMU_VA = 0xFFFFFFF00B9D2350    # mov x0, #0x300000
PANIC_VA = 0xFFFFFFF00BF69B04      # _panic
VINFO_VA = 0xFFFFFFF00C0A2970      # _vinfo
PANIC_CAVE_END = 0xFFFFFFF00B9D0FF0
CAVE_SIZE = 0x60
CAVES = {1: 0xFFFFFFF00B9D0500, 2: 0xFFFFFFF00B9D0560, 3: 0xFFFFFFF00B9D05C0, 4: 0xFFFFFFF00B9D0620,
         5: 0xFFFFFFF00B9D0680}
# Stage 5 runs with the MMU on, where the panel is still unmapped, so it is
# larger than the reset/bar caves (it drops the MMU at a physical alias, paints
# and turns the MMU back on).
STAGE_SIZE = {1: CAVE_SIZE, 2: CAVE_SIZE, 3: CAVE_SIZE, 4: CAVE_SIZE, 5: 0x200}
TIMEBASE = 19200000
BOOT_ARGS = 0        # the kernel's copy of the boot arguments (panic screen fallback)
RING_DESC_MAGIC = b"LAVLOCRINGDESC1\x00"
RING_HEADER_BYTES = 0x40

PAINT = """
  ldr x1, [{args}, #0x28]
  ldr x2, [{args}, #0x38]
  ldr x3, [{args}, #0x48]
  mul x2, x2, x3
  lsr x2, x2, #4
  bic x2, x2, #7
  mov x4, #{band}
  mul x5, x2, x4
  add x1, x1, x5
  movz w6, #{lo}
  movk w6, #{hi}, lsl #16
1:
  stp w6, w6, [x1], #8
  subs x2, x2, #8
  b.hi 1b
  dsb sy
"""

SOURCES = {
    1: ".text\n.global _start\n_start:\n" + PAINT.format(args='x0', band=1, lo='0x00FF', hi='0xFF00') + "  b target\n",
    2: ".text\n.global _start\n_start:\n" + PAINT.format(args='x20', band=2, lo='0x00FF', hi='0xFFFF')
       + "  msr SCTLR_EL1, x0\n  b target\n",
    3: """.text
.global _start
_start:
  mov x0, #0x300000
  mrs x1, cntpct_el0
  ldr x2, ={ticks}
  add x2, x1, x2
1:
  isb
  mrs x1, cntpct_el0
  cmp x1, x2
  b.lo 1b
  movz w0, #0x0009
  movk w0, #0x8400, lsl #16
  mov x1, xzr
  mov x2, xzr
  mov x3, xzr
  smc #0
2:
  wfe
  b 2b
""" + ".ltorg\n",
    4: """.text
.global _start
_start:
  bl callee
  mrs x1, cntpct_el0
  ldr x2, ={ticks}
  add x2, x1, x2
1:
  isb
  mrs x1, cntpct_el0
  cmp x1, x2
  b.lo 1b
  movz w0, #0x0009
  movk w0, #0x8400, lsl #16
  mov x1, xzr
  mov x2, xzr
  mov x3, xzr
  smc #0
2:
  wfe
  b 2b
""" + ".ltorg\n",
}

# Stage 5 runs where the MMU is already on, but the panel sits outside the
# bootstrap identity map, so the frame buffer cannot be written through the
# kernel's mappings yet.  Paint through the physical alias instead:
#   1. remember SCTLR, clear the M bit,
#   2. branch to the same code at its physical address (mapped 1:1),
#   3. turn the MMU off, paint the band into the physical frame buffer,
#   4. turn the MMU on again and continue where the probe displaced.
# If the band appears, the CPU executed with translation on (the fetch after
# `msr SCTLR_EL1` and the branch that reached this cave); if not, it died in
# the first moments after the MMU came up.
SOURCES[5] = ("""
.text
.global _start
_start:
  mrs x8, sctlr_el1
  bic x9, x8, #1
  adr x10, 1f
  br x10
  .org 0x40
1:
  msr sctlr_el1, x9
  isb
""" + PAINT.format(args='x20', band='{band}', lo='{lo}', hi='{hi}') + """
  msr sctlr_el1, x8
  isb
  mov x0, #0x300000
  b target
""")

RESET_BEFORE = """.text
.global _start
_start:
  movz w0, #0x0009
  movk w0, #0x8400, lsl #16
  mov x1, xzr
  mov x2, xzr
  mov x3, xzr
  smc #0
1:
  wfe
  b 1b
"""
def kernel_symbols(kernel):
    """Address -> name for the kernel's symbols, from llvm-nm."""
    nm = shutil.which('llvm-nm') or '/opt/homebrew/opt/llvm/bin/llvm-nm'
    table = {}
    for line in subprocess.run([nm, '-n', kernel], capture_output=True, text=True, check=True).stdout.splitlines():
        parts = line.split()
        if len(parts) == 3:
            table.setdefault(int(parts[0], 16), parts[2])
    return table


def function_range(symbols, name):
    """Start and end (next symbol) of a function by its symbol name."""
    starts = sorted(symbols)
    for start, symbol in symbols.items():
        if symbol == name:
            return start, next(address for address in starts if address > start)
    raise SystemExit(f'No symbol {name} in the kernel')


def find_call(data, symbols, function, name, occurrence):
    """The VA and target of the Nth `bl name` inside FUNCTION."""
    hits = []
    low, high = function_range(symbols, function)
    for va in range(low, high, 4):
        word = struct.unpack_from('<I', data, file_offset(va))[0]
        if word >> 26 == 0b100101:
            delta = word & 0x3FFFFFF
            delta -= (1 << 26) if delta & (1 << 25) else 0
            target = va + delta * 4
            if symbols.get(target) == name:
                hits.append((va, target))
    if len(hits) < occurrence:
        raise SystemExit(f'{function} has {len(hits)} call(s) to {name}, not {occurrence}')
    return hits[occurrence - 1]


def assemble_c(source, va, workdir, symbols, ring=None):
    """Build a freestanding C file for VA; returns (bytes, load address of the first byte, address of panic_screen).

    The linker may start .text a little after VA (it keeps the ELF headers in front), so the
    load address is read back from the ELF and is what the caller must write the bytes to."""
    clang = shutil.which('clang') or '/opt/homebrew/opt/llvm/bin/clang'
    lld = shutil.which('ld.lld') or '/opt/homebrew/opt/llvm/bin/ld.lld'
    objcopy = shutil.which('llvm-objcopy') or '/opt/homebrew/opt/llvm/bin/llvm-objcopy'
    nm = shutil.which('llvm-nm') or '/opt/homebrew/opt/llvm/bin/llvm-nm'
    # UNIXNU_UEFI is the Mu-Silicium checkout when the unixnu driver runs this script.
    font_dir = Path(os.environ.get('UNIXNU_UEFI') or ROOT / 'uefi-sheng') / 'Silicon/Silicium/SiliciumPkg/Library/FrameBufferSerialPortLib'
    obj, elf, binary = (workdir / name for name in ('panic.o', 'panic.elf', 'panic.bin'))
    definitions = []
    for macro, name, width in (('MAX_MEM', '_max_mem', 8),
                               ('PAGE_TOTAL', '_vm_page_pages', 4),
                               ('PAGE_FREE', '_vm_page_free_count', 4)):
        address = symbols.get(name)
        if address is None or address % width or not any(
                base <= address and address + width <= base + size
                for base, size, _, _ in SEGMENTS):
            raise SystemExit(f'Missing or invalid diagnostic symbol {name}')
        definitions.append(f'-D{macro}={address:#x}UL')
    # Optional: the board's banner buffer for the panic message (kits/sheng/BootProgress.c); kernels without it get 0 (no copy).
    text = symbols.get('_boot_progress_panic_text')
    if text is not None:
        if not any(base <= text and text + 256 <= base + size for base, size, _, _ in SEGMENTS):
            raise SystemExit('Invalid _boot_progress_panic_text')
        definitions.append(f'-DPANIC_TEXT={text:#x}UL')
    # The kernel's own copy of the boot arguments, used when the video console
    # does not exist yet and the panic screen falls back to the frame buffer.
    boot_args = BOOT_ARGS or symbols.get('_const_boot_args') or 0
    ring_va, ring_data = ring if ring is not None else (0, 0)
    definitions.extend((f'-DPANIC_RING={ring_va:#x}UL', f'-DPANIC_RING_DATA={ring_data:#x}UL'))
    subprocess.run([clang, '--target=aarch64-none-elf', '-ffreestanding', '-fno-builtin', '-fno-stack-protector',
                    '-mgeneral-regs-only', '-fno-pic', '-mcmodel=tiny', '-Oz', f'-DVINFO={VINFO_VA:#x}UL',
                    f'-DBOOT_ARGS={boot_args:#x}UL',
                    *definitions, f'-I{font_dir}', '-c', source, '-o', obj], check=True)
    script = workdir / 'panic.ld'
    script.write_text('SECTIONS { . = %#x; .text : { *(.text.a_entry) *(.text.a_common) *(.text*) *(.rodata*) } /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) *(.ARM.*) } }\n' % va)
    size = shutil.which('llvm-size') or '/opt/homebrew/opt/llvm/bin/llvm-size'
    sizes = subprocess.run([size, '--format=berkeley', obj], capture_output=True, text=True, check=True).stdout.splitlines()[-1].split()
    if int(sizes[1]) or int(sizes[2]):
        # The code runs from the kernel's read-only text: a global would fault on its first store.
        raise SystemExit(f'{source.name} has writable data (data {sizes[1]}, bss {sizes[2]}); use the stack')
    subprocess.run([lld, '-T', script, '-e', 'panic_screen', obj, '-o', elf], check=True)
    # Only .text: a plain binary dump would start at the ELF headers kept in front of it.
    subprocess.run([objcopy, '-O', 'binary', '-j', '.text', elf, binary], check=True)
    readelf = shutil.which('llvm-readelf') or '/opt/homebrew/opt/llvm/bin/llvm-readelf'
    load = None
    for line in subprocess.run([readelf, '-S', elf], capture_output=True, text=True, check=True).stdout.splitlines():
        parts = line.replace('[', ' ').replace(']', ' ').split()
        if len(parts) >= 4 and parts[1] == '.text':
            load = int(parts[3], 16)
    if load is None or load < va:
        raise SystemExit('Cannot find where the linker put .text')
    entry = None
    for line in subprocess.run([nm, elf], capture_output=True, text=True, check=True).stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == 'panic_screen':
            entry = int(parts[0], 16)
    if entry != load:
        raise SystemExit(f'panic_screen at {entry:#x} is not the first byte {load:#x}')
    return binary.read_bytes(), load, entry


def panic_common_entry(workdir, code, load):
    """Resolve the real alternate entry and verify its distinct argument ABI."""
    nm = shutil.which('llvm-nm') or '/opt/homebrew/opt/llvm/bin/llvm-nm'
    names = {parts[2]: int(parts[0], 16)
             for line in subprocess.check_output([nm, workdir / 'panic.elf'], text=True).splitlines()
             if len(parts := line.split()) == 3}
    entry = names.get('panic_screen_common')
    main = names.get('panic_screen_main')
    if entry is None or main is None or not load <= entry <= load + len(code) - 8:
        raise SystemExit('Common panic renderer entry is missing or out of bounds')
    at = entry - load
    if code[at:at + 8] != struct.pack('<II', 0xf9400021, branch(entry + 4, main)):
        raise SystemExit('Common panic entry does not dereference the Darwin va_list pointer and branch to the renderer')
    return entry


ROOT = Path(__file__).resolve().parents[1]


SEGMENTS = []   # (vmaddr, vmsize, fileoff, filesize) of the kernel being patched
EXEC_SEGMENTS = []  # (vmaddr, vmsize, fileoff, filesize) executable file-backed ranges


def load_segments(data):
    """Read the Mach-O segment table; the file layout differs between kernel builds."""
    magic, _, _, _, ncmds = struct.unpack_from('<IiiII', data, 0)
    if magic != 0xFEEDFACF:
        raise SystemExit('Not a 64-bit Mach-O')
    offset = 32
    SEGMENTS.clear()
    EXEC_SEGMENTS.clear()
    for _ in range(ncmds):
        cmd, size = struct.unpack_from('<II', data, offset)
        if cmd == 0x19:
            _, vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<16sQQQQ', data, offset + 8)
            initprot = struct.unpack_from('<I', data, offset + 60)[0]
            if filesize:
                SEGMENTS.append((vmaddr, vmsize, fileoff, filesize))
                if initprot & 4:
                    EXEC_SEGMENTS.append((vmaddr, vmsize, fileoff, filesize))
        offset += size


def find_ring_descriptor(data):
    """Return (ring VA, data capacity) from the instrumented kernel descriptor."""
    offsets = []
    start = 0
    while True:
        at = data.find(RING_DESC_MAGIC, start)
        if at < 0:
            break
        offsets.append(at)
        start = at + 1
    if len(offsets) != 1:
        raise SystemExit(f'panic screen requires one log-ring descriptor; found {len(offsets)}')
    at = offsets[0]
    ring, total, capacity, reserved = struct.unpack_from('<QQQQ', data, at + len(RING_DESC_MAGIC))
    if (reserved != 0 or capacity < 0x1000 or capacity & (capacity - 1) or
            total != RING_HEADER_BYTES + capacity or ring > (1 << 64) - 1 - total):
        raise SystemExit('panic screen found malformed log-ring geometry')
    if not any(base <= ring and ring + total <= base + size for base, size, _, _ in SEGMENTS):
        raise SystemExit('panic ring does not fit in a kernel segment')
    return ring, capacity


def cave_referenced(data, low, high):
    """Reject caves targeted by direct branches or ADRP page references."""
    low_page = low & ~0xFFF
    high_page = (high + 0xFFF) & ~0xFFF
    for vmaddr, vmsize, fileoff, filesize in EXEC_SEGMENTS:
        words = struct.unpack_from('<%dI' % (min(vmsize, filesize) // 4), data, fileoff)
        for index, word in enumerate(words):
            pc = vmaddr + index * 4
            if (word & 0x9F000000) == 0x90000000:  # ADRP
                imm = (((word >> 5) & 0x7FFFF) << 2) | ((word >> 29) & 3)
                if imm & (1 << 20): imm -= 1 << 21
                page = (pc & ~0xFFF) + (imm << 12)
                if low_page <= page < high_page:
                    return True
            # B/BL, B.cond, CBZ/CBNZ and TBZ/TBNZ direct targets.
            if (word & 0x7C000000) in (0x14000000, 0x94000000):
                imm = word & 0x3FFFFFF
                if imm & (1 << 25): imm -= 1 << 26
                target = pc + (imm << 2)
            elif (word & 0xFF000010) == 0x54000000 or (word & 0x7E000000) == 0x34000000:
                imm = (word >> 5) & 0x7FFFF
                if imm & (1 << 18): imm -= 1 << 19
                target = pc + (imm << 2)
            elif (word & 0x7E000000) == 0x36000000:
                imm = (word >> 5) & 0x3FFF
                if imm & (1 << 13): imm -= 1 << 14
                target = pc + (imm << 2)
            else:
                continue
            if low <= target < high:
                return True
    return False


def find_exec_cave(data, minimum):
    """Find the largest unreferenced executable file-backed zero/NOP run."""
    best = None
    for vmaddr, vmsize, fileoff, filesize in EXEC_SEGMENTS:
        end = fileoff + min(vmsize, filesize)
        offset = fileoff
        while offset + 4 <= end:
            word = struct.unpack_from('<I', data, offset)[0]
            if word not in (0, 0xD503201F):
                offset += 4
                continue
            start = offset
            while offset + 4 <= end and struct.unpack_from('<I', data, offset)[0] in (0, 0xD503201F):
                offset += 4
            length = offset - start
            va = vmaddr + start - fileoff
            if length >= minimum and not cave_referenced(data, va, va + length) and (best is None or length > best[0]):
                best = (length, va)
    if best is None:
        raise SystemExit(f'no unreferenced executable zero/NOP panic cave of {minimum:#x} bytes')
    return best[1], best[0]


def unixthread_pc(data):
    """LC_UNIXTHREAD entry pc (arm64 thread state: flavor, count, x0..x28, fp, lr, sp, pc)."""
    offset = 32
    for _ in range(struct.unpack_from('<I', data, 16)[0]):
        cmd, size = struct.unpack_from('<II', data, offset)
        if cmd == 0x5:
            return struct.unpack_from('<Q', data, offset + 16 + 32 * 8)[0]
        offset += size
    raise SystemExit('No LC_UNIXTHREAD in kernel')


def rebase(kernel, data):
    """The probe sites are fixed offsets from the entry; shift them for another kernel build.

    _panic and _vinfo come from the symbol table when the kernel has one (the plain source
    kernel does); the retained candidate layout is the unshifted case.
    """
    global ENTRY_VA, START_FIRST_CPU, MMU_ENABLE_VA, AFTER_MMU_VA, PANIC_VA, VINFO_VA, PANIC_CAVE_END, CAVES
    delta = unixthread_pc(data) - ENTRY_VA
    if delta:
        ENTRY_VA += delta
        START_FIRST_CPU += delta
        MMU_ENABLE_VA += delta
        AFTER_MMU_VA += delta
        PANIC_CAVE_END += delta
        CAVES = {stage: va + delta for stage, va in CAVES.items()}
        PANIC_VA += delta
        VINFO_VA += delta
    names = {name: va for va, name in kernel_symbols(kernel).items()}
    if '_start_first_cpu' in names:
        START_FIRST_CPU = names['_start_first_cpu']
        # Native bootstrap table-capacity checks can grow before SCTLR setup.
        # Locate the unique instruction sequence in this bounded entry body;
        # do not assume a constant offset from the Mach-O entry point.
        matches = []
        for va in range(START_FIRST_CPU, START_FIRST_CPU + 0x1000, 4):
            def instruction(at):
                return struct.unpack_from('<I', data, file_offset(at))[0]
            if (instruction(va) == 0xD5181000 and
                    instruction(va + 4) == 0xD5033FDF and
                    instruction(va + 0x20) == 0xD2A00600 and
                    instruction(va + 0x24) == 0xD5181040):
                matches.append(va)
        if len(matches) != 1:
            raise SystemExit('Require one verified SCTLR/ISB/CPACR entry sequence')
        MMU_ENABLE_VA = matches[0]
        AFTER_MMU_VA = MMU_ENABLE_VA + 0x20
    if '_panic' in names and '_vinfo' in names:
        PANIC_VA = names['_panic']
        # Board diagnostics seed this ABI-compatible prefix from valid console
        # mappings, then pin it to their retained GOP mapping. Ordinary source
        # kernels without the observer retain the original vinfo path.
        VINFO_VA = names.get('_boot_progress_panic_vinfo', names['_vinfo'])
    return names


def file_offset(va):
    for vmaddr, vmsize, fileoff, filesize in SEGMENTS:
        if vmaddr <= va < vmaddr + min(vmsize, filesize):
            return fileoff + (va - vmaddr)
    raise SystemExit(f'{va:#x} is not in a file-backed segment of this kernel')


def branch(from_va, to_va):
    delta = (to_va - from_va) // 4
    if (to_va - from_va) % 4 or not -(1 << 25) <= delta < (1 << 25):
        raise SystemExit('Branch out of range')
    return 0x14000000 | (delta & 0x3FFFFFF)


def assemble(source, va, symbols, workdir):
    asm, obj, elf, binary = (workdir / name for name in ('cave.S', 'cave.o', 'cave.elf', 'cave.bin'))
    asm.write_text(source)
    clang = shutil.which('clang') or '/opt/homebrew/opt/llvm/bin/clang'
    lld = shutil.which('ld.lld') or '/opt/homebrew/opt/llvm/bin/ld.lld'
    objcopy = shutil.which('llvm-objcopy') or '/opt/homebrew/opt/llvm/bin/llvm-objcopy'
    subprocess.run([clang, '--target=aarch64-none-elf', '-c', asm, '-o', obj], check=True)
    subprocess.run([lld, f'-Ttext={va:#x}', *[f'--defsym={name}={value:#x}' for name, value in symbols.items()], '-e', '_start', obj, '-o', elf], check=True)
    subprocess.run([objcopy, '-O', 'binary', elf, binary], check=True)
    return binary.read_bytes()


def padded_clear(data, start, length):
    """Padding that may be zero-fill or linker NOP alignment (0xd503201f).

    Full words must be zero or NOP. A non-word-aligned remainder is only tolerated
    when the word containing it is pure zero or pure NOP filler (the panic code is
    branch-targeted, so it never depends on padding contents; this check exists to
    refuse a wrong site where real instructions or data live).
    """
    whole = length - length % 4
    if whole:
        for value in struct.unpack_from(f'<{whole // 4}I', data, start):
            if value not in (0, 0xD503201F):
                return False
    tail_start, tail = start + whole, data[start + whole:length]
    if not tail:
        return True
    word_start = start + whole - 4
    word = struct.unpack_from('<I', data, max(word_start, 0))[0]
    return all(not b for b in tail) or word in (0, 0xD503201F)


def padding_extent(data, va):
    """The padded run around va: (start, end) virtual addresses."""
    at = file_offset(va)
    start = at
    while start >= 4 and struct.unpack_from('<I', data, start - 4)[0] in (0, 0xD503201F):
        start -= 4
    end = at
    while struct.unpack_from('<I', data, end)[0] in (0, 0xD503201F):
        end += 4
    for vmaddr, vmsize, fileoff, filesize in SEGMENTS:
        if fileoff <= at < fileoff + filesize:
            return vmaddr + (start - fileoff), vmaddr + (end - fileoff)
    raise SystemExit('cave is not in a file-backed segment')


def main():
    global PANIC_VA, VINFO_VA
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('kernel', type=Path, help='the COPY of the kernel to patch in place')
    parser.add_argument('--reset-delay', type=int, default=6, help='seconds before the reset')
    parser.add_argument('--reset-after', metavar='SYMBOL', help='place the reset after this call in arm_init instead of stage 3')
    parser.add_argument('--reset-before', action='store_true', help='reset at the call site before invoking SYMBOL (requires --reset-after)')
    parser.add_argument('--panic-screen', action='store_true', help='install panic recovery screen and software reset')
    parser.add_argument('--panic-screen-only', action='store_true',
                        help='patch only panic recovery; resolve symbols/cave dynamically, no entry-probe offsets')
    parser.add_argument('--no-reset', action='store_true', help='no reset probe at all')
    parser.add_argument('--post-mmu-band', action='store_true',
                        help='paint a white bar just after the MMU is on (drops the MMU at a physical alias to reach the panel)')
    parser.add_argument('--occurrence', type=int, default=1, help='which call to SYMBOL (1 = first)')
    parser.add_argument('--boot-args', type=lambda value: int(value, 0),
                        help='the address of the kernel\'s copy of the boot arguments')
    parser.add_argument('--panic-cave', type=lambda value: int(value, 0),
                        help='the address of the padding the panic screen is placed in')
    parser.add_argument('--in', dest='function', default='_arm_init', help='function that makes the call (default _arm_init)')
    args = parser.parse_args()
    if args.panic_screen_only and not args.panic_screen:
        raise SystemExit('--panic-screen-only requires --panic-screen')
    if args.panic_screen_only and (args.reset_after or args.reset_before or args.post_mmu_band):
        raise SystemExit('--panic-screen-only cannot be combined with entry reset/band probes')
    if args.reset_before and not args.reset_after:
        raise SystemExit('--reset-before requires --reset-after')
    data = bytearray(args.kernel.read_bytes())
    load_segments(data)
    if args.panic_screen_only:
        kernel_names = {name: va for va, name in kernel_symbols(args.kernel).items()}
        if '_panic' not in kernel_names or '_panic_trap_to_debugger' not in kernel_names:
            raise SystemExit('panic-only patch requires _panic and _panic_trap_to_debugger symbols')
        if '_vinfo' not in kernel_names and '_boot_progress_panic_vinfo' not in kernel_names:
            raise SystemExit('panic-only patch requires a valid _vinfo mapping symbol')
        PANIC_VA = kernel_names['_panic']
        VINFO_VA = kernel_names.get('_boot_progress_panic_vinfo', kernel_names.get('_vinfo'))
    else:
        kernel_names = rebase(args.kernel, data)

    def word(va):
        return struct.unpack_from('<I', data, file_offset(va))[0]

    sites = []
    if not args.panic_screen_only:
        expected = {ENTRY_VA: branch(ENTRY_VA, START_FIRST_CPU), MMU_ENABLE_VA: 0xD5181000, AFTER_MMU_VA: 0xD2A00600}
        for va, value in expected.items():
            if word(va) != value:
                raise SystemExit(f'{va:#x} holds {word(va):#010x}, expected {value:#010x}: not the kernel these probes are for')
        for va in CAVES.values():
            if not padded_clear(data, file_offset(va), CAVE_SIZE):
                raise SystemExit(f'Padding at {va:#x} is not empty (nor NOP-alignment fill)')
        # (site, cave stage, symbols the cave is linked with)
        sites = [(ENTRY_VA, 1, {'target': START_FIRST_CPU}),
                 (MMU_ENABLE_VA, 2, {'target': MMU_ENABLE_VA + 4})]
    if args.no_reset or args.panic_screen_only:
        description = 'no reset probe'
    elif args.reset_after:
        symbols = kernel_symbols(args.kernel)
        call_va, callee = find_call(data, symbols, args.function, args.reset_after, args.occurrence)
        sites.append((call_va, 4, {'callee': callee, 'target': call_va + 4}))
        description = f'stage 4 reset {"before" if args.reset_before else "after"} call #{args.occurrence} to {args.reset_after} in {args.function} (at {call_va:#x})'
    else:
        sites.append((AFTER_MMU_VA, 3, {'target': AFTER_MMU_VA + 4}))
        description = f'stage 3 reset {args.reset_delay} s after the MMU is on'
    if args.post_mmu_band:
        sites.append((AFTER_MMU_VA, 5, {'target': AFTER_MMU_VA + 4}))
        description += ', post-MMU white bar'

    with tempfile.TemporaryDirectory() as tmp:
        for site, stage, symbols in sites:
            if stage == 5:
                # Everything is relative.  The prologue branch runs before the
                # kernel constructs a KVA, so the cave is executing at its
                # physical alias already (the V=P identity map): `adr` gives the
                # address to switch the MMU off at, and `b target` resumes at the
                # identity alias of the displaced instruction.  No absolute
                # addresses, so a loader slide (the phone and QEMU disagree on
                # the base) cannot break it.
                source = SOURCES[5].format(band=3, lo='0xFFFF', hi='0xFFFF')
            else:
                source = RESET_BEFORE if (stage == 4 and args.reset_before) else SOURCES[stage].format(ticks=TIMEBASE * args.reset_delay)
            code = assemble(source, CAVES[stage], symbols, Path(tmp))
            if len(code) > STAGE_SIZE[stage]:
                raise SystemExit(f'Stage {stage} code is too large')
            if not padded_clear(data, file_offset(CAVES[stage]), STAGE_SIZE[stage]):
                raise SystemExit(f'Padding after stage {stage} is not empty')
            data[file_offset(CAVES[stage]):file_offset(CAVES[stage]) + len(code)] = code
    for site, stage, _ in sites:
        struct.pack_into('<I', data, file_offset(site), branch(site, CAVES[stage]))
    if args.panic_screen:
        if b'LAVPANICRECOVERY1' in data:
            raise SystemExit('panic recovery marker already present; refusing to patch twice')
        panic_ring = find_ring_descriptor(data)
        if args.panic_screen_only:
            PANIC_CAVE, panic_bytes = find_exec_cave(data, 0x400)
            panic_limit = PANIC_CAVE + panic_bytes
            panic_cave_bytes = panic_bytes
        else:
            # Right after the last probe cave in use, so the padding is shared as tightly as possible.
            PANIC_CAVE = max(CAVES[stage] + STAGE_SIZE[stage] for _, stage, _ in sites)
            panic_limit = PANIC_CAVE_END
            panic_cave_bytes = file_offset(panic_limit) - file_offset(PANIC_CAVE)
        if args.panic_cave:
            # A different kernel has a different padding layout: use the given
            # padding and check it against the space that is actually there.
            cave_start, cave_end = padding_extent(data, args.panic_cave)
            PANIC_CAVE = max(cave_start, args.panic_cave)
            panic_limit = cave_end
            panic_cave_bytes = file_offset(panic_limit - 4) + 4 - file_offset(PANIC_CAVE)
        if (word(PANIC_VA) in (0, 0xFFFFFFFF) or panic_cave_bytes <= 0 or
                not padded_clear(data, file_offset(PANIC_CAVE), panic_cave_bytes)):
            raise SystemExit('panic() or the panic screen padding is not as expected')
        with tempfile.TemporaryDirectory() as tmp:
            code, load, entry = assemble_c(ROOT / 'scripts/xnu-panic-screen.c', PANIC_CAVE, Path(tmp), kernel_names, panic_ring)
            common_entry = panic_common_entry(Path(tmp), code, load)
        if load + len(code) > panic_limit:
            raise SystemExit(f'panic screen is {len(code):#x} bytes at {load:#x}, the padding ends at {panic_limit:#x}')
        if not padded_clear(data, file_offset(load), len(code)):
            raise SystemExit(f'Padding at {load:#x} is not empty (nor NOP-alignment fill)')
        data[file_offset(load):file_offset(load) + len(code)] = code
        struct.pack_into('<I', data, file_offset(PANIC_VA), branch(PANIC_VA, entry))
        common = kernel_names.get('_panic_trap_to_debugger')
        if common is None or word(common) in (0, 0xFFFFFFFF):
            raise SystemExit('Common panic funnel is missing; refuse a partially covered panic screen')
        struct.pack_into('<I', data, file_offset(common), branch(common, common_entry))
        description += f', panic screen ({len(code):#x} bytes at {entry:#x}; common funnel at {common:#x})'
    args.kernel.write_bytes(data)
    print(f'patched {args.kernel.name}: {description}')

if __name__ == '__main__':
    main()
