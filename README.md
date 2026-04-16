# hypemm -- Cross-Perp Statistical Arbitrage on Hyperliquid

Trades mean-reversion of price ratios between correlated cryptocurrency perpetual futures on Hyperliquid. When two normally-correlated coins diverge significantly (measured by z-score), takes the opposite side and waits for convergence.

**No API keys needed** -- all data is read-only from public endpoints.

## Setup

```bash
uv sync
```

## Usage

```bash
# Fetch historical candle data
uv run hypemm fetch

# Run full backtest (7 months, all pairs)
uv run hypemm backtest

# Parameter sweep across lookback/entry-z grid
uv run hypemm sweep

# Correlation stability analysis
uv run hypemm correlation

# Live orderbook depth analysis (2 hours of snapshots)
uv run hypemm orderbook

# Go/no-go synthesis from all analysis steps
uv run hypemm synthesize

# Start paper trading
uv run hypemm paper

# Paper trade ignoring saved state
uv run hypemm paper --fresh
```

## Strategy

- **Pairs**: LINK/SOL, DOGE/AVAX, SOL/AVAX, BTC/SOL
- **Entry**: z-score of log price ratio exceeds +/-2.0, with 7-day rolling correlation > 0.7
- **Exit**: z-score reverts to +/-0.5, stop loss at +/-4.0, or 48h time stop
- **Position sizing**: $50K per leg, 2 bps maker fee per side
- **Evaluation cadence**: Hourly (matching backtest timescale)

See [THESIS.md](THESIS.md) for the full research path and results.

## Architecture

```
src/hypemm/
  models.py         Domain dataclasses (Signal, CompletedTrade, etc.)
  config.py         Strategy + infrastructure configuration
  math/             Pure functions: z-score, correlation, P&L
  strategy/         Core engine (entry/exit logic) + signal computation
  data/             Candle fetching, CSV loading, hourly price buffer
  execution/        Paper (and future live) execution adapters
  persistence/      State save/load, trade CSV logging
  dashboard/        Rich terminal UI
  analysis/         Backtest, stats, sweep, correlation, orderbook, synthesis
  cli/              CLI entry points
```

The strategy engine is mode-agnostic: it processes signals and returns orders. The backtest, paper trader, and future live system all use the same engine with different orchestrators and execution adapters.

## Development

```bash
uv run pytest                # Run tests
uv run black .               # Format
uv run ruff check .          # Lint
uv run mypy src/             # Type check
```
