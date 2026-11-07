#!/usr/bin/env bash
# Offline tests for ESP log/diagnostic extraction; requires mtools, no device access.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
python3 "$ROOT/scripts/tests/test_lavender_log_pull.py"
