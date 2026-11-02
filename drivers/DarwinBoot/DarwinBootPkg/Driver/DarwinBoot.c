// SPDX-License-Identifier: BSD-2-Clause-Patent
#include <Uefi.h>
#include <Guid/FileInfo.h>
#include <Library/BaseLib.h>
#include <Library/BaseMemoryLib.h>
#include <Library/DebugLib.h>
#include <Library/MemoryAllocationLib.h>
#include <Library/UefiBootServicesTableLib.h>
#include <Library/PrintLib.h>
#include <Library/UefiLib.h>
#include <Protocol/UniXNULoader.h>
#include <Protocol/LoadedImage.h>
#include <Protocol/GraphicsOutput.h>
#include <Protocol/SimpleFileSystem.h>
#include "BootArgs.h"
#include "BootVideo.h"
#include "LogRing.h"
#include "PackedRamdisk.h"
#include "WindowSize.h"

#define WINDOW_SIZE  SIZE_256MB
#define BLOCK_SIZE   SIZE_2MB
#define MAX_SEGMENTS 64

typedef struct {
  UINT64  Address;
  UINT64  Size;
  UINT64  FileOffset;
  UINT64  FileSize;
  UINT32  Protection;
} SEGMENT;

typedef struct {
  SEGMENT  Segments[MAX_SEGMENTS];
  UINTN    Count;
  UINT64   Base;
  UINT64   End;
  UINT64   Entry;
} KERNEL;

STATIC EFI_HANDLE  mImage;
UINT64 EFIAPI DarwinCurrentEl (VOID);
VOID EFIAPI DarwinEnter (UINT64 Args, UINT64 Entry, UINT64 Memory, UINT64 Size, UINT64 VideoBase, UINT64 VideoBytes);

#define READ_SYSREG(Name)  ({ UINT64 Value_; __asm__ volatile ("mrs %0, " #Name : "=r" (Value_)); Value_; })

STATIC UINT32 U32 (CONST VOID *P) { return ReadUnaligned32 ((CONST UINT32 *)P); }
STATIC UINT64 U64 (CONST VOID *P) { return ReadUnaligned64 ((CONST UINT64 *)P); }

STATIC
EFI_STATUS
ReadBootVideo (UINT64 Video[6])
{
  EFI_GRAPHICS_OUTPUT_PROTOCOL           *Gop;
  EFI_STATUS                            Status;

  ZeroMem (Video, 6 * sizeof (*Video));
  Status = gBS->HandleProtocol (gST->ConsoleOutHandle, &gEfiGraphicsOutputProtocolGuid, (VOID **)&Gop);
  if (EFI_ERROR (Status)) {
    Status = gBS->LocateProtocol (&gEfiGraphicsOutputProtocolGuid, NULL, (VOID **)&Gop);
  }
  if (Status == EFI_NOT_FOUND) { return EFI_SUCCESS; }
  if (EFI_ERROR (Status)) { return Status; }
  return DarwinBootVideoFromGop (Gop, Video);
}

STATIC
EFI_STATUS
ReadFile (
  EFI_FILE_PROTOCOL *Root,
  CONST CHAR16      *Path,
  UINTN             Limit,
  VOID              **Data,
  UINTN             *Size
  )
{
  EFI_STATUS         Status;
  EFI_FILE_PROTOCOL  *File;
  EFI_FILE_INFO      *Info;
  UINTN              InfoSize;
  UINTN              Actual;

  *Data = NULL;
  *Size = 0;
  Status = Root->Open (Root, &File, (CHAR16 *)Path, EFI_FILE_MODE_READ, 0);
  if (EFI_ERROR (Status)) { return Status; }
  Info = NULL;
  InfoSize = 0;
  Status = File->GetInfo (File, &gEfiFileInfoGuid, &InfoSize, NULL);
  if (Status != EFI_BUFFER_TOO_SMALL) { goto Done; }
  Info = AllocatePool (InfoSize);
  if (Info == NULL) { Status = EFI_OUT_OF_RESOURCES; goto Done; }
  Status = File->GetInfo (File, &gEfiFileInfoGuid, &InfoSize, Info);
  if (EFI_ERROR (Status)) { goto Done; }
  if ((Info->Attribute & EFI_FILE_DIRECTORY) || (Info->FileSize == 0) || (Info->FileSize > Limit)) {
    Status = EFI_BAD_BUFFER_SIZE;
    goto Done;
  }
  *Size = (UINTN)Info->FileSize;
  *Data = AllocatePool (*Size);
  if (*Data == NULL) { Status = EFI_OUT_OF_RESOURCES; goto Done; }
  Actual = *Size;
  Status = File->Read (File, &Actual, *Data);
  if (!EFI_ERROR (Status) && (Actual != *Size)) { Status = EFI_DEVICE_ERROR; }
Done:
  if (Info != NULL) { FreePool (Info); }
  File->Close (File);
  if (EFI_ERROR (Status) && (*Data != NULL)) { FreePool (*Data); *Data = NULL; }
  return Status;
}

STATIC
EFI_STATUS
ParseKernel (CONST UINT8 *Data, UINTN Size, KERNEL *Kernel)
{
  UINTN    Offset;
  UINTN    End;
  UINTN    Index;
  UINTN    Other;
  UINT32   Count;
  UINT32   Command;
  UINT32   Length;
  SEGMENT  *Segment;
  BOOLEAN  HasEntry;
  BOOLEAN  Executable;

  if ((Size < 32) || (U32 (Data) != 0xfeedfacf) || (U32 (Data + 4) != 0x100000c) ||
      (U32 (Data + 8) != 0) || (U32 (Data + 12) != 2)) {
    return EFI_UNSUPPORTED;
  }
  if (U32 (Data + 20) > Size - 32) { return EFI_LOAD_ERROR; }
  ZeroMem (Kernel, sizeof (*Kernel));
  Count = U32 (Data + 16);
  End = 32 + U32 (Data + 20);
  Offset = 32;
  HasEntry = FALSE;
  for (Index = 0; Index < Count; Index++) {
    if (End - Offset < 8) { return EFI_LOAD_ERROR; }
    Command = U32 (Data + Offset);
    Length = U32 (Data + Offset + 4);
    if ((Length < 8) || (Length & 7) || (Length > End - Offset)) { return EFI_LOAD_ERROR; }
    if (Command == 0x19) {
      if ((Length < 72) || (U32 (Data + Offset + 64) > (Length - 72) / 80) ||
          (Kernel->Count == MAX_SEGMENTS)) { return EFI_LOAD_ERROR; }
      if (U64 (Data + Offset + 32) != 0) {
        Segment = &Kernel->Segments[Kernel->Count++];
        Segment->Address = U64 (Data + Offset + 24);
        Segment->Size = U64 (Data + Offset + 32);
        Segment->FileOffset = U64 (Data + Offset + 40);
        Segment->FileSize = U64 (Data + Offset + 48);
        Segment->Protection = U32 (Data + Offset + 60);
        if ((Segment->Size > WINDOW_SIZE) || (Segment->Address > MAX_UINT64 - Segment->Size) ||
            (Segment->FileSize > Segment->Size) || (Segment->FileOffset > Size) ||
            (Segment->FileSize > Size - Segment->FileOffset)) { return EFI_LOAD_ERROR; }
      }
    } else if (Command == 5) {
      if (HasEntry || (Length != 288) || (U32 (Data + Offset + 8) != 6) ||
          (U32 (Data + Offset + 12) != 68)) { return EFI_LOAD_ERROR; }
      Kernel->Entry = U64 (Data + Offset + 272);
      HasEntry = TRUE;
    }
    Offset += Length;
  }
  if ((Offset != End) || !HasEntry || (Kernel->Count == 0)) { return EFI_LOAD_ERROR; }
  Kernel->Base = MAX_UINT64;
  Executable = FALSE;
  for (Index = 0; Index < Kernel->Count; Index++) {
    Segment = &Kernel->Segments[Index];
    Kernel->Base = MIN (Kernel->Base, Segment->Address);
    Kernel->End = MAX (Kernel->End, Segment->Address + Segment->Size);
    if ((Kernel->Entry >= Segment->Address) && (Kernel->Entry - Segment->Address < Segment->FileSize) &&
        ((Segment->Protection & 4) != 0)) { Executable = TRUE; }
    for (Other = 0; Other < Index; Other++) {
      if ((Segment->Address < Kernel->Segments[Other].Address + Kernel->Segments[Other].Size) &&
          (Kernel->Segments[Other].Address < Segment->Address + Segment->Size)) { return EFI_LOAD_ERROR; }
    }
  }
  if (!Executable || (Kernel->Entry & 3) || (Kernel->Base & EFI_PAGE_MASK) ||
      (Kernel->Base > MAX_UINT64 - WINDOW_SIZE) ||
      (Kernel->End - Kernel->Base > WINDOW_SIZE - SIZE_2MB)) { return EFI_LOAD_ERROR; }
  return EFI_SUCCESS;
}

STATIC
BOOLEAN
AdtNode (CONST UINT8 *Data, UINTN Size, UINTN *Offset, UINTN Depth, UINTN ParentPath, UINTN *RamdiskOffset, UINTN *TrustCacheOffset, UINT64 *WindowSize, BOOLEAN *KeepPacked)
{
  UINT32 Properties;
  UINT32 Children;
  UINT32 Length;
  UINTN  Index;
  UINTN  Path;
  UINTN  Candidate;
  UINTN  TrustCandidate;
  UINTN  MemoryCandidate;
  CONST UINT8 *Name;
  UINTN  NameLength;

  Name = NULL;
  NameLength = 0;
  Candidate = MAX_UINTN;
  TrustCandidate = MAX_UINTN;
  MemoryCandidate = MAX_UINTN;
  if ((Depth > 64) || (Size - *Offset < 8)) { return FALSE; }
  Properties = U32 (Data + *Offset);
  Children = U32 (Data + *Offset + 4);
  *Offset += 8;
  if (Properties > (Size - *Offset) / 36) { return FALSE; }
  for (Index = 0; Index < Properties; Index++) {
    if (Size - *Offset < 36) { return FALSE; }
    if (ScanMem8 (Data + *Offset, 32, 0) == NULL) { return FALSE; }
    Length = U32 (Data + *Offset + 32);
    *Offset += 36;
    if (Length > Size - *Offset) { return FALSE; }
    if (AsciiStrCmp ((CONST CHAR8 *)(Data + *Offset - 36), "name") == 0) {
      if (Name != NULL) { return FALSE; }
      Name = Data + *Offset;
      NameLength = Length;
    }
    if ((AsciiStrCmp ((CONST CHAR8 *)(Data + *Offset - 36), "RAMDisk") == 0) ||
        (AsciiStrCmp ((CONST CHAR8 *)(Data + *Offset - 36), "PackedRAMDisk") == 0)) {
      if (AsciiStrCmp ((CONST CHAR8 *)(Data + *Offset - 36), "PackedRAMDisk") == 0) {
        if (*KeepPacked) { return FALSE; }
        *KeepPacked = TRUE;
      }
      if ((Candidate != MAX_UINTN) || (Length != 16)) { return FALSE; }
      Candidate = *Offset;
    }
    if (AsciiStrCmp ((CONST CHAR8 *)(Data + *Offset - 36), "TrustCache") == 0) {
      if ((TrustCandidate != MAX_UINTN) || (Length != 16)) { return FALSE; }
      TrustCandidate = *Offset;
    }
    if (AsciiStrCmp ((CONST CHAR8 *)(Data + *Offset - 36), "darwinboot-memory-size") == 0) {
      if ((MemoryCandidate != MAX_UINTN) || (Length != 8)) { return FALSE; }
      MemoryCandidate = *Offset;
    }
    *Offset += Length;
    if (ALIGN_VALUE (*Offset, 4) > Size) { return FALSE; }
    *Offset = ALIGN_VALUE (*Offset, 4);
  }
  Path = 3;
  if (Depth == 0) { Path = 0; }
  if ((Depth == 1) && (ParentPath == 0) && (NameLength == 7) && (CompareMem (Name, "chosen", 7) == 0)) { Path = 1; }
  if ((Depth == 2) && (ParentPath == 1) && (NameLength == 11) && (CompareMem (Name, "memory-map", 11) == 0)) { Path = 2; }
  if ((Path == 2) && (Candidate != MAX_UINTN)) {
    if (*RamdiskOffset != MAX_UINTN) { return FALSE; }
    *RamdiskOffset = Candidate;
  }
  if ((Path == 2) && (TrustCandidate != MAX_UINTN)) {
    if (*TrustCacheOffset != MAX_UINTN) { return FALSE; }
    *TrustCacheOffset = TrustCandidate;
  }
  if (MemoryCandidate != MAX_UINTN) {
    if ((Path != 1) || (*WindowSize != 0)) { return FALSE; }
    *WindowSize = U64 (Data + MemoryCandidate);
    if (!DarwinWindowSizeValid (*WindowSize)) { return FALSE; }
  }
  if (Children > (Size - *Offset) / 8) { return FALSE; }
  for (Index = 0; Index < Children; Index++) {
    if (!AdtNode (Data, Size, Offset, Depth + 1, Path, RamdiskOffset, TrustCacheOffset, WindowSize, KeepPacked)) { return FALSE; }
  }
  return TRUE;
}

STATIC
BOOLEAN
ValidTrustCaches (CONST UINT8 *Data, UINTN Size)
{
  UINTN Count;
  UINTN Index;
  UINTN Start;
  UINTN End;
  UINTN Entries;
  UINTN Entry;

  if (Size < 8) { return FALSE; }
  Count = U32 (Data);
  if ((Count == 0) || (Count > 128) || (Count > (Size - 4) / 4)) { return FALSE; }
  for (Index = 0; Index < Count; Index++) {
    Start = U32 (Data + 4 + Index * 4);
    End = (Index + 1 < Count) ? U32 (Data + 8 + Index * 4) : Size;
    if ((Start < 4 + Count * 4) || (Start & 3) || (End > Size) ||
        (End < Start) || (End - Start < 24) || (U32 (Data + Start) != 2)) { return FALSE; }
    Entries = U32 (Data + Start + 20);
    if ((Entries > (End - Start - 24) / 24) || (End - Start != 24 + Entries * 24)) { return FALSE; }
    for (Entry = 0; Entry < Entries; Entry++) {
      CONST UINT8 *Hash = Data + Start + 24 + Entry * 24;
      if ((Hash[20] < 1) || (Hash[20] > 4) ||
          ((Entry != 0) && (CompareMem (Hash - 24, Hash, 20) >= 0))) { return FALSE; }
    }
  }
  return TRUE;
}

STATIC
EFI_STATUS
GetMap (EFI_MEMORY_DESCRIPTOR **Map, UINTN *Capacity, UINTN *Size, UINTN *Key, UINTN *Stride)
{
  EFI_STATUS Status;
  UINT32     Version;
  UINTN      Attempt;

  for (Attempt = 0; Attempt < 3; Attempt++) {
    *Size = *Capacity;
    Status = gBS->GetMemoryMap (Size, *Map, Key, Stride, &Version);
    if (Status != EFI_BUFFER_TOO_SMALL) { return Status; }
    if (*Map != NULL) { FreePool (*Map); }
    *Capacity = *Size + 32 * sizeof (EFI_MEMORY_DESCRIPTOR);
    *Map = AllocatePool (*Capacity);
    if (*Map == NULL) { return EFI_OUT_OF_RESOURCES; }
  }
  return EFI_OUT_OF_RESOURCES;
}

// End of the run of EfiConventionalMemory that begins at Start: the UEFI map often lists physically
// adjacent free ranges as separate descriptors, so extend over every descriptor that continues the run.
STATIC
UINT64
RunEnd (EFI_MEMORY_DESCRIPTOR *Map, UINTN Size, UINTN Stride, UINT64 Start, UINT64 End)
{
  EFI_MEMORY_DESCRIPTOR *Descriptor;
  UINTN                 Offset;
  BOOLEAN               Extended;

  do {
    Extended = FALSE;
    for (Offset = 0; Offset + Stride <= Size; Offset += Stride) {
      Descriptor = (EFI_MEMORY_DESCRIPTOR *)((UINT8 *)Map + Offset);
      if ((Descriptor->Type == EfiConventionalMemory) && (Descriptor->PhysicalStart == End) &&
          (Descriptor->NumberOfPages != 0) && (Descriptor->NumberOfPages < (MAX_UINT64 - End) / EFI_PAGE_SIZE)) {
        End += EFI_PAGES_TO_SIZE (Descriptor->NumberOfPages);
        Extended = TRUE;
      }
    }
  } while (Extended);
  (VOID)Start;
  return End;
}

//
// Console ring.  See LogRing.h for the contract with the kernel patch.
//
// The ring is never copied whole: it is read straight out of RAM while the
// previous boot's memory is still intact, and only the tail that goes on the
// screen is kept in the driver.
//
STATIC CHAR8   mLogTail[LOG_TAIL + 1];
STATIC UINTN   mLogLength;      // bytes of ring data in use
STATIC UINTN   mLogStart;       // ring offset of the oldest byte
STATIC UINT64  mLogData;        // the ring's data size, a power of two
STATIC BOOLEAN mLogPresent;
STATIC BOOLEAN mLogLocatorPresent;
STATIC CONST CHAR8 *mLogReadReason;
STATIC EFI_STATUS mLogArchiveStatus;
STATIC UINTN mLogArchiveBytes;
STATIC EFI_STATUS mLogLocationStatus;
STATIC LOG_RING_DESC mRingDesc;

// Find the ring that the kernel patch put in the image.  The loader cannot
// know where it is: the kernel is rebuilt far more often than the loader is.
STATIC
BOOLEAN
LogFindRing (
  CONST UINT8    *Image,
  UINTN          Size,
  LOG_RING_DESC  *Desc
  )
{
  UINTN  Index;

  if (Size < sizeof (LOG_RING_DESC)) { return FALSE; }
  for (Index = 0; Index + sizeof (LOG_RING_DESC) <= Size; Index++) {
    if (Image[Index] != LOG_DESC_MAGIC[0]) { continue; }
    if (CompareMem (Image + Index, LOG_DESC_MAGIC, sizeof (Desc->Magic)) != 0) { continue; }
    CopyMem (Desc, Image + Index, sizeof (LOG_RING_DESC));
    if ((Desc->Total <= LOG_RING_HEADER) || (Desc->Data < 0x1000) ||
        (Desc->Data != Desc->Total - LOG_RING_HEADER) ||
        ((Desc->Data & (Desc->Data - 1)) != 0) ||
        (Desc->Ring > MAX_UINT64 - Desc->Total)) { continue; }
    return TRUE;
  }
  return FALSE;
}

// Is [Start, End) inside one descriptor the firmware will let us read?  A
// corrupt location file must not send us into a hole or a device window.
// The map is read into a stack buffer on purpose: this runs before the loader
// allocates anything, so that the ring is copied out of RAM before a pool
// allocation can land on it.
STATIC
BOOLEAN
LogMapped (
  UINT64  Start,
  UINT64  End
  )
{
  EFI_MEMORY_DESCRIPTOR  *Descriptor;
  EFI_MEMORY_DESCRIPTOR  MapBuffer[96];
  UINTN                   MapSize;
  UINTN                   MapKey;
  UINTN                   MapStride;
  UINT32                  MapVersion;
  EFI_STATUS              Status;
  UINTN                   Offset;
  UINT64                  DescriptorEnd;
  BOOLEAN                 Mapped;

  if ((Start == 0) || (End <= Start)) { return FALSE; }
  MapSize = sizeof (MapBuffer);
  Status = gBS->GetMemoryMap (&MapSize, MapBuffer, &MapKey, &MapStride, &MapVersion);
  if (EFI_ERROR (Status) || (MapStride < sizeof (EFI_MEMORY_DESCRIPTOR)) || (MapStride == 0)) { return FALSE; }
  Mapped = FALSE;
  for (Offset = 0; Offset + MapStride <= MapSize; Offset += MapStride) {
    Descriptor = (EFI_MEMORY_DESCRIPTOR *)((UINT8 *)MapBuffer + Offset);
    if ((Descriptor->NumberOfPages == 0) ||
        (Descriptor->NumberOfPages > (MAX_UINT64 - Descriptor->PhysicalStart) / EFI_PAGE_SIZE)) { continue; }
    DescriptorEnd = Descriptor->PhysicalStart + EFI_PAGES_TO_SIZE (Descriptor->NumberOfPages);
    if (((Descriptor->Type == EfiConventionalMemory) || (Descriptor->Type == EfiLoaderCode) ||
         (Descriptor->Type == EfiLoaderData) || (Descriptor->Type == EfiBootServicesCode) ||
         (Descriptor->Type == EfiBootServicesData) || (Descriptor->Type == EfiRuntimeServicesCode) ||
         (Descriptor->Type == EfiRuntimeServicesData) || (Descriptor->Type == EfiACPIReclaimMemory)) &&
        (Descriptor->PhysicalStart <= Start) && (End <= DescriptorEnd)) {
      Mapped = TRUE;
      break;
    }
  }
  return Mapped;
}

STATIC
UINT8
LogByte (
  UINT64  Ring,
  UINTN   Index
  )
{
  return *(UINT8 *)(UINTN)(Ring + LOG_RING_HEADER + ((mLogStart + Index) & (mLogData - 1)));
}

// Put the recovered log on the ESP, so it can be read off the phone without
// booting it.  The ring is circular, so the text is two runs.
STATIC
EFI_STATUS
LogWriteArchive (
  EFI_FILE_PROTOCOL  *Root,
  UINT64             Ring,
  UINTN              *BytesWritten
  )
{
  EFI_FILE_PROTOCOL  *File;
  EFI_STATUS         Status;
  UINT8              InfoBuffer[sizeof (EFI_FILE_INFO) + 64];
  EFI_FILE_INFO      *Info;
  UINTN              InfoSize;
  UINTN              Length;
  UINTN              First;
  UINTN              Expected;

  *BytesWritten = 0;
  File = NULL;
  Status = Root->Open (Root, &File, L"\\EFI\\UniXNU\\bootlog.txt",
                       EFI_FILE_MODE_READ | EFI_FILE_MODE_WRITE | EFI_FILE_MODE_CREATE, 0);
  if (EFI_ERROR (Status)) { return Status; }
  // CREATE does not imply truncation for an existing UEFI file. Shrink it
  // first, or a shorter new log would be followed by stale bytes from last time.
  Info = (EFI_FILE_INFO *)InfoBuffer;
  ZeroMem (InfoBuffer, sizeof (InfoBuffer));
  InfoSize = sizeof (InfoBuffer);
  Status = File->GetInfo (File, &gEfiFileInfoGuid, &InfoSize, Info);
  if (EFI_ERROR (Status) || (Info->Size < sizeof (EFI_FILE_INFO)) || (Info->Size > InfoSize)) {
    if (!EFI_ERROR (Status)) { Status = EFI_COMPROMISED_DATA; }
    File->Close (File);
    return Status;
  }
  Info->FileSize = 0;
  Status = File->SetInfo (File, &gEfiFileInfoGuid, Info->Size, Info);
  if (EFI_ERROR (Status)) { File->Close (File); return Status; }
  Status = File->SetPosition (File, 0);
  if (EFI_ERROR (Status)) { File->Close (File); return Status; }

  First = (UINTN)(mLogData - mLogStart);
  if (First > mLogLength) { First = mLogLength; }
  Expected = First;
  Length = Expected;
  Status = File->Write (File, &Length, (VOID *)(UINTN)(Ring + LOG_RING_HEADER + mLogStart));
  if (EFI_ERROR (Status) || (Length != Expected)) {
    if (!EFI_ERROR (Status)) { Status = EFI_DEVICE_ERROR; }
    File->Close (File);
    return Status;
  }
  *BytesWritten = Length;

  if (mLogLength > First) {
    Expected = mLogLength - First;
    Length = Expected;
    Status = File->Write (File, &Length, (VOID *)(UINTN)(Ring + LOG_RING_HEADER));
    if (EFI_ERROR (Status) || (Length != Expected)) {
      if (!EFI_ERROR (Status)) { Status = EFI_DEVICE_ERROR; }
      File->Close (File);
      return Status;
    }
    *BytesWritten += Length;
  }

  Status = File->Flush (File);
  if (EFI_ERROR (Status)) { File->Close (File); return Status; }
  return File->Close (File);
}

// Copy the log out of the previous boot's ring and leave it on the ESP.
//
// Only the physical address comes from a file; the bytes come from memory, so
// this works even when the last boot died before it could write anything.
//
// Nothing here allocates: the ring is read before the loader asks for the
// 50 MiB kernel image, because a pool allocation landing on the ring would
// lose the log.
STATIC
VOID
LogReadPrevious (
  EFI_FILE_PROTOCOL  *Root
  )
{
  EFI_FILE_PROTOCOL  *File;
  EFI_STATUS         Status;
  LOG_LOC_HEADER     Loc;
  UINT8              InfoBuffer[sizeof (EFI_FILE_INFO) + 64];
  EFI_FILE_INFO      *Info;
  UINTN              InfoSize;
  UINTN              Size;
  UINT64             Ring;
  UINT64             Total;
  UINTN              Index;
  UINTN              Tail;

  mLogPresent = FALSE;
  mLogLocatorPresent = FALSE;
  mLogLength = 0;
  mLogStart = 0;
  mLogReadReason = "ring.loc absent";
  mLogArchiveStatus = EFI_NOT_FOUND;
  mLogArchiveBytes = 0;
  File = NULL;
  Status = Root->Open (Root, &File, L"\\EFI\\UniXNU\\ring.loc", EFI_FILE_MODE_READ, 0);
  if (EFI_ERROR (Status)) {
    if (Status != EFI_NOT_FOUND) { mLogReadReason = "ring.loc open failed"; }
    goto Done;
  }
  mLogLocatorPresent = TRUE;
  // The file information includes the name, so the buffer has to be larger
  // than the structure itself.
  Info = (EFI_FILE_INFO *)InfoBuffer;
  ZeroMem (InfoBuffer, sizeof (InfoBuffer));
  InfoSize = sizeof (InfoBuffer);
  Status = File->GetInfo (File, &gEfiFileInfoGuid, &InfoSize, Info);
  if (EFI_ERROR (Status) || (Info->Size > InfoSize) || (Info->FileSize < sizeof (LOG_LOC_HEADER))) {
    mLogReadReason = EFI_ERROR (Status) ? "ring.loc metadata read failed" : "ring.loc is too small or malformed";
    goto Done;
  }
  Size = sizeof (Loc);
  Status = File->Read (File, &Size, &Loc);
  if (EFI_ERROR (Status) || (Size != sizeof (Loc))) {
    mLogReadReason = EFI_ERROR (Status) ? "ring.loc read failed" : "ring.loc is truncated";
    goto Done;
  }
  Ring = Loc.Ring;
  if ((Loc.Magic != LOG_LOC_MAGIC) || (Loc.Version != LOG_LOC_VERSION) || (Ring == 0) ||
      (Loc.Data < 0x1000) || (Loc.Data > MAX_UINTN) ||
      ((Loc.Data & (Loc.Data - 1)) != 0) || (Loc.Data > MAX_UINT64 - LOG_RING_HEADER) ||
      (Loc.Total != LOG_RING_HEADER + Loc.Data) || (Ring > MAX_UINT64 - Loc.Total)) {
    mLogReadReason = "ring.loc geometry is invalid";
    goto Done;
  }
  if (!LogMapped (Ring, Ring + Loc.Total)) {
    mLogReadReason = "prior ring memory is not mapped; reset may have cleared RAM";
    goto Done;
  }
  mLogData = Loc.Data;
  if (U64 ((VOID *)(UINTN)Ring) != LOG_RING_MAGIC) {
    mLogReadReason = "prior ring magic missing; reset may have cleared RAM";
    goto Done;
  }
  Total = U64 ((VOID *)(UINTN)(Ring + 8));
  if (Total > mLogData) {
    // The ring has wrapped: the oldest byte is where the counter is now.
    mLogStart = (UINTN)(Total & (mLogData - 1));
    mLogLength = (UINTN)mLogData;
  } else {
    mLogStart = 0;
    mLogLength = (UINTN)Total;
  }
  if (mLogLength == 0) {
    mLogReadReason = "prior ring was empty";
    goto Done;
  }
  mLogPresent = TRUE;
  mLogReadReason = "prior ring recovered";
  //
  // The tail is all that goes on the screen; the whole log is written to the
  // ESP below, straight out of RAM.
  //
  Tail = (mLogLength < LOG_TAIL) ? mLogLength : (UINTN)LOG_TAIL;
  for (Index = 0; Index < Tail; Index++) {
    UINT8 Byte = LogByte (Ring, mLogLength - Tail + Index);
    if (((Byte < 32) || (Byte > 126)) && (Byte != '\n') && (Byte != '\r') && (Byte != '\t')) { Byte = '.'; }
    mLogTail[Index] = (CHAR8)Byte;
  }
  mLogTail[Tail] = 0;
  mLogArchiveStatus = LogWriteArchive (Root, Ring, &mLogArchiveBytes);
  if (!EFI_ERROR (mLogArchiveStatus) && (mLogArchiveBytes != mLogLength)) {
    mLogArchiveStatus = EFI_DEVICE_ERROR;
  }
Done:
  if (File != NULL) { File->Close (File); }
  if (!mLogPresent) { mLogTail[0] = 0; }
}

// Hand the next boot the location of the ring that is about to go live.  Called
// once the window is known and before the volume is closed.
STATIC
EFI_STATUS
LogWriteNext (
  EFI_FILE_PROTOCOL  *Root,
  UINT64             Ring,
  UINT64             Total,
  UINT64             Data
  )
{
  EFI_FILE_PROTOCOL  *File;
  EFI_STATUS         Status;
  LOG_LOC_HEADER     Loc;
  UINTN              Length;

  if (Ring == 0) { return EFI_NOT_FOUND; }
  //
  // The ring belongs to the kernel, which owns the byte counter at +8 and the
  // text at +0x40.  Only the magic at +0 is written here, so that the next
  // boot can tell a real ring from whatever the memory happens to hold.
  //
  WriteUnaligned64 ((UINT64 *)(UINTN)Ring, LOG_RING_MAGIC);

  ZeroMem (&Loc, sizeof (Loc));
  Loc.Magic = LOG_LOC_MAGIC;
  Loc.Version = LOG_LOC_VERSION;
  Loc.Ring = Ring;
  Loc.Total = Total;
  Loc.Data = Data;
  File = NULL;
  Status = Root->Open (Root, &File, L"\\EFI\\UniXNU\\ring.loc",
                       EFI_FILE_MODE_READ | EFI_FILE_MODE_WRITE | EFI_FILE_MODE_CREATE, 0);
  if (EFI_ERROR (Status)) { return Status; }
  Status = File->SetPosition (File, 0);
  if (!EFI_ERROR (Status)) {
    Length = sizeof (Loc);
    Status = File->Write (File, &Length, (VOID *)&Loc);
    if (!EFI_ERROR (Status) && (Length != sizeof (Loc))) { Status = EFI_DEVICE_ERROR; }
  }
  if (!EFI_ERROR (Status)) { Status = File->Flush (File); }
  {
    EFI_STATUS CloseStatus = File->Close (File);
    if (!EFI_ERROR (Status)) { Status = CloseStatus; }
  }
  return Status;
}

// Large diagnostic text straight into the GOP framebuffer: the UEFI text console font is too small to
// photograph. 5x12 glyphs from DiagFont.h scaled 4x, white on a black box; wraps at the right edge.
#include "DiagFont.h"
STATIC CONST UINT64 mDiagFont[] = GLYPH_FONT;

STATIC
VOID
DiagDraw (CONST CHAR16 *Text, UINTN Row)
{
  EFI_GRAPHICS_OUTPUT_PROTOCOL *Gop;
  UINT64                       Video[6];
  UINTN                        Scale = 4, X = 0, Y, Index, Gx, Gy, Dx, Dy, Cell, Line;
  UINT32                       *Fb;
  UINT64                       Glyph, Data;
  CHAR16                       Ch;

  if (EFI_ERROR (gBS->LocateProtocol (&gEfiGraphicsOutputProtocolGuid, NULL, (VOID **)&Gop)) ||
      EFI_ERROR (DarwinBootVideoFromGop (Gop, Video))) { return; }
  Cell = 6 * Scale;
  Line = 14 * Scale;
  Y = Video[4] / 4 + Row * Line;
  Fb = (UINT32 *)(UINTN)Video[0];
  for (Index = 0; Text[Index] != 0; Index++) {
    Ch = Text[Index];
    if ((Ch < 32) || (Ch > 126)) { continue; }
    if (X + Cell > Video[3]) { X = 0; Y += Line; }
    if (Y + Line > Video[4]) { return; }
    Glyph = mDiagFont[Ch - 32];
    for (Gy = 0; Gy < 14 * Scale; Gy++) {
      for (Gx = 0; Gx < Cell; Gx++) {
        Fb[(Y + Gy) * (Video[2] / 4) + X + Gx] = 0xFF000000;
      }
    }
    for (Gy = 0; Gy < 12; Gy++) {
      Data = (Gy < 6) ? ((Glyph >> 32) >> (Gy * 5)) : ((Glyph & 0xFFFFFFFFULL) >> ((Gy - 6) * 5));
      for (Gx = 0; Gx < 5; Gx++) {
        if ((Data >> Gx) & 1) {
          for (Dy = 0; Dy < Scale; Dy++) {
            for (Dx = 0; Dx < Scale; Dx++) {
              Fb[(Y + Gy * Scale + Dy) * (Video[2] / 4) + X + Gx * Scale + Dx] = 0xFFFFFFFF;
            }
          }
        }
      }
    }
    X += Cell;
  }
}

STATIC
EFI_STATUS
AllocateWindow (KERNEL *Kernel, UINT64 WindowSize, EFI_PHYSICAL_ADDRESS *Address)
{
  EFI_MEMORY_DESCRIPTOR *Map;
  EFI_MEMORY_DESCRIPTOR *Descriptor;
  EFI_STATUS            Status;
  UINTN                 Capacity;
  UINTN                 Size;
  UINTN                 Key;
  UINTN                 Stride;
  UINTN                 Offset;
  UINT64                End;
  UINT64                Candidate;
  UINT64                BestStart;
  UINT64                BestBytes;
  UINT64                RunBytes;

  *Address = 0;
  Map = NULL;
  Capacity = 0;
  BestStart = 0;
  BestBytes = 0;
  Status = GetMap (&Map, &Capacity, &Size, &Key, &Stride);
  if (EFI_ERROR (Status)) { if (Map != NULL) { FreePool (Map); } return Status; }
  Status = EFI_OUT_OF_RESOURCES;
  for (Offset = 0; Offset + Stride <= Size; Offset += Stride) {
    Descriptor = (EFI_MEMORY_DESCRIPTOR *)((UINT8 *)Map + Offset);
    if ((Descriptor->Type != EfiConventionalMemory) || (Descriptor->NumberOfPages == 0)) { continue; }
    End = RunEnd (Map, Size, Stride, Descriptor->PhysicalStart,
                  Descriptor->PhysicalStart + EFI_PAGES_TO_SIZE (Descriptor->NumberOfPages));
    RunBytes = End - Descriptor->PhysicalStart;
    if (RunBytes > BestBytes) { BestBytes = RunBytes; BestStart = Descriptor->PhysicalStart; }
    Candidate = ALIGN_VALUE (Descriptor->PhysicalStart, BLOCK_SIZE) + (Kernel->Base & (BLOCK_SIZE - 1));
    if ((Candidate < Descriptor->PhysicalStart) || (End < Candidate) || (End - Candidate < WindowSize)) { continue; }
    Status = gBS->AllocatePages (AllocateAddress, EfiLoaderCode, EFI_SIZE_TO_PAGES (WindowSize), &Candidate);
    if (!EFI_ERROR (Status)) { *Address = Candidate; break; }
  }
  if (EFI_ERROR (Status)) {
    CHAR16 Line[96];
    UINTN  Row = 1;
    UnicodeSPrint (Line, sizeof (Line), L"NO WINDOW %lx BIG RUN %lx @ %lx", WindowSize, BestBytes, BestStart);
    DiagDraw (Line, 0);
    for (Offset = 0; Offset + Stride <= Size; Offset += Stride) {
      Descriptor = (EFI_MEMORY_DESCRIPTOR *)((UINT8 *)Map + Offset);
      if ((Descriptor->Type == EfiConventionalMemory) && (Descriptor->NumberOfPages >= 0x400) && (Row < 8)) {
        UnicodeSPrint (Line, sizeof (Line), L"FREE %lx + %lx", Descriptor->PhysicalStart, EFI_PAGES_TO_SIZE (Descriptor->NumberOfPages));
        DiagDraw (Line, Row++);
      }
    }

  }
  FreePool (Map);
  return Status;
}

STATIC
EFI_STATUS
EFIAPI
Boot (
  IN UNIXNU_LOADER_PROTOCOL    *This,
  IN CONST UNIXNU_LOADER_BOOT_REQUEST *Request
  )
{
  EFI_STATUS                      Status;
  EFI_SIMPLE_FILE_SYSTEM_PROTOCOL  *Fs;
  EFI_FILE_PROTOCOL               *Root;
  VOID                            *Image;
  VOID                            *Tree;
  VOID                            *TrustCaches;
  UINTN                           ImageSize;
  UINTN                           TreeSize;
  UINTN                           TrustCacheSize;
  CHAR16                          *TreePath;
  UINTN                           Length;
  UINTN                           Index;
  UINTN                           Offset;
  KERNEL                          Kernel;
  EFI_PHYSICAL_ADDRESS            Address;
  UINT64                          ArgsAddress;
  UINT64                          TreeAddress;
  UINT64                          Used;
  UINT64                          WindowSize;
  UINT64                          Ring;
  LOG_RING_DESC                   RingDesc;
  UINT64                          RamdiskAddress;
  UINTN                           RamdiskOffset;
  UINTN                           RamdiskBytes;
  UINTN                           RamdiskExpandedSize;
  BOOLEAN                         RamdiskPacked;
  BOOLEAN                         KeepPacked;
  UINTN                           TrustCacheOffset;
  UINT64                          TrustCacheAddress;
  DARWIN_BOOT_ARGS                *Args;
  EFI_MEMORY_DESCRIPTOR           *Map;
  UINTN                           Capacity;
  UINTN                           MapSize;
  UINTN                           Key;
  UINTN                           Stride;
  UINT32                          Version;
  UINTN                           Attempt;
  CHAR8                           CommandLine[1024];
  UINT64                          Video[6];

  (VOID)This;
  if ((Request == NULL) || (Request->Volume == NULL) || (Request->KernelPath == NULL)) { return EFI_INVALID_PARAMETER; }
  if ((Request->KextCount != 0) || (Request->DeviceTree != NULL) || (Request->DeviceTreeSize != 0) ||
      (DarwinCurrentEl () != 4)) {
    Print (L"DARWINBOOT: unsupported inputs or exception level; firmware retained\n");
    return EFI_UNSUPPORTED;
  }
  if (!RamdiskLayout (Request->Ramdisk, Request->RamdiskSize, &RamdiskExpandedSize, &RamdiskPacked)) {
    return EFI_BAD_BUFFER_SIZE;
  }
  ZeroMem (CommandLine, sizeof (CommandLine));
  if (Request->BootArgs != NULL) {
    for (Index = 0; Request->BootArgs[Index] != 0; Index++) {
      if ((Index >= sizeof (CommandLine) - 1) || (Request->BootArgs[Index] > 0x7f)) { return EFI_BAD_BUFFER_SIZE; }
      CommandLine[Index] = (CHAR8)Request->BootArgs[Index];
    }
  }
  Root = NULL;
  Image = NULL;
  Tree = NULL;
  TrustCaches = NULL;
  TreePath = NULL;
  Map = NULL;
  Address = 0;
  Capacity = 0;
  Status = gBS->HandleProtocol (Request->Volume, &gEfiSimpleFileSystemProtocolGuid, (VOID **)&Fs);
  if (EFI_ERROR (Status)) { return Status; }
  Status = Fs->OpenVolume (Fs, &Root);
  if (EFI_ERROR (Status)) { return Status; }
  LogReadPrevious (Root);
  Status = ReadFile (Root, Request->KernelPath, SIZE_128MB, &Image, &ImageSize);
  if (EFI_ERROR (Status)) { goto Fail; }
  Status = ParseKernel (Image, ImageSize, &Kernel);
  if (EFI_ERROR (Status)) { goto Fail; }
  //
  // The console ring, if this kernel carries one: a hole in the kernel's own
  // data that the kernel appends its console output to, and that the next
  // boot reads back.
  //
  ZeroMem (&RingDesc, sizeof (RingDesc));
  Ring = 0;
  if (LogFindRing ((CONST UINT8 *)Image, ImageSize, &RingDesc)) {
    mRingDesc = RingDesc;
    if ((RingDesc.Ring >= Kernel.Base) && (RingDesc.Ring - Kernel.Base < Kernel.End - Kernel.Base)) {
      Ring = RingDesc.Ring - Kernel.Base;   // the offset into the window
    }
  }
  Length = StrSize (Request->KernelPath);
  TreePath = AllocatePool (Length + 11 * sizeof (CHAR16));
  if (TreePath == NULL) { Status = EFI_OUT_OF_RESOURCES; goto Fail; }
  CopyMem (TreePath, Request->KernelPath, Length);
  StrCatS (TreePath, Length / sizeof (CHAR16) + 11, L".adt");
  Status = ReadFile (Root, TreePath, SIZE_16MB, &Tree, &TreeSize);
  if (EFI_ERROR (Status)) { goto Fail; }
  Offset = 0;
  RamdiskOffset = MAX_UINTN;
  TrustCacheOffset = MAX_UINTN;
  WindowSize = 0;
  KeepPacked = FALSE;
  if (!AdtNode (Tree, TreeSize, &Offset, 0, 3, &RamdiskOffset, &TrustCacheOffset, &WindowSize, &KeepPacked) || (Offset != TreeSize)) { Status = EFI_COMPROMISED_DATA; goto Fail; }
  if (KeepPacked && (!RamdiskPacked || (RamdiskOffset == MAX_UINTN))) { Status = EFI_COMPROMISED_DATA; goto Fail; }
  if (WindowSize == 0) { WindowSize = WINDOW_SIZE; }
  if (Kernel.Base > MAX_UINT64 - WindowSize) { Status = EFI_LOAD_ERROR; goto Fail; }
  if (Request->Ramdisk != NULL) {
    if ((RamdiskOffset == MAX_UINTN) || (U64 ((UINT8 *)Tree + RamdiskOffset) != 0) ||
        (U64 ((UINT8 *)Tree + RamdiskOffset + 8) != 0)) { Status = EFI_COMPROMISED_DATA; goto Fail; }
  } else if (RamdiskOffset != MAX_UINTN) {
    Status = EFI_UNSUPPORTED;
    goto Fail;
  }
  CopyMem (TreePath, Request->KernelPath, Length);
  StrCatS (TreePath, Length / sizeof (CHAR16) + 11, L".trustcache");
  Status = ReadFile (Root, TreePath, SIZE_16MB, &TrustCaches, &TrustCacheSize);
  if (EFI_ERROR (Status) && (Status != EFI_NOT_FOUND)) { goto Fail; }
  if (TrustCaches != NULL) {
    if ((TrustCacheOffset == MAX_UINTN) || (U64 ((UINT8 *)Tree + TrustCacheOffset) != 0) ||
        (U64 ((UINT8 *)Tree + TrustCacheOffset + 8) != 0) || !ValidTrustCaches (TrustCaches, TrustCacheSize)) {
      Status = EFI_COMPROMISED_DATA;
      goto Fail;
    }
  } else if (TrustCacheOffset != MAX_UINTN) {
    Status = EFI_COMPROMISED_DATA;
    goto Fail;
  }
  Status = ReadBootVideo (Video);
  if (EFI_ERROR (Status)) { goto Fail; }
  Status = AllocateWindow (&Kernel, WindowSize, &Address);
  if (EFI_ERROR (Status)) { goto Fail; }
  ArgsAddress = ALIGN_VALUE (Address + Kernel.End - Kernel.Base, SIZE_16KB);
  TreeAddress = ArgsAddress + EFI_PAGE_SIZE;
  RamdiskAddress = ALIGN_VALUE (TreeAddress + TreeSize, EFI_PAGE_SIZE);
  RamdiskBytes = ALIGN_VALUE (KeepPacked ? Request->RamdiskSize : RamdiskExpandedSize, EFI_PAGE_SIZE);
  TrustCacheAddress = RamdiskAddress + RamdiskBytes;
  Used = ALIGN_VALUE (TrustCacheAddress + TrustCacheSize, SIZE_16KB) - Address;
  if (Used > WindowSize - BLOCK_SIZE) { Status = EFI_BAD_BUFFER_SIZE; goto Fail; }
  ZeroMem ((VOID *)(UINTN)Address, (UINTN)Used);
  for (Index = 0; Index < Kernel.Count; Index++) {
    CopyMem ((VOID *)(UINTN)(Address + Kernel.Segments[Index].Address - Kernel.Base),
             (UINT8 *)Image + Kernel.Segments[Index].FileOffset, (UINTN)Kernel.Segments[Index].FileSize);
  }
  //
  // The console ring lives in the kernel's own data, so that it is still
  // mapped after the kernel replaces its page tables.  Its header goes in
  // after the image is copied, or the copy would wipe it.
  //
  mLogLocationStatus = (Ring != 0)
      ? LogWriteNext (Root, Address + Ring, RingDesc.Total, RingDesc.Data)
      : EFI_NOT_FOUND;
  Args = (DARWIN_BOOT_ARGS *)(UINTN)ArgsAddress;
  Args->Revision = 2;
  Args->Version = 2;
  Args->VirtualBase = Kernel.Base;
  Args->PhysicalBase = Address;
  Args->MemorySize = WindowSize;
  Args->MemorySizeActual = WindowSize;
  Args->TopOfKernelData = Address + Used;
  CopyMem (Args->Video, Video, sizeof (Video));
  Args->DeviceTree = Kernel.Base + TreeAddress - Address;
  Args->DeviceTreeLength = (UINT32)TreeSize;
  CopyMem (Args->CommandLine, CommandLine, sizeof (CommandLine));
  CopyMem ((VOID *)(UINTN)TreeAddress, Tree, TreeSize);
  if (Request->Ramdisk != NULL) {
    if (KeepPacked) { CopyMem ((VOID *)(UINTN)RamdiskAddress, Request->Ramdisk, Request->RamdiskSize); }
    else { CopyRamdisk ((VOID *)(UINTN)RamdiskAddress, Request->Ramdisk, Request->RamdiskSize, RamdiskPacked); }
    WriteUnaligned64 ((UINT64 *)(UINTN)(TreeAddress + RamdiskOffset), RamdiskAddress);
    WriteUnaligned64 ((UINT64 *)(UINTN)(TreeAddress + RamdiskOffset + 8), KeepPacked ? Request->RamdiskSize : RamdiskBytes);
  }
  if (TrustCaches != NULL) {
    CopyMem ((VOID *)(UINTN)TrustCacheAddress, TrustCaches, TrustCacheSize);
    WriteUnaligned64 ((UINT64 *)(UINTN)(TreeAddress + TrustCacheOffset), TrustCacheAddress);
    WriteUnaligned64 ((UINT64 *)(UINTN)(TreeAddress + TrustCacheOffset + 8), TrustCacheSize);
    FreePool (TrustCaches); TrustCaches = NULL;
  }
  FreePool (Image); Image = NULL;
  FreePool (Tree); Tree = NULL;
  FreePool (TreePath); TreePath = NULL;
  Root->Close (Root); Root = NULL;
  Status = GetMap (&Map, &Capacity, &MapSize, &Key, &Stride);
  if (EFI_ERROR (Status)) { goto Fail; }
  Print (L"DARWINBOOT: ARM64 va=0x%lx pa=0x%lx args=0x%lx entry=0x%lx owned=0x%lx\n",
         Kernel.Base, Address, ArgsAddress, Address + Kernel.Entry - Kernel.Base, (UINT64)WindowSize);
  Print (L"DARWINBOOT: video base=0x%lx display=%lu row=%lu width=%lu height=%lu depth=%lu\n",
         Video[0], Video[1], Video[2], Video[3], Video[4], Video[5]);
  Print (L"DARWINBOOT: inputs validated; exiting boot services\n");

  //
  // The same facts in large type over the whole screen. The firmware's log
  // draws far larger than the text console, and the form feed clears the
  // screen first. This is what stays visible if the kernel never draws.
  //
  DEBUG ((DEBUG_ERROR, "\fDARWINBOOT: hand-off to the kernel\n\n"));
  DEBUG ((DEBUG_ERROR, "kernel   va 0x%lx  pa 0x%lx\n", Kernel.Base, Address));
  DEBUG ((DEBUG_ERROR, "entry    pa 0x%lx\n", Address + Kernel.Entry - Kernel.Base));
  DEBUG ((DEBUG_ERROR, "args     pa 0x%lx  window 0x%lx bytes\n", ArgsAddress, (UINT64)WindowSize));
  DEBUG ((DEBUG_ERROR, "video    base 0x%lx  %lux%lu  row %lu bytes  %lu bpp\n", Video[0], Video[3], Video[4], Video[2], Video[5]));
  DEBUG ((DEBUG_ERROR, "entry words %08x %08x %08x %08x\n",
          ReadUnaligned32 ((CONST UINT32 *)(UINTN)(Address + Kernel.Entry - Kernel.Base)),
          ReadUnaligned32 ((CONST UINT32 *)(UINTN)(Address + Kernel.Entry - Kernel.Base + 4)),
          ReadUnaligned32 ((CONST UINT32 *)(UINTN)(Address + Kernel.Entry - Kernel.Base + 8)),
          ReadUnaligned32 ((CONST UINT32 *)(UINTN)(Address + Kernel.Entry - Kernel.Base + 12))));
  DEBUG ((DEBUG_ERROR, "CurrentEL %lu  SCTLR_EL1 0x%lx  TCR_EL1 0x%lx\n", DarwinCurrentEl () >> 2, READ_SYSREG (sctlr_el1), READ_SYSREG (tcr_el1)));
  DEBUG ((DEBUG_ERROR, "ID_AA64MMFR0_EL1 0x%lx  (PARange %lu, 4 KiB granule %a)\n", READ_SYSREG (id_aa64mmfr0_el1),
          READ_SYSREG (id_aa64mmfr0_el1) & 0xF, (((READ_SYSREG (id_aa64mmfr0_el1) >> 28) & 0xF) == 0xF) ? "NOT supported" : "supported"));
  DEBUG ((DEBUG_ERROR, "ID_AA64PFR0_EL1  0x%lx\n", READ_SYSREG (id_aa64pfr0_el1)));
  //
  // The exact hand-off contract the kernel's early boot computes from.
  // The kernel reads these from the boot args, so they are printed as they
  // are written, not as they are meant to be.
  //
  DEBUG ((DEBUG_ERROR, "args virt=0x%lx phys=0x%lx mem=0x%lx top=0x%lx flags=0x%lx\n",
          Args->VirtualBase, Args->PhysicalBase, Args->MemorySize, Args->TopOfKernelData, Args->BootFlags));
  DEBUG ((DEBUG_ERROR, "log ring 0x%lx  previous log %a (%lu bytes) reason=%a\n",
          (Ring != 0) ? Address + Ring : 0, mLogPresent ? "found" : "none",
          (UINT64)mLogLength, mLogReadReason));
  DEBUG ((DEBUG_ERROR, "log archive %a status=%r bytes=%lu/%lu; next locator %a status=%r\n",
          (mLogPresent && !EFI_ERROR (mLogArchiveStatus) && (mLogArchiveBytes == mLogLength)) ? "SAVED" : "NOT SAVED",
          mLogArchiveStatus, (UINT64)mLogArchiveBytes, (UINT64)mLogLength,
          (Ring != 0) ? "written" : "not available", mLogLocationStatus));
  {
    CHAR16 LogStatus[96];
    if (mLogPresent && !EFI_ERROR (mLogArchiveStatus) && (mLogArchiveBytes == mLogLength)) {
      UnicodeSPrint (LogStatus, sizeof (LogStatus), L"LOG SAVED TO ESP: %lu BYTES", (UINT64)mLogArchiveBytes);
    } else if (mLogPresent) {
      UnicodeSPrint (LogStatus, sizeof (LogStatus), L"LOG ARCHIVE FAILED: %r", mLogArchiveStatus);
    } else {
      UnicodeSPrint (LogStatus, sizeof (LogStatus), L"NO PRIOR LOG: %a", mLogReadReason);
    }
    DiagDraw (LogStatus, 2);
    if (Ring != 0 && !EFI_ERROR (mLogLocationStatus)) {
      DiagDraw (L"NEXT CRASH LOG LOCATION SAVED", 3);
    } else {
      DiagDraw (L"NEXT CRASH LOG LOCATION FAILED", 3);
    }
  }
  // Make a failed previous-ring recovery visible long enough to read on the
  // phone; otherwise the next boot's band probes can immediately obscure it.
  if (mLogLocatorPresent &&
      (!mLogPresent || EFI_ERROR (mLogArchiveStatus) || (mLogArchiveBytes != mLogLength))) {
    gBS->Stall (5 * 1000 * 1000);
  }
  DEBUG ((DEBUG_ERROR, "TTBR0_EL1 0x%lx  TTBR1_EL1 0x%lx  sctlr after disable 0x%lx\n",
          READ_SYSREG (ttbr0_el1), READ_SYSREG (ttbr1_el1),
          READ_SYSREG (sctlr_el1) & ~((UINT64)((1 << 0) | (1 << 2) | (1 << 12)))));
  DEBUG ((DEBUG_ERROR, "\nExiting boot services. A green bar across the top\nmeans the kernel entry code ran.\n"));
  if (mLogPresent) {
    //
    // The log from the last boot.  It is printed last so that it is what stays
    // on screen: the kernel paints its bands over the top of the display, and
    // everything below them is what a photograph can still show.
    //
    DEBUG ((DEBUG_ERROR, "\n--- previous boot log, %lu bytes ---\n%a\n", (UINT64)mLogLength, mLogTail));
  }
  for (Attempt = 0; Attempt < 3; Attempt++) {
    MapSize = Capacity;
    Status = gBS->GetMemoryMap (&MapSize, Map, &Key, &Stride, &Version);
    if (EFI_ERROR (Status)) {
      if (Attempt == 0) { goto Fail; }
      CpuDeadLoop ();
    }
    Status = gBS->ExitBootServices (mImage, Key);
    if (!EFI_ERROR (Status)) {
      DarwinEnter (ArgsAddress, Address + Kernel.Entry - Kernel.Base, Address, WindowSize, Video[0], Video[2] * Video[4]);
      CpuDeadLoop ();
    }
    if (Status != EFI_INVALID_PARAMETER) { CpuDeadLoop (); }
  }
  CpuDeadLoop ();
  return EFI_ABORTED;
Fail:
  if (Root != NULL) { Root->Close (Root); }
  if (Image != NULL) { FreePool (Image); }
  if (Tree != NULL) { FreePool (Tree); }
  if (TrustCaches != NULL) { FreePool (TrustCaches); }
  if (TreePath != NULL) { FreePool (TreePath); }
  if (Map != NULL) { FreePool (Map); }
  if (Address != 0) { gBS->FreePages (Address, EFI_SIZE_TO_PAGES (WindowSize)); }
  Print (L"DARWINBOOT: boot refused: %r; firmware retained\n", Status);
  return Status;
}

STATIC UNIXNU_LOADER_PROTOCOL mLoader = { UNIXNU_LOADER_REVISION, Boot };

STATIC
EFI_STATUS
EFIAPI
Unload (EFI_HANDLE ImageHandle)
{
  return gBS->UninstallMultipleProtocolInterfaces (ImageHandle, &gUniXNULoaderProtocolGuid, &mLoader, NULL);
}

EFI_STATUS
EFIAPI
DarwinBootEntry (EFI_HANDLE ImageHandle, EFI_SYSTEM_TABLE *SystemTable)
{
  EFI_LOADED_IMAGE_PROTOCOL *Loaded;
  EFI_STATUS               Status;
  VOID                     *Existing;

  (VOID)SystemTable;
  Status = gBS->LocateProtocol (&gUniXNULoaderProtocolGuid, NULL, &Existing);
  if (!EFI_ERROR (Status)) { return EFI_ALREADY_STARTED; }
  Status = gBS->HandleProtocol (ImageHandle, &gEfiLoadedImageProtocolGuid, (VOID **)&Loaded);
  if (EFI_ERROR (Status)) { return Status; }
  mImage = ImageHandle;
  Status = gBS->InstallMultipleProtocolInterfaces (&ImageHandle, &gUniXNULoaderProtocolGuid, &mLoader, NULL);
  if (!EFI_ERROR (Status)) {
    Loaded->Unload = Unload;
    Print (L"DARWINBOOT: loader v2 installed EL%u\n", (UINT32)(DarwinCurrentEl () >> 2));
  }
  return Status;
}
