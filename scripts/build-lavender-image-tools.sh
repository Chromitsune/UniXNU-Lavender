#!/usr/bin/env bash
# Build the host-only IPSW/IM4P utilities; no device is opened.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CARGO_TARGET_DIR=${CARGO_TARGET_DIR:-"$ROOT/out/lavender/image-tools-target"} \
  cargo build --locked --release --manifest-path "$ROOT/tools/lavender-image-tools/Cargo.toml"
