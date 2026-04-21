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

# Run backtest
uv run hypemm backtest

# Run validation gates
uv run hypemm validate

# Start paper trading
uv run hypemm run

# Paper trade ignoring saved state
uv run hypemm run --fresh

# Start the isolated original_3+ADA paper instance
./scripts/paper_original_3_ada.sh start

# Check / tail / stop that isolated instance
./scripts/paper_original_3_ada.sh status
./scripts/paper_original_3_ada.sh tail
./scripts/paper_original_3_ada.sh stop
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
  cli.py            CLI entry points
  config.py         Strategy and infrastructure configuration
  models.py         Domain dataclasses
  data.py           Candle fetching and CSV loading
  signals.py        Z-score and entry signal generation
  engine.py         Core strategy engine
  backtest.py       Backtest and sweep orchestration
  correlation.py    Correlation analysis and validation helpers
  funding.py        Funding-rate integration
  runner.py         Paper-trading orchestration
  dashboard.py      Rich terminal UI
  execution.py      Execution adapters
  persistence.py    State save/load and trade logs
  math.py           Shared stat and PnL helpers
  validate.py       Validation gate pipeline
```

The strategy engine is mode-agnostic: it processes signals and returns orders. The backtest, paper trader, and future live system all use the same engine with different orchestrators and execution adapters.

## Development

```bash
uv run pytest                # Run tests
uv run black .               # Format
uv run ruff check .          # Lint
uv run mypy src/             # Type check
```
