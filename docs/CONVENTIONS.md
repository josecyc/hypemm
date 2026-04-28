# Repo Conventions

The repo separates **inputs** from **outputs** and **modes of execution** from
each other. The structure is mechanical so a config file and its outputs can
never drift apart.

## Directory Layout

```
configs/
├── backtest/   # historical backtests + walk-forward
├── paper/      # paper trading (no real orders)
├── testnet/    # Hyperliquid testnet (real orders, no real money)
└── live/       # ⚠ mainnet (real money)

data/
├── market/     # input data (read-only, refetchable)
│   ├── hyperliquid/             {candles,funding}/
│   └── binance_futures/
│       ├── 2y/                  {candles,funding}/
│       ├── 6y/                  {candles,funding}/
│       └── expanded/            {candles,funding}/
└── runs/       # output data (per-instance, mutable)
    ├── backtest/<config-stem>/  # backtest_summary.json, trades.csv, ...
    ├── paper/<config-stem>/     # state.json, paper_trades.csv, runner.log, ...
    ├── testnet/<config-stem>/
    └── live/<config-stem>/

docs/
├── CONVENTIONS.md   # this file
├── RUNBOOK.md       # operational procedures
├── CURRENT_STATE.md # what's running on the server right now
├── LIVE_DEPLOYMENT.md
├── METRICS.md
└── research/        # frozen research reports, dated, pinned to a commit

src/hypemm/          # the package
tests/               # mirrors src/
scripts/             # operational helpers (launch.sh, fetch_data.py, ...)
notebooks/           # analysis notebooks; consume committed CSV/JSON only
```

## Config Naming

`configs/<mode>/<strategy>_<dataset>.toml`

- `<mode>` is one of `backtest`, `paper`, `testnet`, `live`.
- `<strategy>` describes the strategy variant: `optimized_4pair`,
  `original_3pair`, `min_size_4pair`, etc.
- `<dataset>` is the data window: `2y`, `6y`, `hl` (Hyperliquid native).
  Omitted for runtime configs (paper/testnet/live use the live API directly).

## The Mechanical Mapping

For any config at `configs/<mode>/<stem>.toml`:

- **run dir** is `data/runs/<mode>/<stem>/` — derived in code, never set in
  TOML. See `derive_run_dir` in `src/hypemm/config.py`.
- **tmux/screen session name** is `hypemm-<mode>-<stem>` — derived by
  `scripts/launch.sh` from the config path.

Setting `data_dir` or `run_dir` in a TOML is a hard error. The convention
prevents two configs from fighting over the same output directory.

## What Goes In a TOML

```toml
[strategy]    # all strategy parameters
[infra]       # market_dir + execution knobs (NO data_dir / run_dir)
[gates]       # threshold gates (backtest only)
[sweep]       # parameter sweep grid (backtest only)
[risk]        # kill-switch thresholds (runtime only)
```

`[infra].market_dir` points at a `data/market/...` directory. For Binance
backtest configs, this is the relevant `<window>/` sub-directory. For HL
configs, it's `data/market/hyperliquid/`.

## Notebook Rule

Notebooks are presentation, not source of truth. A notebook should:

1. State the config it uses.
2. Read inputs only from `data/market/...` and `data/runs/...` paths.
3. Never write to ad-hoc locations — outputs go alongside the run that
   produced the inputs.

If a notebook needs an input that doesn't exist on disk, run the corresponding
backtest first; do not invent a parallel data layout inside the notebook.

## Research Reports

Each headline research claim should have a frozen report under
`docs/research/<YYYY-MM>__<title>.md` that names:

1. The config that produced it (path + commit hash).
2. The market data snapshot used (path + lookback window).
3. The exact command run.
4. The output directory containing the artifacts the claim is based on.

Reports are immutable once committed. Re-running the same study produces a
new, dated report — never an in-place edit.

## When Adding A New Instance Or Backtest

1. Create a config under the right `configs/<mode>/` directory.
2. The output dir is automatic — don't pre-create it.
3. For runtime: launch with `scripts/launch.sh start configs/<mode>/<name>.toml`.
4. Update `docs/CURRENT_STATE.md` if the instance is going to run on the
   server long-term.
