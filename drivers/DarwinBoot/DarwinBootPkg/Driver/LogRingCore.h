/* SPDX-License-Identifier: BSD-2-Clause-Patent */
#ifndef DARWINBOOT_LOG_RING_CORE_H
#define DARWINBOOT_LOG_RING_CORE_H

#ifdef LOG_RING_HOST_TEST
#include <stddef.h>
#include <stdint.h>
typedef uint64_t LOG_CORE_U64;
typedef uint32_t LOG_CORE_U32;
typedef size_t   LOG_CORE_SIZE;
#define LOG_CORE_U64_MAX UINT64_MAX
#else
typedef UINT64 LOG_CORE_U64;
typedef UINT32 LOG_CORE_U32;
typedef UINTN   LOG_CORE_SIZE;
#define LOG_CORE_U64_MAX MAX_UINT64
#endif

#define LOG_CORE_HEADER_BYTES 0x40U
#define LOG_CORE_LOC_VERSION  1U

static inline int
LogCorePowerOfTwo (LOG_CORE_U64 Value)
{
  return Value != 0 && (Value & (Value - 1)) == 0;
}

static inline int
LogCoreLocatorValid (
  LOG_CORE_U64 Magic,
  LOG_CORE_U32 Version,
  LOG_CORE_U32 Reserved,
  LOG_CORE_U64 Ring,
  LOG_CORE_U64 Total,
  LOG_CORE_U64 Data,
  LOG_CORE_U64 ExpectedMagic,
  LOG_CORE_U64 MaximumData
  )
{
  return Magic == ExpectedMagic && Version == LOG_CORE_LOC_VERSION && Reserved == 0 &&
         Ring != 0 && (Ring & 7U) == 0 && Data >= 0x1000U && Data <= MaximumData &&
         LogCorePowerOfTwo (Data) && Data <= LOG_CORE_U64_MAX - LOG_CORE_HEADER_BYTES &&
         Total == LOG_CORE_HEADER_BYTES + Data && Ring <= LOG_CORE_U64_MAX - Total;
}

// Convert the cumulative byte counter into the oldest byte and valid length.
static inline int
LogCoreSpan (
  LOG_CORE_U64 Counter,
  LOG_CORE_U64 Capacity,
  LOG_CORE_U64 *Start,
  LOG_CORE_U64 *Length
  )
{
  if (!LogCorePowerOfTwo (Capacity) || Start == NULL || Length == NULL) { return 0; }
  if (Counter > Capacity) {
    *Start = Counter & (Capacity - 1);
    *Length = Capacity;
  } else {
    *Start = 0;
    *Length = Counter;
  }
  return *Length != 0;
}

#endif
