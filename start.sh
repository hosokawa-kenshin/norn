#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${NORN_NODE_BIN:-}" ]]; then
  NODE_EXECUTABLE="$NORN_NODE_BIN/node"
elif command -v node >/dev/null 2>&1; then
  NODE_EXECUTABLE="$(command -v node)"
elif [[ -x /tmp/node-v22.14.0-linux-x64/bin/node ]]; then
  NODE_EXECUTABLE=/tmp/node-v22.14.0-linux-x64/bin/node
else
  printf 'Linux Node.js 22+ is required inside WSL.\n' >&2
  exit 1
fi

UVICORN="$ROOT_DIR/.venv/bin/uvicorn"
VITE_ENTRY="$ROOT_DIR/frontend/node_modules/vite/bin/vite.js"

if [[ ! -x "$UVICORN" ]]; then
  printf 'Backend dependencies are missing. See README.md.\n' >&2
  exit 1
fi
if [[ ! -f "$VITE_ENTRY" ]]; then
  printf 'Frontend dependencies are missing. Run npm install in frontend/.\n' >&2
  exit 1
fi

find_free_port() {
  "$ROOT_DIR/.venv/bin/python" -c '
import socket
import sys

start = int(sys.argv[1])
for port in range(start, start + 100):
    with socket.socket() as server:
        try:
            server.bind(("127.0.0.1", port))
        except OSError:
            continue
        print(port)
        break
else:
    raise SystemExit("No free port found")
' "$1"
}

API_PORT="$(find_free_port 8000)"
FRONTEND_PORT="$(find_free_port 5173)"
API_URL="http://127.0.0.1:$API_PORT"

export PATH="$(dirname -- "$NODE_EXECUTABLE"):$PATH"

"$UVICORN" app:app --app-dir "$ROOT_DIR/backend" --host 127.0.0.1 --port "$API_PORT" &
BACKEND_PID=$!
(
  cd "$ROOT_DIR/frontend"
  export NORN_API_URL="$API_URL"
  exec "$NODE_EXECUTABLE" "$VITE_ENTRY" --host 127.0.0.1 --port "$FRONTEND_PORT" --strictPort
) &
FRONTEND_PID=$!

cleanup() {
  trap - EXIT INT TERM
  kill "$FRONTEND_PID" "$BACKEND_PID" 2>/dev/null || true
  wait "$FRONTEND_PID" "$BACKEND_PID" 2>/dev/null || true
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf 'NORN frontend: http://127.0.0.1:%s\n' "$FRONTEND_PORT"
printf 'NORN API:      %s\n' "$API_URL"
printf 'Press Ctrl+C to stop both services.\n'

wait -n "$BACKEND_PID" "$FRONTEND_PID"