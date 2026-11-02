#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${NORN_NODE_BIN:-}" ]]; then
  NODE_EXECUTABLE="$NORN_NODE_BIN/node"
elif command -v node >/dev/null 2>&1 && [[ "$(node -p process.platform)" == linux ]]; then
  NODE_EXECUTABLE="$(command -v node)"
elif [[ -x /tmp/node-v22.14.0-linux-x64/bin/node ]]; then
  NODE_EXECUTABLE=/tmp/node-v22.14.0-linux-x64/bin/node
else
  printf 'Install Linux Node.js 22+ inside WSL.\n' >&2
  exit 1
fi
export PATH="$(dirname -- "$NODE_EXECUTABLE"):$PATH"
cd "$ROOT_DIR/frontend"
npm run build