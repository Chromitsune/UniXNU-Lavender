/* SPDX-License-Identifier: BSD-2-Clause-Patent */
#ifndef DARWINBOOT_RECOVERY_BCB_H
#define DARWINBOOT_RECOVERY_BCB_H

#include <stddef.h>

#define ANDROID_BCB_BYTES 2048

typedef struct {
  unsigned char Command[32];
  unsigned char Status[32];
  unsigned char Recovery[768];
  unsigned char Stage[32];
  unsigned char Reserved[1184];
} ANDROID_BOOTLOADER_MESSAGE;

typedef char ANDROID_BCB_SIZE_MUST_BE_2048[(sizeof (ANDROID_BOOTLOADER_MESSAGE) == ANDROID_BCB_BYTES) ? 1 : -1];

/* Prepare the standard Android bootloader_message in a read/modify/write buffer.
 * Preserve Reserved so vendor fields outside the command are not clobbered. */
static inline int
BuildOrangeFoxRecoveryBcb (void *Buffer, size_t BufferSize)
{
  ANDROID_BOOTLOADER_MESSAGE *Message = (ANDROID_BOOTLOADER_MESSAGE *)Buffer;
  static const unsigned char Command[] = "boot-recovery";
  static const unsigned char Recovery[] = "recovery\n--show_text\n";
  size_t Index;

  if (Buffer == NULL || BufferSize < sizeof (*Message)) {
    return 0;
  }
  for (Index = 0; Index < sizeof (Message->Command); Index++) { Message->Command[Index] = 0; }
  for (Index = 0; Index < sizeof (Message->Status); Index++) { Message->Status[Index] = 0; }
  for (Index = 0; Index < sizeof (Message->Recovery); Index++) { Message->Recovery[Index] = 0; }
  for (Index = 0; Index < sizeof (Message->Stage); Index++) { Message->Stage[Index] = 0; }
  for (Index = 0; Index < sizeof (Command); Index++) { Message->Command[Index] = Command[Index]; }
  for (Index = 0; Index < sizeof (Recovery); Index++) { Message->Recovery[Index] = Recovery[Index]; }
  return 1;
}

#endif
