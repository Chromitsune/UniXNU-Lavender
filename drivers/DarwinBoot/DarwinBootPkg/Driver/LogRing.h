// SPDX-License-Identifier: BSD-2-Clause-Patent
#ifndef DARWINBOOT_LOG_RING_H
#define DARWINBOOT_LOG_RING_H

//
// The kernel has no console on lavender: the panel is outside its bootstrap
// map, so once the MMU is on it can neither draw nor speak to anything.  The
// one channel that survives is memory the next boot can read back.
//
// The ring is a hole in the kernel's own __DATA segment, so it stays mapped
// across arm_vm_init.  A cave on the kernel's _cnputs and on
// console_write_unbuffered appends every console byte to it: the first is the
// printf path, the second is the panic log path, which is where a panic
// message goes.
//
// The next boot attempts to copy the ring out of RAM and keep it on the ESP:
//
//   \EFI\UniXNU\ring.loc     where the ring was (the kernel window moves, so
//                            the address cannot be assumed)
//   \EFI\UniXNU\bootlog.txt  the log itself, for reading it off the phone
//
// The kernel patch (scripts/patch-xnu-log-ring.py) writes a descriptor with
// the ring's address into the kernel image; this is how the loader finds it.
//

#define LOG_RING_MAGIC    0x3130474F4C56414CULL   // "LAVLOG01" as stored
#define LOG_RING_HEADER   0x40ULL                 // magic, cumulative byte counter, reserved

// The descriptor in the kernel image.
#define LOG_DESC_MAGIC    "LAVLOCRINGDESC1"
#define LOG_DESC_SIZE     48

typedef struct {
  CHAR8   Magic[16];
  UINT64  Ring;        // the kernel's virtual address of the ring
  UINT64  Total;       // header + data
  UINT64  Data;        // the data size, a power of two
  UINT64  Reserved;
} LOG_RING_DESC;

// The location file: where the previous boot's ring was.
#define LOG_LOC_MAGIC     0x314C4F56414C414CULL   // "LAVLOC01" as stored
#define LOG_LOC_VERSION   1

// How much of the log to reprint on the console.  The bands the kernel paints
// cover the top ~25% of the screen, so the tail has to be short enough to stay
// below them.
#define LOG_TAIL          1200

typedef struct {
  UINT64  Magic;
  UINT32  Version;
  UINT32  Reserved;
  UINT64  Ring;        // physical address of the ring that was live last boot
  UINT64  Total;       // header + data
  UINT64  Data;        // the data size, a power of two
} LOG_LOC_HEADER;

#endif
