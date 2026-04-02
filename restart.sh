#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PID_PATTERN='src/proxy.py'
LOG_FILE="$ROOT_DIR/run.log"
PYTHON_BIN="$ROOT_DIR/venv/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Python executable not found: $PYTHON_BIN" >&2
    exit 1
fi

OLD_PIDS="$(pgrep -f "$PID_PATTERN" || true)"
if [[ -n "$OLD_PIDS" ]]; then
    echo "Stopping existing proxy process: $OLD_PIDS"
    pkill -f "$PID_PATTERN" || true
    sleep 1
fi

echo "Starting proxy..."
nohup "$PYTHON_BIN" src/proxy.py > "$LOG_FILE" 2>&1 &
sleep 2

NEW_PID="$(pgrep -f "$PID_PATTERN" | head -n 1 || true)"
if [[ -z "$NEW_PID" ]]; then
    echo "Proxy failed to start. Recent log output:" >&2
    tail -n 50 "$LOG_FILE" >&2 || true
    exit 1
fi

echo "Proxy started. pid=$NEW_PID"
echo "Log file: $LOG_FILE"
tail -n 20 "$LOG_FILE"
