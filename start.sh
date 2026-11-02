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

select_port() {
  local variable_name="$1"
  local default_port="$2"
  local requested_port="${!variable_name:-}"
  if [[ -z "$requested_port" && -f "$ROOT_DIR/.env" ]]; then
    requested_port="$(sed -n "s/^${variable_name}=//p" "$ROOT_DIR/.env" | head -n 1 | tr -d '\r')"
    requested_port="${requested_port%\"}"
    requested_port="${requested_port#\"}"
  fi
  if [[ -z "$requested_port" ]]; then
    find_free_port "$default_port"
    return
  fi
  if [[ ! "$requested_port" =~ ^[0-9]{1,5}$ ]] || (( requested_port < 1024 || requested_port > 65535 )); then
    printf '%s must be a port number between 1024 and 65535.\n' "$variable_name" >&2
    return 1
  fi
  if ! "$ROOT_DIR/.venv/bin/python" -c 'import socket,sys; s=socket.socket(); s.bind(("127.0.0.1", int(sys.argv[1]))); s.close()' "$requested_port"; then
    printf '%s port %s is already in use.\n' "$variable_name" "$requested_port" >&2
    return 1
  fi
  printf '%s\n' "$requested_port"
}

API_PORT="$(select_port NORN_API_PORT 8000)"
FRONTEND_PORT="$(select_port NORN_FRONTEND_PORT 5173)"
API_URL="http://127.0.0.1:$API_PORT"

export PATH="$(dirname -- "$NODE_EXECUTABLE"):$PATH"
"$NODE_EXECUTABLE" "$ROOT_DIR/frontend/scripts/prepare-voice-assets.mjs"

NORN_API_URL="$API_URL" NORN_FRONTEND_URL="http://127.0.0.1:$FRONTEND_PORT" \
  NORN_GOOGLE_REDIRECT_URI="$API_URL/" \
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