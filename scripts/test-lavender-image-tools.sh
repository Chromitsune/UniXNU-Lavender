#!/usr/bin/env bash
# Host-only unit and local ranged-IPSW fixture tests; no Apple files or device access.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export CARGO_TARGET_DIR=${CARGO_TARGET_DIR:-"$ROOT/out/lavender/image-tools-target"}
cargo test --locked --manifest-path "$ROOT/tools/lavender-image-tools/Cargo.toml"
cargo build --locked --release --manifest-path "$ROOT/tools/lavender-image-tools/Cargo.toml"
python3 "$ROOT/scripts/tests/test_lavender_ipsw_get.py"
