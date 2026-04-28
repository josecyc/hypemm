#!/usr/bin/env bash
# Launch a hypemm runtime instance in a detached `screen` session.
#
# Convention (must match src/hypemm/config.py:derive_run_dir):
#   configs/<mode>/<stem>.toml  →  data/runs/<mode>/<stem>/  →  hypemm-<mode>-<stem>
#
# Usage:
#   scripts/launch.sh <subcommand> configs/<mode>/<stem>.toml [extra args...]
#
# Subcommands:
#   start        — start (resume on-disk state)
#   fresh        — start with --fresh (ignore on-disk state)
#   stop         — kill the screen session
#   status       — print whether it's running
#   tail         — tail -f the runner log
#   live         — start with --live --confirm-live (real money; mainnet only)
#
# Example:
#   scripts/launch.sh start configs/paper/optimized_4pair.toml
#   scripts/launch.sh live  configs/live/min_size_4pair.toml

set -euo pipefail

cmd="${1:-}"
config_arg="${2:-}"

usage() {
  echo "usage: $0 {start|fresh|stop|status|tail|live} <config-path> [extra args...]"
  exit 1
}

if [[ -z "$cmd" || -z "$config_arg" ]]; then
  usage
fi

shift 2 || true
extra_args=("$@")

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Resolve config path relative to repo root if not absolute.
if [[ "$config_arg" = /* ]]; then
  CONFIG_PATH="$config_arg"
else
  CONFIG_PATH="$ROOT_DIR/$config_arg"
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "config not found: $CONFIG_PATH" >&2
  exit 1
fi

# Derive mode + stem from the config path.
rel="${CONFIG_PATH#$ROOT_DIR/}"
case "$rel" in
  configs/*/*.toml) ;;
  *)
    echo "config must be under configs/<mode>/<stem>.toml: $rel" >&2
    exit 1
    ;;
esac

mode="$(echo "$rel" | awk -F/ '{print $2}')"
stem_file="$(echo "$rel" | awk -F/ '{print $3}')"
stem="${stem_file%.toml}"

RUN_DIR="$ROOT_DIR/data/runs/$mode/$stem"
LOG_FILE="$RUN_DIR/runner.log"
SESSION_NAME="hypemm-$mode-$stem"

mkdir -p "$RUN_DIR"

ensure_screen() {
  if ! command -v screen >/dev/null 2>&1; then
    echo "screen is required but not installed" >&2
    exit 1
  fi
}

is_running() {
  local sessions
  sessions="$(screen -ls 2>/dev/null || true)"
  printf '%s\n' "$sessions" | grep -Eq "[[:space:]][0-9]+\\.${SESSION_NAME}[[:space:]]"
}

session_id() {
  local sessions
  sessions="$(screen -ls 2>/dev/null || true)"
  printf '%s\n' "$sessions" | awk '/[[:space:]][0-9]+\.'"${SESSION_NAME}"'[[:space:]]/ {print $1; exit}'
}

start_session() {
  local extra="$*"
  : >"$LOG_FILE"
  screen -dmS "$SESSION_NAME" bash -lc \
    "cd '$ROOT_DIR' && exec uv run hypemm run --config '$CONFIG_PATH' ${extra} >>'$LOG_FILE' 2>&1"
  sleep 3
  if ! is_running; then
    echo "failed to start $SESSION_NAME" >&2
    exit 1
  fi
}

case "$cmd" in
  start)
    ensure_screen
    if is_running; then
      echo "$SESSION_NAME already running ($(session_id))"
      exit 0
    fi
    start_session "${extra_args[@]}"
    echo "started $SESSION_NAME ($(session_id))"
    echo "log: $LOG_FILE"
    ;;
  fresh)
    ensure_screen
    if is_running; then
      echo "$SESSION_NAME already running ($(session_id)); stop first" >&2
      exit 1
    fi
    start_session --fresh "${extra_args[@]}"
    echo "started $SESSION_NAME with --fresh ($(session_id))"
    echo "log: $LOG_FILE"
    ;;
  live)
    ensure_screen
    if is_running; then
      echo "$SESSION_NAME already running ($(session_id))"
      exit 0
    fi
    start_session --live --confirm-live "${extra_args[@]}"
    echo "started $SESSION_NAME (LIVE) ($(session_id))"
    echo "log: $LOG_FILE"
    ;;
  stop)
    if ! is_running; then
      echo "$SESSION_NAME is not running"
      exit 0
    fi
    screen -S "$SESSION_NAME" -X quit
    echo "stopped $SESSION_NAME"
    ;;
  status)
    if is_running; then
      echo "running ($(session_id))"
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
    usage
    ;;
esac
