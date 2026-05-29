#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$ROOT/bot.pid"
LOG_DIR="$ROOT/logs"
LOG_FILE="$LOG_DIR/alert-bot.log"

if [[ -f "$PID_FILE" ]]; then
  EXISTING_PID="$(cat "$PID_FILE" || true)"
  if [[ -n "${EXISTING_PID:-}" ]] && kill -0 "$EXISTING_PID" 2>/dev/null; then
    echo "Alert bot is already running. PID=$EXISTING_PID"
    exit 0
  fi
fi

mkdir -p "$LOG_DIR"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT/.venv/bin/python"
else
  PYTHON_BIN="${PYTHON:-python3}"
fi

nohup "$PYTHON_BIN" bot.py >>"$LOG_FILE" 2>&1 &
BOT_PID="$!"
echo "$BOT_PID" > "$PID_FILE"

echo "Started alert bot. PID=$BOT_PID"
echo "log: $LOG_FILE"
