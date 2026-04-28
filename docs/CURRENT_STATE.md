# Current State

What's running on the remote server right now. This file is a hand-curated
snapshot — update it when you change what's deployed.

## Server

```
ssh dark-forest-guardian@100.91.78.8
```

## Running Instances

Each instance has exactly one config, one tmux session, one run directory,
and one log file. Live always runs alongside its paper twin (same stem).

| Instance | Mode | Config | Run Dir | Tmux Session |
|---|---|---|---|---|
| `min_size_4pair` (live) | Live (mainnet) | `configs/live/min_size_4pair.toml` | `data/runs/live/min_size_4pair/` | `hypemm-live-min_size_4pair` |
| `min_size_4pair` (paper twin) | Paper | `configs/paper/min_size_4pair.toml` | `data/runs/paper/min_size_4pair/` | `hypemm-paper-min_size_4pair` |

## Legacy Sessions (Untouched)

Kept running for historical continuity; not part of the unified `hypemm run`
flow.

- `hype_mm:0.0` — `verification.paper_trade --fresh` (predates the unified
  package). State in `data/paper_trades/`.

## Archived Run Dirs

Preserved on disk but not relaunched.

- `data/runs/paper/_legacy_50k_optimized/` — 21+ days of paper trades at
  $50K/leg from `paper_optimized.toml` before the live+paper-twin
  reorganization on 2026-04-28.
- `data/runs/paper/_legacy_default/` — orphan state.json from the old default
  `data/paper_trades/` location.
- `data/runs/testnet/optimized_3pair/` — testnet smoke-test artifacts; no
  active runner.

Last deployed commit, server-side: TBD after server migration.
