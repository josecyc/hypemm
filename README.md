# hypemm -- Cross-Perp Statistical Arbitrage on Hyperliquid

Trades mean-reversion of price ratios between correlated cryptocurrency perpetual futures on Hyperliquid. When two normally-correlated coins diverge significantly (measured by z-score), takes the opposite side and waits for convergence.

**No API keys needed** -- all data is read-only from public endpoints.

## Setup

```bash
uv sync
```

## Server access

The bot runs on a dedicated server reachable over Tailscale:

```bash
tailscale ssh dark-forest-guardian@100.91.78.8
```

All paper and live runs are managed inside a single tmux session named
`hype_mm_opt` so panes can be observed side-by-side:

```bash
tmux attach -t hype_mm_opt
```

If the session does not yet exist, create it with:

```bash
tmux new -s hype_mm_opt
```

## Live trading

Live mode places real orders against Hyperliquid mainnet. Auth is handled via
a Foundry keystore (matching the `rpo-{nb}` convention used elsewhere in the
broader stack); credentials live in a gitignored `.env` at the repo root.

1. Copy `.env.example` to `.env` and fill in `HYPERLIQUID_KEYSTORE` (e.g.
   `rpo-81`) and `RPO_KEYSTORE_PWD`. `.env` is loaded automatically by the CLI.
2. Confirm the signer address matches the wallet you funded:

   ```bash
   uv run python -c "
   from dotenv import load_dotenv; load_dotenv()
   import json, os
   from eth_account import Account
   with open(os.path.expanduser('~/.foundry/keystores/' + os.environ['HYPERLIQUID_KEYSTORE'])) as f:
       k = Account.decrypt(json.load(f), os.environ.get('HYPERLIQUID_KEYSTORE_PWD') or os.environ['RPO_KEYSTORE_PWD'])
   print('signer:', Account.from_key(k).address)
   "
   ```

3. Dry-run paper mode against the live config to validate wiring (no real
   orders placed):

   ```bash
   uv run hypemm run --config configs/live_min.toml --once --fresh
   ```

4. From inside the `hype_mm_opt` tmux session, split a new pane and launch:

   ```bash
   # Inside tmux: Ctrl-b "  (horizontal split) or Ctrl-b %  (vertical split)
   mkdir -p logs
   uv run hypemm run --config configs/live_min.toml --live --confirm-live --fresh \
     --log-file logs/live_min.log
   ```

   Or non-interactively from outside tmux:

   ```bash
   tmux split-window -t hype_mm_opt -v \
     "mkdir -p logs && uv run hypemm run --config configs/live_min.toml --live --confirm-live --fresh --log-file logs/live_min.log"
   ```

5. Watch the live dashboard in another pane:

   ```bash
   uv run hypemm dashboard --config configs/live_min.toml
   ```

### Paper and live coexistence

Paper and live runs are isolated by `data_dir` in their respective configs:

- `configs/paper_optimized.toml` → `data/paper_optimized/`
- `configs/live_min.toml`        → `data/live_min/`

Each runner writes its own `state.json`, `paper_trades.csv`, and snapshot
files. Dashboards always read from the `data_dir` of the config passed via
`--config`, so a paper dashboard can never accidentally show live trades and
vice versa. The two runners can safely poll Hyperliquid in parallel — both
respect `infra.rate_limit_sec` independently and mainnet has ample headroom
for two 60-second pollers.

`live_min.toml` is sized for the HL minimum: $25/leg × 8 legs at 5x leverage
→ $40 USDC max margin. Risk kill-switches are scaled to match (`-$3` daily
loss halt, `-$8` unrealized halt) so they actually fire at this size.

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
