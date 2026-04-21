#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="$ROOT_DIR/configs/paper_original_3_ada.toml"
DATA_DIR="$ROOT_DIR/data/paper_original_3_ada"
LOG_FILE="$DATA_DIR/runner.log"
SESSION_NAME="hypemm_paper_original_3_ada"

mkdir -p "$DATA_DIR"

is_running() {
  local sessions
  sessions="$(screen -ls 2>/dev/null || true)"
  if printf '%s\n' "$sessions" | grep -Eq "[[:space:]][0-9]+\\.${SESSION_NAME}[[:space:]]"; then
    return 0
  fi
  return 1
}

session_id() {
  local sessions
  sessions="$(screen -ls 2>/dev/null || true)"
  printf '%s\n' "$sessions" | awk '/[[:space:]][0-9]+\.'"${SESSION_NAME}"'[[:space:]]/ {print $1; exit}'
}

ensure_screen() {
  if ! command -v screen >/dev/null 2>&1; then
    echo "screen is required but not installed"
    exit 1
  fi
}

start_session() {
  local fresh_flag="${1:-}"
  : >"$LOG_FILE"
  screen -dmS "$SESSION_NAME" bash -lc \
    "cd '$ROOT_DIR' && exec uv run hypemm run --config '$CONFIG_PATH' ${fresh_flag} >>'$LOG_FILE' 2>&1"
  sleep 3
  if ! is_running; then
    echo "failed to start original_3+ADA paper trader"
    exit 1
  fi
}

case "${1:-}" in
  start)
    ensure_screen
    if is_running; then
      echo "original_3+ADA paper trader already running in screen session $(session_id)"
      exit 0
    fi
    start_session
    echo "started original_3+ADA paper trader"
    echo "screen: $(session_id)"
    echo "log: $LOG_FILE"
    ;;
  fresh)
    ensure_screen
    if is_running; then
      echo "original_3+ADA paper trader already running in screen session $(session_id)"
      exit 1
    fi
    start_session "--fresh"
    echo "started original_3+ADA paper trader with --fresh"
    echo "screen: $(session_id)"
    echo "log: $LOG_FILE"
    ;;
  stop)
    if ! is_running; then
      echo "original_3+ADA paper trader is not running"
      exit 0
    fi
    screen -S "$SESSION_NAME" -X quit
    echo "stopped original_3+ADA paper trader"
    ;;
  status)
    if is_running; then
      echo "running"
      echo "screen: $(session_id)"
      echo "log: $LOG_FILE"
    else
      echo "not running"
    fi
    ;;
  tail)
    touch "$LOG_FILE"
    tail -n 80 -f "$LOG_FILE"
    ;;
  *)
    echo "usage: $0 {start|fresh|stop|status|tail}"
    exit 1
    ;;
esac
