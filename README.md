# hypemm — Cross-Perp Statistical Arbitrage on Hyperliquid

Trades mean-reversion of price ratios between correlated cryptocurrency
perpetual futures on Hyperliquid. When two normally-correlated coins diverge
significantly (measured by z-score), takes the opposite side and waits for
convergence.

## Setup

```bash
uv sync
```

## Documentation Map

- [docs/CONVENTIONS.md](docs/CONVENTIONS.md) — directory layout, config naming,
  notebook rules. Read this first.
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — operational procedures: fetching data,
  running backtests, launching paper / testnet / live instances.
- [docs/CURRENT_STATE.md](docs/CURRENT_STATE.md) — what's running on the
  server right now.
- [docs/LIVE_DEPLOYMENT.md](docs/LIVE_DEPLOYMENT.md) — mainnet checklist.
- [docs/research/](docs/research/) — frozen research reports.

## Layout At A Glance

```
configs/
  backtest/   paper/   testnet/   live/         # one config = one instance
data/
  market/<provider>/<window>/{candles,funding}/  # inputs (read-only)
  runs/<mode>/<config-stem>/                     # outputs (per-instance)
src/hypemm/                                      # the package
notebooks/                                       # consume committed artifacts
```

A config at `configs/<mode>/<stem>.toml` writes its outputs to
`data/runs/<mode>/<stem>/`. The mapping is mechanical — see
[CONVENTIONS.md](docs/CONVENTIONS.md).

## Quick Start

```bash
# 1. Fetch market data
uv run python scripts/fetch_data.py

# 2. Run a backtest (output → data/runs/backtest/optimized_4pair_6y/)
uv run hypemm backtest --config configs/backtest/optimized_4pair_6y.toml

# 3. Run walk-forward validation
uv run hypemm walkforward --config configs/backtest/optimized_4pair_6y.toml \
    --train-years 1 --test-months 12 --step-months 12

# 4. Open analysis notebooks
uv run jupyter lab notebooks
```

## Running A Live Instance

All instances launch through `scripts/launch.sh`, which derives the screen
session name + run directory from the config path:

```bash
# Paper
scripts/launch.sh start configs/paper/optimized_4pair.toml
scripts/launch.sh tail  configs/paper/optimized_4pair.toml

# Testnet
scripts/launch.sh start configs/testnet/optimized_3pair.toml

# Mainnet live (read docs/LIVE_DEPLOYMENT.md first).
# Always start the paper twin alongside it for head-to-head comparison.
scripts/launch.sh live  configs/live/min_size_4pair.toml
scripts/launch.sh start configs/paper/min_size_4pair.toml
```

Watch a running instance:

```bash
uv run hypemm dashboard --config configs/paper/optimized_4pair.toml
```

The `min_size_4pair` live config is sized to Hyperliquid's minimum: $25/leg ×
8 legs at 5x → $40 USDC max margin. Kill switches scale accordingly.

## Server Access

```bash
ssh -p 6969 dark-forest-guardian@100.91.78.8
tmux ls                                  # what's running
tmux attach -t hypemm-paper-min_size_4pair  # attach (Ctrl-b d to detach)
```

## Strategy

- **Pairs (optimized)**: LINK/SOL, DOGE/AVAX, SOL/AVAX, DOGE/ADA
- **Entry**: z-score of log price ratio exceeds ±2.5, with 7-day rolling
  correlation > 0.7
- **Exit**: z reverts to ±0.5, stop at ±4.0, time stop at 36h, or no progress
  after 12h
- **Sizing**: $50K per leg (paper); $25 per leg (live min-size)
- **Evaluation cadence**: hourly

See [docs/research/2026-04__optimized_walkforward.md](docs/research/2026-04__optimized_walkforward.md)
for the full research path.

## Development

```bash
uv run pytest               # tests
uv run black .              # format
uv run ruff check .         # lint
uv run mypy src/            # types
```

## Architecture

```
src/hypemm/
  cli.py            CLI entry points
  config.py         Config loading + run_dir derivation
  models.py         Domain dataclasses
  data.py           Candle fetching and CSV loading
  signals.py        Z-score and entry signal generation
  engine.py         Core strategy engine
  backtest.py       Backtest and sweep orchestration
  walkforward.py    Walk-forward validation + statistical metrics
  correlation.py    Correlation analysis
  funding.py        Funding-rate integration
  runner.py         Paper / live runtime loop
  dashboard.py      Rich terminal UI
  execution.py      Paper + live execution adapters
  persistence.py    State save/load and trade logs
  validate.py       Validation gate pipeline
```

The engine is mode-agnostic: backtest, paper, testnet, and live all use the
same engine with different orchestrators and execution adapters.
