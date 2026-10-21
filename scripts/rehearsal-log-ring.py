#!/usr/bin/env python3
"""QEMU rehearsal for the console ring.

The ring is the only way to see what the kernel printed: the phone has no
console, and the panel is not mapped by the kernel's own page tables yet.  The
kernel appends every console byte to a ring in the tail of its window, and the
next boot reads that ring back and prints it.

This script exercises both halves under QEMU:

  1. boot the payload, find the ring in the guest's memory and read it back,
     which proves the kernel side appends what it should;
  2. inject that ring into the memory of a fresh boot, together with a
     ring.loc file on the ESP, so the loader has to find it, print it and
     write bootlog.txt back to the ESP.

usage: rehearsal-log-ring.py <qmp.sock> [step]
"""
import json
import socket
import subprocess
import sys
import time

SOCK = sys.argv[1]
STEP = sys.argv[2] if len(sys.argv) > 2 else "read"

MAGIC = bytes([0x4C, 0x41, 0x56, 0x4C, 0x4F, 0x47, 0x30, 0x31])   # "LAVLOG01"
RING_TOTAL = 0x80040
RING_DATA = 0x80000


def connect():
    s = socket.socket(socket.AF_UNIX)
    s.connect(SOCK)
    f = s.makefile('rw')
    f.readline()
    return f


def cmd(f, c, **kw):
    f.write(json.dumps({'execute': c, 'arguments': kw}) + '\n')
    f.flush()
    while True:
        r = json.loads(f.readline())
        if 'return' in r or 'error' in r:
            return r


def hmp(f, text):
    r = cmd(f, 'human-monitor-command', **{'command-line': text})
    return r.get('return', '')


def find_ring(f):
    out = hmp(f, 'find 0x40000000 0x40000000 ' + ' '.join('0x%02x' % b for b in MAGIC))
    for line in out.splitlines():
        if line.startswith('0x'):
            return int(line.split(':')[0], 16)
    return None


def dump(f, addr, length):
    out = hmp(f, 'xp /%dxb 0x%x' % (length, addr))
    data = bytearray()
    for line in out.splitlines():
        if ':' not in line:
            continue
        for word in line.split(':')[1].split():
            if word.startswith('0x'):
                data += bytes.fromhex(word[2:])
    return bytes(data)


def main():
    f = connect()
    cmd(f, 'qmp_capabilities')
    if STEP == 'read':
        ring = int(sys.argv[3], 0) if len(sys.argv) > 3 else find_ring(f)
        if ring is None:
            print('no ring found in guest memory')
            return 1
        head = dump(f, ring, 0x40)
        total = int.from_bytes(head[8:16], 'little')
        print('ring at 0x%x, magic %s, total %d, enabled %d' %
              (ring, head[:8].hex(), total, head[16]))
        data = dump(f, ring + 0x40, min(total, RING_DATA))
        text = bytes(data).decode('utf-8', 'replace')
        print('--- ring contents (%d bytes) ---' % len(text))
        print(text)
        return 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
