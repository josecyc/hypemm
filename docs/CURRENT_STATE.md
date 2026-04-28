# Current State

What's running on the remote server right now. This file is a hand-curated
snapshot — update it when you change what's deployed.

## Server

```
ssh dark-forest-guardian@100.91.78.8
```

## Running Instances

Each instance has exactly one config, one screen session, one run directory,
and one log file.

| Instance | Mode | Config | Run Dir | Screen |
|---|---|---|---|---|
| `optimized_4pair` | Paper | `configs/paper/optimized_4pair.toml` | `data/runs/paper/optimized_4pair/` | `hypemm-paper-optimized_4pair` |
| `original_3pair_ada` | Paper | `configs/paper/original_3pair_ada.toml` | `data/runs/paper/original_3pair_ada/` | `hypemm-paper-original_3pair_ada` |
| `optimized_3pair` | Testnet | `configs/testnet/optimized_3pair.toml` | `data/runs/testnet/optimized_3pair/` | `hypemm-testnet-optimized_3pair` |
| `min_size_4pair` | Live (mainnet) | `configs/live/min_size_4pair.toml` | `data/runs/live/min_size_4pair/` | `hypemm-live-min_size_4pair` |

Last deployed commit, server-side: TBD after migration.

## Pending Migration

The structural cleanup of 2026-04-28 renamed every config and moved every run
directory. Until the server is migrated to match (see RUNBOOK.md "Migrating An
Instance"), the server is still running under the legacy layout.
