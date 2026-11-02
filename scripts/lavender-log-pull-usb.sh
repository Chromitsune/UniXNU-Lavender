#!/usr/bin/env bash
# Stream the ESP prefix of userdata over recovery ADB, then extract its log.
# This is an offline recovery path: XNU itself currently has no USB console.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OUT=${1:-bootlog.txt}
BLOCK=${LAVENDER_USERDATA_BLOCK:-/dev/block/by-name/userdata}

if ! command -v adb >/dev/null 2>&1; then
  echo "adb (Android platform-tools) is required" >&2
  exit 1
fi
if ! command -v mcopy >/dev/null 2>&1; then
  echo "mtools (mcopy) is required; install it with: brew install mtools" >&2
  exit 1
fi
if [[ ! "$BLOCK" =~ ^/dev/[A-Za-z0-9_./-]+$ ]]; then
  echo "invalid userdata block path: $BLOCK" >&2
  exit 1
fi

ADB=(adb)
if [[ -n "${ADB_SERIAL:-}" ]]; then
  ADB+=(-s "$ADB_SERIAL")
fi
if ! "${ADB[@]}" get-state >/dev/null 2>&1; then
  echo "no usable ADB device; boot OrangeFox/recovery and enable its ADB service" >&2
  echo "For multiple devices, set ADB_SERIAL to the phone's adb serial." >&2
  exit 1
fi
if ! "${ADB[@]}" shell "test -r '$BLOCK'" >/dev/null 2>&1; then
  echo "cannot read $BLOCK through recovery ADB; set LAVENDER_USERDATA_BLOCK to the correct userdata node" >&2
  exit 1
fi

TMP=$(mktemp -d "${TMPDIR:-/tmp}/lavender-log.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
IMAGE="$TMP/userdata-prefix.img"
# The ESP is the first 192 MiB of userdata. exec-out keeps the transfer binary-safe.
REMOTE_DD="dd if=$BLOCK bs=1048576 count=192 2>/dev/null"
"${ADB[@]}" exec-out "$REMOTE_DD" > "$IMAGE"
BYTES=$(wc -c < "$IMAGE" | tr -d ' ')
if [[ "$BYTES" -ne 201326592 ]]; then
  echo "short USB read: expected 201326592 bytes from userdata, got $BYTES" >&2
  exit 1
fi
"$ROOT/scripts/lavender-log-pull.sh" "$IMAGE" "$OUT"
