#!/usr/bin/env python3
"""Paint a ladder of checkpoints through the XNU early boot.

The kernel entry code runs with the MMU off, so it cannot report anything. This
tool rewrites a COPY of the kernel: at each milestone of the early boot it
replaces one instruction with a branch into a code cave that paints a coloured
band into the frame buffer (the frame buffer address comes from the boot
arguments), re-executes the displaced instruction and returns.

Bands are painted top to bottom, so the screen shows exactly how far the kernel
got. Nothing here changes the original kernel or the boot arguments.
"""
import argparse
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

SEGMENTS = []

PAINT = """
  ldr x1, [{args}, #0x28]
  ldr x2, [{args}, #0x38]
  mov x3, #{band}
  sub x3, x3, #1
  mov x4, #16
  mul x3, x3, x4
  mul x3, x3, x2
  add x1, x1, x3
  lsr x2, x2, #3
  mov x3, #16
  movz w6, #{lo}
  movk w6, #{hi}, lsl #16
1:
  mov x4, x1
  mov x5, x2
2:
  stp w6, w6, [x4], #8
  subs x5, x5, #1
  b.hi 2b
  add x1, x1, x4
  subs x3, x3, #1
  b.hi 1b
  dsb sy
"""

# The same paint, but the boot arguments are reached through a kernel global
# (const_boot_args) instead of a register.  Needed once the kernel is in C,
# where no register still holds the boot arguments.  The frame buffer itself
# is mapped by the early bootstrap tables, so it is reachable from C.
PAINT_GLOBAL = """
  adrp x9, global
  add x9, x9, {off}
""" + PAINT.format(args='x9', band='{band}', lo='{lo}', hi='{hi}')

# The colours are 0xAARRGGBB in the frame buffer (BGRA on this panel).
COLOURS = [
    (0x00FF, 0x0000),   # band 2: green
    (0x00FF, 0x00FF),   # band 3: cyan
    (0x0000, 0x00FF),   # band 4: blue
    (0x00FF, 0xFF00),   # band 5: magenta
    (0x0000, 0xFF00),   # band 6: yellow
    (0xFF00, 0x0000),   # band 7: red
    (0xFFFF, 0xFFFF),   # band 8: white
]

# Milestones, in execution order: the instruction word to find and the name.
MILESTONES = [
    ('msr VBAR_EL1, x0', 0xD518C000),
    ('ldr x22, [x20, #8]', 0xF9400696),
    ('ldr w1, [x0, #0x18]', 0xB9401801),
    ('dsb ish', 0xD5033B9F),
    ('msr TCR_EL1, x1', 0xD5182041),
    ('msr TTBR1_EL1, x0', 0xD5182020),
    ('msr SCTLR_EL1, x0', 0xD5181000),
]

CAVE_SIZE = 0x80
NOP = 0xD503201F


def load_segments(data):
    magic, _, _, _, ncmds = struct.unpack_from('<IiiII', data, 0)
    if magic != 0xFEEDFACF:
        raise SystemExit('Not a 64-bit Mach-O')
    offset = 32
    SEGMENTS.clear()
    for _ in range(ncmds):
        cmd, size = struct.unpack_from('<II', data, offset)
        if cmd == 0x19:
            _, vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<16sQQQQ', data, offset + 8)
            if filesize:
                SEGMENTS.append((vmaddr, vmsize, fileoff, filesize))
        offset += size


def file_offset(va):
    for vmaddr, vmsize, fileoff, filesize in SEGMENTS:
        if vmaddr <= va < vmaddr + min(vmsize, filesize):
            return fileoff + (va - vmaddr)
    raise SystemExit(f'{va:#x} is not in a file-backed segment')


def branch(from_va, to_va):
    delta = (to_va - from_va) // 4
    if (to_va - from_va) % 4 or not -(1 << 25) <= delta < (1 << 25):
        raise SystemExit('Branch out of range')
    return 0x14000000 | (delta & 0x3FFFFFF)


def assemble(source, va, workdir, symbols):
    asm, obj, elf, binary = (workdir / name for name in ('cave.S', 'cave.o', 'cave.elf', 'cave.bin'))
    asm.write_text(source)
    clang = '/Users/rebuke/aOS/toolchain/llvm-combined/bin/clang'
    if not Path(clang).is_file():
        clang = shutil.which('clang') or '/opt/homebrew/opt/llvm/bin/clang'
    lld = '/Users/rebuke/aOS/toolchain/llvm-combined/bin/ld.lld'
    if not Path(lld).is_file():
        lld = shutil.which('ld.lld') or '/opt/homebrew/opt/llvm/bin/ld.lld'
    objcopy = '/Users/rebuke/aOS/toolchain/llvm-combined/bin/llvm-objcopy'
    if not Path(objcopy).is_file():
        objcopy = shutil.which('llvm-objcopy') or '/opt/homebrew/opt/llvm/bin/llvm-objcopy'
    subprocess.run([clang, '--target=aarch64-none-elf', '-c', asm, '-o', obj], check=True)
    subprocess.run([lld, f'-Ttext={va:#x}', *[f'--defsym={name}={value:#x}' for name, value in symbols.items()],
                    '-e', '_start', obj, '-o', elf], check=True)
    subprocess.run([objcopy, '-O', 'binary', elf, binary], check=True)
    return binary.read_bytes()


def find_cave(data, start_va, count):
    """Find the largest run of padding (zeros or NOPs) that holds the caves."""
    need = count * CAVE_SIZE
    best = None
    for segment in SEGMENTS:
        vmaddr, vmsize, fileoff, filesize = segment
        if not (vmaddr <= start_va < vmaddr + vmsize):
            continue  # stay in the segment that holds the entry code
        at = fileoff
        end = fileoff + filesize
        while at < end - 4:
            word = struct.unpack_from('<I', data, at)[0]
            if word in (0, NOP):
                stop = at
                while stop < end - 4 and struct.unpack_from('<I', data, stop)[0] in (0, NOP):
                    stop += 4
                if stop - at >= need and (best is None or stop - at > best[0]):
                    best = (stop - at, vmaddr + (at - fileoff))
                at = stop
            else:
                at += 4
    if best is None:
        raise SystemExit('no padding found for the probe caves')
    print(f'probe caves: {best[0]:#x} bytes of padding at {best[1]:#x}')
    return best[1]


def kernel_symbols(data):
    """Symbol table addresses of the kernel (LC_SYMTAB with 64-bit nlist)."""
    offset = 32
    symoff = stroff = None
    nsyms = 0
    for _ in range(struct.unpack_from('<I', data, 16)[0]):
        cmd, size = struct.unpack_from('<II', data, offset)
        if cmd == 0x2:
            symoff, nsyms, stroff = struct.unpack_from('<III', data, offset + 8)
            break
        offset += size
    if symoff is None:
        return {}
    names = {}
    for index in range(nsyms):
        n_strx, n_type, n_sect, n_desc = struct.unpack_from('<IBBH', data, symoff + 16 * index)
        value = struct.unpack_from('<Q', data, symoff + 16 * index + 8)[0]
        if not n_strx:
            continue
        end = data.find(b'\0', stroff + n_strx)
        name = data[stroff + n_strx:end].decode('ascii', 'replace')
        names.setdefault(name, value)
    return names


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('kernel', type=Path, help='the COPY of the kernel to patch in place')
    parser.add_argument('--function', default='_start_first_cpu',
                        help='the symbol whose body is scanned for the milestone instructions')
    parser.add_argument('--site', action='append', default=[],
                        metavar='WORD:LABEL',
                        help='an extra probe: the 32-bit instruction word to hook and its label')
    parser.add_argument('--call', action='append', default=[],
                        metavar='SYMBOL:LABEL',
                        help='an extra probe: the call to that symbol inside the function')
    parser.add_argument('--call-after', action='append', default=[],
                        metavar='SYMBOL:LABEL',
                        help='a probe on the instruction after the call to that symbol')
    parser.add_argument('--site-at', action='append', default=[],
                        metavar='ADDRESS:LABEL',
                        help='a probe on the instruction at an exact address')
    parser.add_argument('--band', type=int, default=2, help='the first band number to use')
    parser.add_argument('--args-global', metavar='SYMBOL',
                        help='read the boot arguments from this kernel global instead of a register')
    parser.add_argument('--args-address', type=lambda value: int(value, 0),
                        help='the address of the boot arguments copy in the kernel image')
    args = parser.parse_args()
    data = bytearray(args.kernel.read_bytes())
    load_segments(data)

    # The entry point is the first instruction of the kernel text.
    offset = 32
    entry = None
    for _ in range(struct.unpack_from('<I', data, 16)[0]):
        cmd, size = struct.unpack_from('<II', data, offset)
        if cmd == 0x5:
            entry = struct.unpack_from('<Q', data, offset + 16 + 32 * 8)[0]
            break
        offset += size
    if entry is None:
        raise SystemExit('No LC_UNIXTHREAD in kernel')

    # The probes live in the boot CPU entry function.
    names = kernel_symbols(data)
    start_va = names.get(args.function, entry)
    window_start = start_va
    window_end = start_va + 0x4000
    base = file_offset(window_start)
    limit = file_offset(window_end)

    exact = []
    for item in args.site_at:
        address, _, label = item.partition(':')
        exact.append((int(address, 0), label or address))

    milestones = [] if (args.site or exact) else list(MILESTONES)
    for item in args.site:
        word, _, label = item.partition(':')
        milestones.append((label or word, int(word, 0)))
    calls = []
    for item in args.call:
        symbol, _, label = item.partition(':')
        calls.append((symbol, label or symbol, False))
    for item in args.call_after:
        symbol, _, label = item.partition(':')
        calls.append((symbol, label or symbol, True))

    # A call site is found by its target: the call is a direct branch to the symbol.
    for symbol, label, after in calls:
        target = names.get(symbol) or names.get('_' + symbol)
        if target is None:
            raise SystemExit(f'kernel symbol not found: {symbol}')
        # The prelinked region sits a fixed distance from the linked image, so the
        # target is accepted at either address.  A call cannot be replayed inside
        # a cave (it would set the link register to the cave), so the probe can
        # be placed on the instruction after the call instead.
        found = None
        for vmaddr, vmsize, fileoff, filesize in SEGMENTS:
            if vmsize == 0:
                continue
            at = fileoff
            end = fileoff + filesize
            while at + 4 <= end:
                word = struct.unpack_from('<I', data, at)[0]
                if word & 0xFC000000 == 0x94000000:
                    site = vmaddr + (at - fileoff)
                    called = site + (word & 0x3FFFFFF) * 4
                    if called in (target, target + 0x100000000) and site >= window_start:
                        found = at + (4 if after else 0)
                        break
                at += 4
            if found is not None:
                break
        if found is None:
            raise SystemExit(f'call to {symbol} not found in the function')
        milestones.append((label, struct.unpack_from('<I', data, found)[0]))

    for address, label in exact:
        at = file_offset(address)
        milestones.append((label, struct.unpack_from('<I', data, at)[0]))

    sites = []
    for name, word in milestones:
        found = None
        at = base
        while at + 4 <= limit:
            if struct.unpack_from('<I', data, at)[0] == word:
                found = at
                break
            at += 4
        if found is None:
            raise SystemExit(f'milestone not found: {name}')
        sites.append((name, found, word))

    caves = find_cave(data, entry, len(sites) + 1)
    print(f'entry {entry:#x}, {len(sites)} probes, caves at {caves:#x}')

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for index, (name, at, word) in enumerate(sites):
            site_va = window_start + (at - base)
            cave_va = caves + index * CAVE_SIZE
            lo, hi = COLOURS[index % len(COLOURS)]
            band = args.band + index
            symbols = {'target': site_va + 4}
            if args.args_global or args.args_address:
                if args.args_address:
                    target = args.args_address
                else:
                    target = names.get(args.args_global)
                    if not target:
                        raise SystemExit(f'kernel symbol not found or common: {args.args_global}; '
                                         'pass --args-address with the address from the build')
                body = PAINT_GLOBAL.format(off=target & 0xFFF, band=band,
                                           lo=f'{lo:#x}', hi=f'{hi:#x}')
                symbols['global'] = target
            else:
                body = PAINT.format(args='x20', band=band, lo=f'{lo:#x}', hi=f'{hi:#x}')
            source = ('.text\n.global _start\n_start:\n' + body +
                      f'  .word {word:#010x}\n' +
                      '  b target\n')
            code = assemble(source, cave_va, work, symbols)
            if len(code) > CAVE_SIZE:
                raise SystemExit(f'probe {name} is too large')
            data[file_offset(cave_va):file_offset(cave_va) + len(code)] = code
            struct.pack_into('<I', data, at, branch(site_va, cave_va))
            print(f'  band {band}: {name} at {site_va:#x}')

    args.kernel.write_bytes(data)
    print(f'patched {args.kernel.name}: {len(sites)} boot checkpoints')


if __name__ == '__main__':
    main()
