#!/usr/bin/env python3
"""Give the kernel a console ring that survives until the next boot.

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
  * DarwinBoot reads the ring out of RAM on the next boot, prints the tail and
    keeps the whole log on the ESP.

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

CNPUTS = 0xFFFFFFF0073B6B30       # __cnputs(char *c, int size, bool poll)
CNPUTS_WORD = 0xD10203FF          # sub sp, sp, #128
UNBUFFERED = 0xFFFFFFF0073B6C60   # console_write_unbuffered(char c)
UNBUFFERED_WORD = 0xD10103FF      # sub sp, sp, #0x40

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

# Caves live in the zero padding after the entry point.  The boot ladder uses
# [0x7a1de30, 0x7a1e230); the panic screen is at 0x7a1e610.
CAVE_LOG = 0xFFFFFFF007A1E800
CAVE_CHAR = 0xFFFFFFF007A1EA00
CAVE_DESC = 0xFFFFFFF007A1EC00
CAVE_LIMIT = 0xFFFFFFF007A20000

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


def padded_zero(data, va, length):
    at = file_offset(va)
    return all(byte == 0 for byte in data[at:at + length])


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

    data = bytearray(args.kernel.read_bytes())
    load_segments(data)

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

    if not padded_zero(data, CAVE_LOG, 0x100) or not padded_zero(data, CAVE_CHAR, 0x80) \
            or not padded_zero(data, CAVE_DESC, DESC_SIZE):
        raise SystemExit('caves are not in zero padding')
    if CAVE_DESC + DESC_SIZE > CAVE_LIMIT:
        raise SystemExit('cave layout is out of range')

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        log_code = assemble(cave_source(LOG_SOURCE, ring_data), CAVE_LOG, work,
                            {'ring': ring_va, 'resume': CNPUTS + 4})
        char_code = assemble(cave_source(CHAR_SOURCE, ring_data), CAVE_CHAR, work,
                             {'ring': ring_va, 'resume': UNBUFFERED + 4})
        if len(log_code) > 0x200 or len(char_code) > 0x100:
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
