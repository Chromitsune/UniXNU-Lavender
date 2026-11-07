#!/usr/bin/env python3
"""Offline tests for bootlog/status/locator extraction from a FAT ESP image."""
from __future__ import annotations
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAGIC = 0x314C4F56414C414C
STATUS = b"LAVENDER_RING_STATUS_V1\nreason=ring magic missing (RAM lost or overwritten)\n"
LOG = b"[PANIC] synthetic log for host-only extraction test\n"


def run(*args: str) -> None:
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    for tool in ("mformat", "mmd", "mcopy"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} is required (install mtools)")
    base = ROOT / "out/lavender"
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="log-pull-test-", dir=base) as tmp:
        d = Path(tmp)
        image = d / "esp.img"
        image.write_bytes(bytes(16 * 1024 * 1024))
        run("mformat", "-i", str(image), "-F", "::")
        run("mmd", "-i", str(image), "::/EFI")
        run("mmd", "-i", str(image), "::/EFI/UniXNU")
        loc = d / "ring.loc"
        loc.write_bytes(struct.pack("<QIIQQQ", MAGIC, 1, 0, 0xEBEADF00, 0x10040, 0x10000))
        status = d / "ring-status.txt"
        status.write_bytes(STATUS)
        run("mcopy", "-i", str(image), str(loc), "::/EFI/UniXNU/ring.loc")
        run("mcopy", "-i", str(image), str(status), "::/EFI/UniXNU/ring-status.txt")

        no_log = d / "no-log.txt"
        result = subprocess.run([str(ROOT / "scripts/lavender-log-pull.sh"), str(image), str(no_log)],
                                capture_output=True, text=True)
        if result.returncode != 1:
            raise SystemExit(f"missing-log path returned {result.returncode}, expected 1")
        if (d / "no-log.ring.loc").read_bytes() != loc.read_bytes():
            raise SystemExit("ring.loc sidecar mismatch")
        if (d / "no-log.status.txt").read_bytes() != STATUS:
            raise SystemExit("ring-status sidecar mismatch")
        if no_log.exists():
            raise SystemExit("missing bootlog produced a false log file")

        log = d / "bootlog.txt"
        log.write_bytes(LOG)
        run("mcopy", "-o", "-i", str(image), str(log), "::/EFI/UniXNU/bootlog.txt")
        success = d / "with-log.txt"
        subprocess.run([str(ROOT / "scripts/lavender-log-pull.sh"), str(image), str(success)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if success.read_bytes() != LOG or (d / "with-log.status.txt").read_bytes() != STATUS:
            raise SystemExit("bootlog/status extraction mismatch")
    print("PASS: offline bootlog, ring.loc, and ring-status extraction")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
