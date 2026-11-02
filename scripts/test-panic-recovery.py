#!/usr/bin/env python3
"""Host tests for panic-ring geometry, recovery BCB encoding, and optional kernel patching.

No device is opened. With --kernel, only a temporary copy of that Mach-O is modified.
"""
import argparse
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--kernel', type=Path, help='optional current kernel.macho to patch in a temporary directory')
args = p.parse_args()

cc = shutil.which('clang') or shutil.which('cc')
if not cc:
    raise SystemExit('clang/cc is required for host contract tests')

fixture = r'''
#include <assert.h>
#include <stdint.h>
#include <string.h>
#define LOG_RING_HOST_TEST 1
#include "RecoveryBcb.h"
#include "LogRingCore.h"
int main(void) {
  unsigned char b[ANDROID_BCB_BYTES + 16], before[sizeof b];
  memset(b, 0xA5, sizeof b); memcpy(before, b, sizeof b);
  assert(!BuildOrangeFoxRecoveryBcb(b, ANDROID_BCB_BYTES - 1));
  assert(!memcmp(b, before, sizeof b));
  assert(BuildOrangeFoxRecoveryBcb(b, ANDROID_BCB_BYTES));
  const ANDROID_BOOTLOADER_MESSAGE *m = (const ANDROID_BOOTLOADER_MESSAGE *)b;
  assert(!strcmp((const char *)m->Command, "boot-recovery"));
  assert(!strcmp((const char *)m->Recovery, "recovery\n--show_text\n"));
  for (size_t i=0;i<sizeof(m->Reserved);i++) assert(m->Reserved[i]==0xA5);
  for (size_t i=ANDROID_BCB_BYTES;i<sizeof b;i++) assert(b[i]==0xA5);

  const uint64_t magic=UINT64_C(0x314C4F56414C414C), ring=UINT64_C(0x80010000);
  const uint64_t data=UINT64_C(65536), total=data+LOG_CORE_HEADER_BYTES;
  assert(LogCoreLocatorValid(magic,1,0,ring,total,data,magic,SIZE_MAX));
  assert(!LogCoreLocatorValid(magic,2,0,ring,total,data,magic,SIZE_MAX));
  assert(!LogCoreLocatorValid(magic,1,1,ring,total,data,magic,SIZE_MAX));
  assert(!LogCoreLocatorValid(magic,1,0,ring+1,total,data,magic,SIZE_MAX));
  assert(!LogCoreLocatorValid(magic,1,0,ring,total,32768,magic,SIZE_MAX));
  assert(!LogCoreLocatorValid(magic,1,0,ring,UINT64_MAX,UINT64_MAX,magic,SIZE_MAX));
  uint64_t start=99,length=99;
  assert(!LogCoreSpan(0,data,&start,&length));
  assert(LogCoreSpan(3,data,&start,&length) && start==0 && length==3);
  assert(LogCoreSpan(data,data,&start,&length) && start==0 && length==data);
  assert(LogCoreSpan(data+1,data,&start,&length) && start==1 && length==data);
  assert(LogCoreSpan(data+5,data,&start,&length) && start==5 && length==data);
  assert(!LogCoreSpan(4,3,&start,&length));
  return 0;
}
'''
with tempfile.TemporaryDirectory(prefix='lavender-panic-test-') as tmp:
    tmp = Path(tmp)
    source = tmp / 'contract.c'
    binary = tmp / 'contract'
    source.write_text(fixture)
    subprocess.run([cc, '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-I', str(ROOT / 'drivers/DarwinBoot/DarwinBootPkg/Driver'),
                    str(source), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
print('PASS: BCB layout/preservation and ring locator/span contracts')

if args.kernel:
    kernel = args.kernel.resolve()
    if not kernel.is_file():
        raise SystemExit(f'kernel not found: {kernel}')
    patched = None
    with tempfile.TemporaryDirectory(prefix='lavender-panic-kernel-') as tmp:
        patched = Path(tmp) / 'kernel.macho'
        shutil.copyfile(kernel, patched)
        env = dict(os.environ)
        env.setdefault('UNIXNU_UEFI', str(ROOT / 'uefi-sheng'))
        subprocess.run(['python3', ROOT / 'scripts/patch-xnu-log-ring.py', patched],
                       cwd=ROOT, env=env, check=True)
        subprocess.run(['python3', ROOT / 'scripts/patch-xnu-entry-probe.py', patched,
                        '--panic-screen', '--panic-screen-only'], cwd=ROOT, env=env, check=True)
        data = patched.read_bytes()
        assert data.count(b'LAVLOCRINGDESC1\0') == 1
        assert data.count(b'LAVPANICRECOVERY1') == 1
        at = data.index(b'LAVLOCRINGDESC1\0') + 16
        ring, total, capacity, reserved = struct.unpack_from('<QQQQ', data, at)
        assert ring and capacity == 65536 and total == 64 + capacity and reserved == 0
    print('PASS: current kernel copy instruments console ring and panic recovery/reset code; no device access')
else:
    print('SKIP: kernel-patch integration (pass --kernel <kernel.macho>)')
