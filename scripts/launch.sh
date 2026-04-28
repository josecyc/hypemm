#!/usr/bin/env bash
# Launch a hypemm runtime instance in a detached tmux session.
#
# Convention (must match src/hypemm/config.py:derive_run_dir):
#   configs/<mode>/<stem>.toml  →  data/runs/<mode>/<stem>/  →  hypemm-<mode>-<stem>
#
# Each instance lives in its own tmux session with two panes:
#   0.0  runner (the hypemm process)
#   0.1  dashboard (read-only, can be detached/reattached freely)
#
# Usage:
#   scripts/launch.sh <subcommand> configs/<mode>/<stem>.toml [extra args...]
#
# Subcommands:
#   start   — start (resume on-disk state)
#   fresh   — start with --fresh (ignore on-disk state)
#   live    — start with --live --confirm-live (real money; mainnet only)
#   stop    — kill the tmux session
#   status  — print whether it's running
#   tail    — tail -f the runner log
#   attach  — tmux attach to the session
#
# Example:
#   scripts/launch.sh start configs/paper/optimized_4pair.toml
#   scripts/launch.sh live  configs/live/min_size_4pair.toml

set -euo pipefail

cmd="${1:-}"
config_arg="${2:-}"

usage() {
  echo "usage: $0 {start|fresh|live|stop|status|tail|attach} <config-path> [extra args...]"
  exit 1
}

if [[ -z "$cmd" || -z "$config_arg" ]]; then
  usage
fi

shift 2 || true
extra_args=("$@")

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$config_arg" = /* ]]; then
  CONFIG_PATH="$config_arg"
else
  CONFIG_PATH="$ROOT_DIR/$config_arg"
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "config not found: $CONFIG_PATH" >&2
  exit 1
fi

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
SESSION="hypemm-$mode-$stem"

mkdir -p "$RUN_DIR"

ensure_tmux() {
  if ! command -v tmux >/dev/null 2>&1; then
    echo "tmux is required but not installed" >&2
    exit 1
  fi
}

is_running() {
  tmux has-session -t "$SESSION" 2>/dev/null
}

# Build the runner command, log-redirected so the dashboard pane stays clean.
runner_cmd_for() {
  local extra="$*"
  printf 'cd %q && uv run hypemm run --config %q %s --log-file %q' \
    "$ROOT_DIR" "$CONFIG_PATH" "$extra" "$LOG_FILE"
}

dashboard_cmd() {
  printf 'cd %q && uv run hypemm dashboard --config %q' \
    "$ROOT_DIR" "$CONFIG_PATH"
}

start_session() {
  local runner_cmd
  runner_cmd="$(runner_cmd_for "$@")"
  : >"$LOG_FILE"
  # Pane 0.0: runner. Pane 0.1 (split below): dashboard.
  tmux new-session -d -s "$SESSION" -n hypemm "$runner_cmd"
  # Give the runner a moment to start before splitting the dashboard.
  sleep 2
  tmux split-window -t "$SESSION:0" -h "$(dashboard_cmd)"
  tmux select-pane -t "$SESSION:0.0"
  if ! is_running; then
    echo "failed to start $SESSION" >&2
    exit 1
  fi
}

case "$cmd" in
  start)
    ensure_tmux
    if is_running; then
      echo "$SESSION already running"
      exit 0
    fi
    start_session "${extra_args[@]}"
    echo "started $SESSION"
    echo "log: $LOG_FILE"
    echo "attach: tmux attach -t $SESSION"
    ;;
  fresh)
    ensure_tmux
    if is_running; then
      echo "$SESSION already running; stop first" >&2
      exit 1
    fi
    start_session --fresh "${extra_args[@]}"
    echo "started $SESSION with --fresh"
    echo "log: $LOG_FILE"
    echo "attach: tmux attach -t $SESSION"
    ;;
  live)
    ensure_tmux
    if is_running; then
      echo "$SESSION already running"
      exit 0
    fi
    start_session --live --confirm-live "${extra_args[@]}"
    echo "started $SESSION (LIVE)"
    echo "log: $LOG_FILE"
    echo "attach: tmux attach -t $SESSION"
    ;;
  stop)
    if ! is_running; then
      echo "$SESSION is not running"
      exit 0
    fi
    tmux kill-session -t "$SESSION"
    echo "stopped $SESSION"
    ;;
  status)
    if is_running; then
      echo "running"
      echo "log: $LOG_FILE"
      echo "attach: tmux attach -t $SESSION"
    else
      echo "not running"
    fi
    ;;
  tail)
    touch "$LOG_FILE"
    tail -n 80 -f "$LOG_FILE"
    ;;
  attach)
    if ! is_running; then
      echo "$SESSION is not running" >&2
      exit 1
    fi
    tmux attach -t "$SESSION"
    ;;
  *)
    usage
    ;;
esac
