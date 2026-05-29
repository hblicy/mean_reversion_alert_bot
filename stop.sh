#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$ROOT/bot.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "No bot.pid found."
  exit 0
fi

BOT_PID="$(cat "$PID_FILE" || true)"
if [[ -z "${BOT_PID:-}" ]]; then
  rm -f "$PID_FILE"
  echo "Empty pid file removed."
  exit 0
fi

if kill -0 "$BOT_PID" 2>/dev/null; then
  kill "$BOT_PID"
  for _ in {1..10}; do
    if ! kill -0 "$BOT_PID" 2>/dev/null; then
      break
    fi
    sleep 1
  done
  if kill -0 "$BOT_PID" 2>/dev/null; then
    kill -9 "$BOT_PID" 2>/dev/null || true
  fi
  echo "Stopped alert bot. PID=$BOT_PID"
else
  echo "Process not running. Removing stale pid file."
fi

rm -f "$PID_FILE"
