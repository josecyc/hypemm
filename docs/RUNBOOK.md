# Runbook

Operational procedures for running, maintaining, and migrating hypemm
instances. For the directory layout and naming rules, see
[CONVENTIONS.md](CONVENTIONS.md). For what's running right now, see
[CURRENT_STATE.md](CURRENT_STATE.md).

## Setup

```bash
uv sync
```

For mainnet live: see [LIVE_DEPLOYMENT.md](LIVE_DEPLOYMENT.md) for credentials,
account setup, and the size-ramp checklist. Do not run live without reading
that doc first.

## Fetching Market Data

```bash
uv run python scripts/fetch_data.py             # all Binance windows
uv run python scripts/fetch_data.py --quick      # 2y core only
uv run python scripts/fetch_data.py --no-backtest  # skip running backtests after
```

For Hyperliquid native data, the `hypemm fetch` subcommand pulls from the HL
info API; pass any backtest config that uses HL data:

```bash
uv run hypemm fetch --config configs/backtest/optimized_4pair_hl.toml
```

## Running Backtests

```bash
uv run hypemm backtest --config configs/backtest/optimized_4pair_6y.toml
uv run hypemm walkforward --config configs/backtest/optimized_4pair_6y.toml \
    --train-years 1 --test-months 12 --step-months 12
```

Outputs land in `data/runs/backtest/<stem>/` automatically.

## Running Paper / Testnet / Live

All instances are launched through `scripts/launch.sh`, which derives the tmux
session name and run directory from the config path. Each session has the
runner in pane `0.0` and the dashboard in pane `0.1`.

**Live always runs with a paper twin at the same config stem.** Start the live
instance and its paper counterpart together; compare the dashboards as the
primary live-monitoring tool.

```bash
# Paper
scripts/launch.sh start  configs/paper/optimized_4pair.toml
scripts/launch.sh status configs/paper/optimized_4pair.toml
scripts/launch.sh tail   configs/paper/optimized_4pair.toml
scripts/launch.sh stop   configs/paper/optimized_4pair.toml

# Restart with a clean state
scripts/launch.sh fresh  configs/paper/optimized_4pair.toml

# Testnet
scripts/launch.sh start  configs/testnet/optimized_3pair.toml

# Mainnet live (real money — read LIVE_DEPLOYMENT.md first).
# Always launch the paper twin at the same stem alongside it.
scripts/launch.sh live  configs/live/min_size_4pair.toml
scripts/launch.sh start configs/paper/min_size_4pair.toml
```

## Watching A Running Instance

```bash
uv run hypemm dashboard --config configs/paper/optimized_4pair.toml
uv run hypemm trades    --config configs/paper/optimized_4pair.toml
```

Both read from `data/runs/<mode>/<stem>/` and are decoupled from the runner —
restart them freely without touching the runner.

## Migrating An Instance

If a config is renamed or moved, the runner's on-disk state must move with it:

1. Stop the runner: `scripts/launch.sh stop <old-config>`.
2. Move the run directory: `mv data/runs/<old-mode>/<old-stem> data/runs/<new-mode>/<new-stem>`.
3. Pull / apply the config rename.
4. Start with the new config: `scripts/launch.sh start <new-config>`.
5. Confirm `state.json` was preserved (positions, trade count, P&L unchanged).

For the **live** instance, do this last and verify state diffs before
restarting. Losing `state.json` mid-position means the engine forgets
positions and may double-trade.

## Server Layout

The remote server runs each instance in its own tmux session. Naming:

```
hypemm-<mode>-<stem>
```

derived mechanically from the config path. Each session has two panes:

```
0.0  runner
0.1  dashboard
```

```bash
ssh -p 6969 dark-forest-guardian@100.91.78.8
tmux ls                                          # list running instances
tmux attach -t hypemm-paper-min_size_4pair       # attach (Ctrl-b d to detach)
```

## Adding A New Instance

1. Add a config under `configs/<mode>/<name>.toml`. Strategy-similar configs
   should share params; only override what's actually different.
2. Verify locally: `scripts/launch.sh start configs/<mode>/<name>.toml`.
3. Push to the server, pull on the server, launch with the same command.
4. Update [CURRENT_STATE.md](CURRENT_STATE.md) to record the instance.

## When Something Breaks

| Symptom | First check |
|---|---|
| `ValueError: 'data_dir' is no longer a config field` | Old config — strip `data_dir` from the TOML and rely on path-derived run dir. |
| `config path ... is not under a 'configs/' directory` | Config moved outside `configs/<mode>/<stem>.toml` layout. |
| Backtest output lands in unexpected directory | Check the config path; the output dir is derived from it mechanically. |
| Paper runner can't find state | The run dir was renamed but `state.json` wasn't moved. See "Migrating An Instance" above. |
