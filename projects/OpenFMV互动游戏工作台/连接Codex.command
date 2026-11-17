#!/bin/bash
set -euo pipefail
if [ "$#" -ne 0 ]; then
  printf '%s\n' '请直接双击连接Codex.command。'
  exit 2
fi
OPENFMV_PRODUCT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$OPENFMV_PRODUCT_DIR"
umask 077
export TMPDIR="$OPENFMV_PRODUCT_DIR/.intermediate/tmp"
mkdir -p "$TMPDIR"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
exec npm --prefix "$OPENFMV_PRODUCT_DIR/scripts/codex-bridge" run start -- pair --run
