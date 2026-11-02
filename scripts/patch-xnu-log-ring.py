#!/usr/bin/env python3
"""Give the kernel a console ring that the next boot can harvest if DRAM is retained.

There is no serial console on lavender and the kernel cannot draw after it
enables the MMU (the panel is outside the bootstrap map).  The one channel that
works from the first C instruction is memory that the *next* boot can read:

  * the ring lives inside the kernel image itself, in a hole that this script
    adds to the __DATA segment.  That matters: after arm_vm_init replaces the
    bootstrap page tables, only the kernel image (and the randomised physmap)
    is mapped, so a ring in the window or in ordinary RAM would be unreachable
    exactly when the kernel starts to fail;
  * a cave on the kernel's _cnputs and on console_write_unbuffered appends
    every console byte to it.  The first is the printf path, the second is the
    panic log path, which is where a panic message goes;
  * DarwinBoot attempts to read the ring out of RAM on the next boot, prints
    the tail and keeps the whole log on the ESP if reset/firmware preserved it.

Nothing here changes the kernel's code or the boot arguments.  The displaced
instructions are re-executed inside the caves.

usage: patch-xnu-log-ring.py kernel.macho [--output OUT]
"""
import argparse
import struct
import subprocess
import shutil
import sys
import tempfile
from pathlib import Path

CNPUTS = 0                     # resolved from this kernel's symbol table
CNPUTS_WORD = 0xD10203FF         # sub sp, sp, #128
UNBUFFERED = 0                   # resolved from this kernel's symbol table
UNBUFFERED_WORD = 0xD10103FF     # sub sp, sp, #0x40

# Ring geometry.  Keep in sync with DarwinBoot's LogRing.h.
RING_HEADER = 0x40
RING_MINIMUM = 0x1000             # the smallest ring worth having
RING_MAGIC = 0x3130474F4C56414C   # "LAVLOG01" in memory order

# The hole the ring lives in.  The __DATA segment grows by this much and the
# ring sits at its old end.
RING_HOLE = 0x10000

# The descriptor the loader looks for in the kernel image: it cannot know the
# ring's address, because the kernel is rebuilt far more often than the loader.
DESC_MAGIC = b"LAVLOCRINGDESC1\x00"
DESC_SIZE = 0x30

# Place caves dynamically in remaining __TEXT_EXEC zero/NOP padding.  On
# Darwin 22 the old Darwin 23 absolute addresses fall in __DATA (NX).
CAVE_LOG = 0
CAVE_CHAR = 0
CAVE_DESC = 0
CAVE_LIMIT = 0
CAVE_LOG_SIZE = 0x200
CAVE_CHAR_SIZE = 0x100
CAVE_DESC_SIZE = DESC_SIZE

SEGMENTS = []


def load_segments(data):
    magic, _, _, _, ncmds = struct.unpack_from('<IiiII', data, 0)
    if magic != 0xFEEDFACF:
        raise SystemExit('not a 64-bit Mach-O')
    offset = 32
    SEGMENTS.clear()
    for _ in range(ncmds):
        cmd, size = struct.unpack_from('<II', data, offset)
        if cmd == 0x19:
            name, vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<16sQQQQ', data, offset + 8)
            if filesize:
                SEGMENTS.append((name.rstrip(b'\0').decode(), vmaddr, vmsize, fileoff, filesize))
        offset += size
    if not SEGMENTS:
        raise SystemExit('no file-backed segments')


def file_offset(va):
    for _, vmaddr, vmsize, fileoff, filesize in SEGMENTS:
        if vmaddr <= va < vmaddr + min(vmsize, filesize):
            return fileoff + (va - vmaddr)
    raise SystemExit(f'{va:#x} is not in a file-backed segment')


def branch(from_va, to_va):
    delta = (to_va - from_va) // 4
    if not -0x2000000 <= delta < 0x2000000:
        raise SystemExit(f'branch from {from_va:#x} to {to_va:#x} is out of range')
    return 0x14000000 | (delta & 0x3FFFFFF)


def assemble(source, va, workdir, symbols):
    obj = workdir / 'cave.o'
    elf = workdir / 'cave.elf'
    source_path = workdir / 'cave.s'
    source_path.write_text(source)
    clang = shutil.which('clang') or '/opt/homebrew/opt/llvm/bin/clang'
    lld = shutil.which('ld.lld') or '/opt/homebrew/opt/llvm/bin/ld.lld'
    objcopy = shutil.which('llvm-objcopy') or '/opt/homebrew/opt/llvm/bin/llvm-objcopy'
    subprocess.run([clang, '-target', 'aarch64-none-elf', '-c', str(source_path), '-o', str(obj)], check=True)
    subprocess.run([lld, f'-Ttext={va:#x}', *[f'--defsym={name}={value:#x}' for name, value in symbols.items()],
                    '-e', '_start', str(obj), '-o', str(elf)], check=True)
    binary = workdir / 'cave.bin'
    subprocess.run([objcopy, '-O', 'binary', str(elf), str(binary)], check=True)
    return binary.read_bytes()


def padded_code(data, va, length):
    """True only for full words of zero/NOP padding in a file-backed segment."""
    if length <= 0 or length % 4:
        return False
    at = file_offset(va)
    if at + length > len(data):
        return False
    return all(struct.unpack_from('<I', data, offset)[0] in (0, 0xD503201F)
               for offset in range(at, at + length, 4))


def find_code_cave(data, required):
    """Find the largest remaining executable zero/NOP run after earlier probes."""
    best = None
    for name, vmaddr, vmsize, fileoff, filesize in SEGMENTS:
        if name != '__TEXT_EXEC' or not filesize:
            continue
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
            if length >= required and (best is None or length > best[0]):
                best = (length, vmaddr + start - fileoff)
    if best is None:
        raise SystemExit(f'no executable zero/NOP cave of {required:#x} bytes')
    return best[1], best[0]


def unique_symbol(symbols, name):
    addresses = sorted({address for address, symbol in symbols if symbol == name})
    if len(addresses) != 1:
        raise SystemExit(f'expected one {name} symbol, found {len(addresses)}: {addresses}')
    return addresses[0]


def load_symbols(data):
    """The image's symbol table: (address, name) for everything with an address."""
    ncmds = struct.unpack_from('<I', data, 16)[0]
    offset = 32
    symbols = []
    for _ in range(ncmds):
        cmd, size = struct.unpack_from('<II', data, offset)
        if cmd == 0x2:
            symoff, nsyms, stroff, _ = struct.unpack_from('<IIII', data, offset + 8)
            for index in range(nsyms):
                at = symoff + index * 16
                strx, _, _, _, value = struct.unpack_from('<IBBHQ', data, at)
                end = data.index(b'\0', stroff + strx)
                symbols.append((value, data[stroff + strx:end].decode('utf-8', 'replace')))
        offset += size
    symbols.sort()
    return symbols


def referenced_pages(data, low, high):
    """Pages in [low, high) that some adrp in the image points at."""
    pages = set()
    for name, vmaddr, vmsize, fileoff, filesize in SEGMENTS:
        if 'TEXT' not in name:
            continue
        count = filesize // 4
        words = struct.unpack_from('<%dI' % count, data, fileoff)
        for index, word in enumerate(words):
            if (word & 0x9F000000) != 0x90000000:
                continue
            imm = (word >> 5) & 0x7FFFF
            if imm & 0x40000:
                imm -= 0x80000
            page = ((vmaddr + index * 4) & ~0xFFF) + (imm << 12)
            if low <= page < high:
                pages.add(page)
    return pages


# Symbols whose real extent the (size-less) symbol table does not show: the
# linker put the array itself in the hole that follows them.
KNOWN_ARRAYS = ('_zone_array',)

# The holes are tried in this order.  The tail of the BSS comes first: nothing
# is linked after the last symbol, so it is the one place that is free by
# construction.  The others are holes between two objects, which the kernel
# may well be using.
HOLE_ORDER = ('tail', 'gaps')


def find_hole(data, minimum):
    """A run of zeroed memory in __DATA that nothing refers to.

    Returns (address, data size).  The data size is a power of two, so that
    the ring can wrap with a mask.
    """
    symbols = load_symbols(data)
    data_segment = None
    for segment in SEGMENTS:
        if segment[0] == '__DATA':
            data_segment = segment
            break
    if data_segment is None:
        raise SystemExit('no __DATA segment')
    _, vmaddr, vmsize, fileoff, filesize = data_segment
    bss_start = vmaddr + filesize
    bss_end = vmaddr + vmsize
    inside = [(address, name) for address, name in symbols if bss_start <= address < bss_end]
    gaps = []
    previous = bss_start
    previous_name = None
    for address, name in inside:
        if address - previous > 0 and previous_name not in KNOWN_ARRAYS:
            gaps.append((previous, address, previous_name))
        previous = address
        previous_name = name
    if previous_name not in KNOWN_ARRAYS:
        gaps.append((previous, bss_end, previous_name))
    # The tail first, then the biggest gap, then the rest.
    tail = gaps.pop()
    gaps.sort(key=lambda hole: hole[0] - hole[1])
    ordered = [tail] + gaps
    for start, end, after in ordered:
        room = end - start
        if room < minimum + RING_HEADER + 0x100:
            continue
        # Keep away from the edges: a symbol's real extent is not in the table.
        place = start + 0x100
        place -= place % 0x100
        size = 0x1000
        while size * 2 + RING_HEADER + 0x100 <= room:
            size *= 2
        if size < minimum:
            continue
        pages = referenced_pages(data, place & ~0xFFF, (place + size + RING_HEADER + 0xFFF) & ~0xFFF)
        if pages:
            print(f'hole after {after} is referenced at {sorted(pages)[0]:#x}; skipped')
            continue
        return place, size
    raise SystemExit('no free hole in the kernel image for the console ring')


# Append a run of bytes to the ring.  Runs at the very top of _cnputs, before
# its prologue, so the stack is untouched and x0/x1 are restored for the
# function body.  x8..x17 are caller-saved and free to use here.
LOG_SOURCE = """
.text
.global _start
_start:
  mov  x9, x0                // the string
  mov  w10, w1               // its length
  cmp  w10, #0
  b.le 9f
  ldr  x12, =ring
  ldr  x13, [x12, #8]        // the byte counter
  add  x14, x12, #HEADER
  mov  x16, x9
  mov  x8, x10
1:
  ldrb w11, [x16], #1
  ubfx x17, x13, #0, #BITS
  strb w11, [x14, x17]
  add  x13, x13, #1
  subs x8, x8, #1
  b.ne 1b
  str  x13, [x12, #8]
9:
  mov  x0, x9
  mov  w1, w10
  sub  sp, sp, #128
  b    resume
.ltorg
"""

# The same, for the panic log path, which writes one character at a time.
CHAR_SOURCE = """
.text
.global _start
_start:
  mov  w9, w0
  and  w10, w0, #0xff
  ldr  x12, =ring
  ldr  x13, [x12, #8]        // the byte counter
  add  x14, x12, #HEADER
  ubfx x17, x13, #0, #BITS
  strb w10, [x14, x17]
  add  x13, x13, #1
  str  x13, [x12, #8]
9:
  mov  w0, w9
  sub  sp, sp, #64
  b    resume
.ltorg
"""


def cave_source(template, data_size):
    return (template
            .replace('#BITS', str(data_size.bit_length() - 1))
            .replace('#HEADER', str(RING_HEADER)))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('kernel', type=Path)
    parser.add_argument('-o', '--output', type=Path, help='write here instead of in place')
    args = parser.parse_args()

    global CNPUTS, UNBUFFERED, CAVE_LOG, CAVE_CHAR, CAVE_DESC, CAVE_LIMIT
    data = bytearray(args.kernel.read_bytes())
    load_segments(data)
    symbols = load_symbols(data)
    CNPUTS = unique_symbol(symbols, '__cnputs')
    UNBUFFERED = unique_symbol(symbols, '_console_write_unbuffered')
    CAVE_LOG, CaveBytes = find_code_cave(data, CAVE_LOG_SIZE + CAVE_CHAR_SIZE + CAVE_DESC_SIZE)
    CAVE_CHAR = CAVE_LOG + CAVE_LOG_SIZE
    CAVE_DESC = CAVE_CHAR + CAVE_CHAR_SIZE
    CAVE_LIMIT = CAVE_LOG + CaveBytes
    print(f'console caves: {CAVE_LOG:#x}..{CAVE_LIMIT:#x} in __TEXT_EXEC')

    # The ring has to live in the kernel image: after arm_vm_init replaces the
    # bootstrap page tables, only the image itself (and the randomised
    # physmap) is mapped, so a ring in the window or in ordinary RAM would be
    # unreachable exactly when the kernel starts to fail.
    #
    # The __DATA segment ends in zeroed memory, and the kernel's own globals
    # leave holes in it.  The ring goes into one of those holes, which no code
    # refers to (the check below walks every adrp in the image).
    ring_va, ring_data = find_hole(data, RING_MINIMUM)
    ring_total = RING_HEADER + ring_data

    at = file_offset(CNPUTS)
    if struct.unpack_from('<I', data, at)[0] != CNPUTS_WORD:
        raise SystemExit(f'_cnputs does not start with sub sp, sp, #128 at {CNPUTS:#x}')
    at = file_offset(UNBUFFERED)
    if struct.unpack_from('<I', data, at)[0] != UNBUFFERED_WORD:
        raise SystemExit(f'console_write_unbuffered does not look as expected at {UNBUFFERED:#x}')

    if not padded_code(data, CAVE_LOG, CAVE_LOG_SIZE) or \
            not padded_code(data, CAVE_CHAR, CAVE_CHAR_SIZE) or \
            not padded_code(data, CAVE_DESC, CAVE_DESC_SIZE):
        raise SystemExit('executable caves are not in zero/NOP padding')
    if CAVE_DESC + CAVE_DESC_SIZE > CAVE_LIMIT:
        raise SystemExit('cave layout is out of range')

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        log_code = assemble(cave_source(LOG_SOURCE, ring_data), CAVE_LOG, work,
                            {'ring': ring_va, 'resume': CNPUTS + 4})
        char_code = assemble(cave_source(CHAR_SOURCE, ring_data), CAVE_CHAR, work,
                             {'ring': ring_va, 'resume': UNBUFFERED + 4})
        if len(log_code) > CAVE_LOG_SIZE or len(char_code) > CAVE_CHAR_SIZE:
            raise SystemExit('cave does not fit')

        data[file_offset(CAVE_LOG):file_offset(CAVE_LOG) + len(log_code)] = log_code
        data[file_offset(CAVE_CHAR):file_offset(CAVE_CHAR) + len(char_code)] = char_code
        descriptor = DESC_MAGIC + struct.pack('<QQQQ', ring_va, ring_total, ring_data, 0)
        data[file_offset(CAVE_DESC):file_offset(CAVE_DESC) + len(descriptor)] = descriptor
        struct.pack_into('<I', data, file_offset(CNPUTS), branch(CNPUTS, CAVE_LOG))
        struct.pack_into('<I', data, file_offset(UNBUFFERED), branch(UNBUFFERED, CAVE_CHAR))

    print(f"console ring: {ring_va:#x} ({ring_total} bytes) in a hole in the kernel's __DATA")
    print(f'console ring: _cnputs {CNPUTS:#x} -> {CAVE_LOG:#x} ({len(log_code)} bytes)')
    print(f'console ring: console_write_unbuffered {UNBUFFERED:#x} -> {CAVE_CHAR:#x} ({len(char_code)} bytes)')
    print(f'console ring: descriptor at {CAVE_DESC:#x} for the loader')
    out = args.output or args.kernel
    out.write_bytes(data)
    print(f'patched {out}')


if __name__ == '__main__':
    main()
