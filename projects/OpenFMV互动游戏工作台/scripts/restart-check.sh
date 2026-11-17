#!/usr/bin/env bash
set -euo pipefail
PRODUCT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export TMPDIR="$PRODUCT_ROOT/.intermediate/tmp"
mkdir -p "$TMPDIR"
exec "$PRODUCT_ROOT/backend/.venv/bin/python" "$PRODUCT_ROOT/scripts/restart_check.py" "$@"
