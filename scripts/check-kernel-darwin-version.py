#!/usr/bin/env python3
"""Reject a stale XNU kernel whose Darwin release disagrees with this board."""
import argparse
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kernel', type=Path)
    parser.add_argument('--board', type=Path, default=ROOT / 'board.toml')
    args = parser.parse_args()

    try:
        expected = tomllib.loads(args.board.read_text())['kernel']['darwin_version']
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        parser.error(f'cannot read [kernel].darwin_version from {args.board}: {exc}')
    try:
        image = args.kernel.read_bytes()
    except OSError as exc:
        parser.error(f'cannot read kernel {args.kernel}: {exc}')

    found = sorted({match.decode('ascii') for match in
                    re.findall(rb'Darwin Kernel Version ([0-9]+\.[0-9]+\.[0-9]+):', image)})
    if not found:
        parser.error(f'{args.kernel} contains no recognizable Darwin kernel version string')
    if found != [expected]:
        parser.error(f'{args.kernel} reports Darwin {", ".join(found)}, but this board targets Darwin {expected}; rebuild XNU from the pinned iOS 16 source before staging')
    print(f'PASS: kernel Darwin {expected} matches board target')


if __name__ == '__main__':
    main()
