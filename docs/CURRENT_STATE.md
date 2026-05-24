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
- `data/runs/paper/_legacy_min_size_4pair_pre_live/` — 2 days of paper trades
  at $25/leg run 2026-04-28 → 2026-04-30, before paper was reset to align
  start time with the mainnet live twin for a clean head-to-head.
- `data/runs/testnet/optimized_3pair/` — testnet smoke-test artifacts; no
  active runner.

Last deployed commit, server-side: `4c17dba` on `accounting-overhaul`
(restarted 2026-05-24 17:32 UTC). The runner was stopped, two stuck
positions (LINK/SOL, SOL/AVAX) were cleared from `state.json` after
diagnosing a multi-pair coin-netting bug: non-`reduceOnly` entries on
shared coins (e.g. DOGE/AVAX SHORT_RATIO buying AVAX while SOL/AVAX
LONG_RATIO was short AVAX) silently crossed each other on HL, and
subsequent `reduceOnly` closes then rejected with "would increase
position". `4c17dba` drops `reduceOnly` on closes and adds a runtime
reconcile loop that halts trading if engine state diverges from HL.
Realized cost of the incident was −$2 (16 fills' worth of closedPnl + fees).

Spot/perp note: this account is in HL "unified account" mode — the
legacy `clearinghouseState.accountValue` reads `0` but margin actually
comes from the unified pool, and orders fill normally against it. Do
not interpret `accountValue: 0` as "out of funds"; cross-check with
`spotClearinghouseState` and the HL UI's Total Equity.

Prior incident (2026-05-12): a stuck DOGE/ADA position whose close
legs filled on HL on 2026-05-10 but whose `confirm_exit` was suppressed
by a then-fatal slippage cap. Fixed by `c1da297` + `d1c8289`
(slippage-cap-to-warning).
