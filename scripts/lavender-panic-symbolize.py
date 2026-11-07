#!/usr/bin/env python3
"""Symbolize a panic in a Lavender bootlog.txt against its exact kernel.macho.

The address lookup/ESR-FAR report is adapted from iOSPlus10's MIT-licensed
scripts/debug/panic-symbolize.sh. Unlike the Negroni log path, this accepts the
plain-text ring archived by Lavender's DarwinBoot. It is read-only.
"""
from __future__ import annotations
import argparse
import bisect
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UNIXNU = Path(os.environ.get("UNIXNU_ROOT", ROOT.parent / "UniXNU")).resolve()
DEFAULT_KERNEL = Path(os.environ.get("UNIXNU_KERNEL", DEFAULT_UNIXNU / "out/lavender/kernel/kernel.macho"))
SYMBOL_LINE = re.compile(r"^\s*([0-9a-fA-F]+)\s+([A-Za-z])\s+(\S+)\s*$")
FIELD = re.compile(r"\b(pc|lr|caller|far|esr)\b\s*[:=]?\s*(0x[0-9a-fA-F]+)", re.I)
PANIC_MARKERS = ("panic(cpu", "Kernel data abort", "Kernel instruction fetch abort", "[PANIC]")

ESR_EC = {
    0x25: "data abort (kernel)", 0x24: "data abort (user)",
    0x21: "instruction abort (kernel)", 0x20: "instruction abort (user)",
    0x3C: "BRK", 0x0E: "illegal execution", 0x00: "unknown",
}
ESR_DFSC = {
    0x04: "translation fault L0", 0x05: "translation fault L1",
    0x06: "translation fault L2", 0x07: "translation fault L3",
    0x09: "access flag L1", 0x0A: "access flag L2", 0x0B: "access flag L3",
    0x0D: "permission fault L1", 0x0E: "permission fault L2", 0x0F: "permission fault L3",
    0x10: "synchronous external abort", 0x11: "tag check fault", 0x21: "alignment fault",
}


def parse_int(text: str) -> int:
    return int(text, 0)


def load_symbols(kernel: Path) -> list[tuple[int, str]]:
    result = subprocess.run(["nm", "-n", str(kernel)], check=True, text=True, capture_output=True)
    symbols: dict[int, str] = {}
    for line in result.stdout.splitlines():
        match = SYMBOL_LINE.match(line)
        if match and match.group(2) in "TtDdBbSs":
            addr = int(match.group(1), 16)
            # Keep a stable name when aliases have the same value.
            symbols.setdefault(addr, match.group(3))
    if not symbols:
        raise ValueError(f"no address-bearing symbols from nm for {kernel}")
    return sorted(symbols.items())


def resolve(address: int, symbols: list[tuple[int, str]], slide: int = 0) -> str:
    """Resolve a runtime address; slide is runtime_base - linked_base."""
    values = [item[0] for item in symbols]
    linked = address - slide
    at = bisect.bisect_right(values, linked) - 1
    if at < 0:
        return "?"
    base, name = symbols[at]
    return f"{name}+0x{linked - base:x}"


def panic_section(text: str) -> tuple[str, str]:
    # Prefer XNU's full panic/abort block. The Lavender screen appends [PANIC]
    # after the kernel's normal report, so choosing the last marker blindly would
    # discard the PC/LR/FAR lines that iOSPlus10's symbolizer is designed to use.
    candidates = [(text.rfind(marker), marker) for marker in PANIC_MARKERS[:3]]
    candidates = [(pos, marker) for pos, marker in candidates if pos >= 0]
    if candidates:
        pos, marker = max(candidates)
    else:
        pos, marker = text.rfind("[PANIC]"), "[PANIC]"
    if pos < 0:
        raise ValueError("no panic(cpu), abort, or [PANIC] marker found in log")
    line_start = text.rfind("\n", 0, pos) + 1
    section = text[line_start:]
    return marker, section


def fields(section: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for match in FIELD.finditer(section):
        result[match.group(1).lower()] = int(match.group(2), 16)
    return result


def decode_esr(esr: int) -> str:
    ec, iss = esr >> 26, esr & 0x1FFFFFF
    dfsc, wnr = iss & 0x3F, (iss >> 6) & 1
    kind = ESR_EC.get(ec, f"EC 0x{ec:x}")
    fault = ESR_DFSC.get(dfsc, f"DFSC 0x{dfsc:x}")
    return f"ESR 0x{esr:x}: {kind}, {'write' if wnr else 'read'}, {fault}"


def run(log_path: Path, kernel: Path, slide: int) -> int:
    text = log_path.read_text(errors="replace")
    marker, section = panic_section(text)
    parsed = fields(section)
    print(f"log: {log_path}\nkernel: {kernel}\nmarker: {marker}")
    print("--- panic excerpt ---")
    for line in section.splitlines()[:40]:
        print(line)
    if not parsed:
        print("No PC/LR/FAR/ESR fields found; preserve the raw log for manual review.")
        return 0
    symbols = load_symbols(kernel)
    for name in ("pc", "lr", "caller"):
        if name in parsed:
            print(f"{name}: 0x{parsed[name]:x} -> {resolve(parsed[name], symbols, slide)}")
    if "far" in parsed:
        far = parsed["far"]
        desc = "NULL pointer" if far == 0 else ("NULL plus small offset" if far < 0x1000 else "address")
        print(f"far: 0x{far:x} ({desc})")
    if "esr" in parsed:
        print(decode_esr(parsed["esr"]))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path, help="plain-text ESP bootlog.txt extracted by lavender-log-pull.sh")
    parser.add_argument("--kernel", type=Path, default=DEFAULT_KERNEL,
                        help=f"exact matching kernel.macho (default: {DEFAULT_KERNEL})")
    parser.add_argument("--slide", type=parse_int, default=0,
                        help="runtime minus linked kernel address, if the log's PC/LR are slid")
    args = parser.parse_args()
    if not args.log.is_file():
        parser.error(f"log not found: {args.log}")
    if not args.kernel.is_file():
        parser.error(f"kernel not found: {args.kernel}; pass --kernel <exact kernel.macho>")
    try:
        return run(args.log, args.kernel, args.slide)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"lavender-panic-symbolize: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
