#!/usr/bin/env bash
# Run the host-only IPSW/IM4P CLI; never opens or modifies a device.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export CARGO_TARGET_DIR=${CARGO_TARGET_DIR:-"$ROOT/out/lavender/image-tools-target"}
exec cargo run --locked --release --manifest-path "$ROOT/tools/lavender-image-tools/Cargo.toml" -- "$@"
