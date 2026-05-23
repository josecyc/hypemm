# Multi-pair coin-netting fix

Status: in implementation. Branch: `multi-pair-netting`.

## Problem

The 4-pair stat arb live runner stalled with 2 phantom positions in
`state.json` (LINK/SOL, SOL/AVAX) that have no corresponding positions on HL.
Account `0x247B76dd263Fc61eB7ab87e3B13d1eA2B294187A` is at `accountValue=0`
with `assetPositions=[]`, yet the engine still treats both pairs as open
and tries to close them every hour. Every close attempt now fails with
`"Reduce only order would increase position"`.

## Root cause

Two distinct bugs combined.

**Bug A — non-`reduceOnly` entry crossings.** HL nets positions per coin
across the account. When pair B's entry order opens a position in a coin
where pair A already holds the opposite direction, HL fills it as a single
order that *closes* pair A's leg and opens pair B's leg. The engine never
hears about it and remains certain pair A is still open. The actual fill
that started this incident: 2026-05-13 17:00:54, `AVAX buy 2.56
dir="Short > Long" startPos=-2.55 → +0.01` — DOGE/AVAX SHORT_RATIO entry
crossed SOL/AVAX LONG_RATIO's AVAX short.

**Bug B — all-or-nothing close path.** Once SOL/AVAX's AVAX leg was gone
from HL, every close attempt did: leg A (sell SOL `reduceOnly`) succeeded
and reduced the SOL position; leg B (buy AVAX `reduceOnly`) rejected. The
except-block at `execution.py:396` ran `_flatten_position` on leg A (also
`reduceOnly`, also rejected) and re-raised. The engine got `ExecutionError`
and never updated state. The SOL leg was slowly peeled to zero over
multiple close attempts, eventually consuming LINK/SOL's SOL leg too.

The earlier `c1da297` slippage-cap fix addressed an orthogonal orphan
path. This is a different bug.

## Architecture choice

Three options were considered:

1. **Strategy-level entry guard.** Refuse to open a pair whose leg would
   cross another pair's exposure on a shared coin. Simple, deterministic,
   backtest-identical. Cost: portfolio capacity drops — many legitimate
   stat-arb signal combinations get suppressed.
2. **Sub-accounts per pair.** Each pair lives in its own HL sub-account so
   per-pair positions are real in HL. Cleanest match between engine and
   exchange models. Blocked: HL gates sub-accounts behind ~$100k cumulative
   trading volume; we don't qualify yet.
3. **Signed-delta on a single account.** Engine tracks per-pair positions
   for accounting; orders go out at per-pair leg sizes without
   `reduceOnly`; HL just integrates the signed deltas. Per-coin net at HL
   equals the sum of per-pair signed sizes by construction.

Chosen: **option 3**, gated by **periodic mid-run reconciliation** as the
replacement tripwire for `reduceOnly`. Revisit option 2 once sub-accounts
unlock.

## How signed-delta works (worked example)

State at the moment of the bug, under the new architecture:

| Step | Order | HL effect | Engine pair state |
|---|---|---|---|
| SOL/AVAX enters LONG_RATIO | BUY SOL 0.27, SELL AVAX 2.55 (no `reduceOnly`) | SOL +0.27, AVAX −2.55 | SOL/AVAX opened with those sizes |
| DOGE/AVAX enters SHORT_RATIO | SELL DOGE x, BUY AVAX 2.56 (no `reduceOnly`) | AVAX −2.55 → +0.01 (HL crosses) | DOGE/AVAX opened |
| Engine's expected net AVAX | — | +0.01 ✓ matches HL | — |
| SOL/AVAX closes | SELL SOL 0.27, BUY AVAX 2.55 (no `reduceOnly`) | SOL 0, AVAX +2.56 | SOL/AVAX cleared |
| Engine's expected net AVAX | — | +2.56 ✓ still matches HL | DOGE/AVAX still long 2.56 AVAX |

Per-pair P&L is computed from per-pair entry/exit prices recorded against
each pair. HL only sees the net; the engine's per-pair accounting is
internal bookkeeping. The two stay consistent because the deltas the
engine sends to HL sum to exactly the change in expected per-coin net.

## Implementation outline

1. **`execution.py`** — drop `reduceOnly` everywhere it's currently set.
   Concretely:
   - `LiveExecutionAdapter._place_ioc` no longer takes a `reduce_only`
     parameter; orders go out with `"r": False`.
   - `_flatten_position` no longer sends `"r": True`. On close-path
     leg-B failure the flatten is now a normal opposite-direction order
     that restores the leg-A position (vs the broken `reduceOnly` flatten
     that always rejects).
   - The `is_close` plumbing through `get_fill_prices` is kept — it still
     flips leg directions and gates the sub-$10 pre-flight check. Just no
     more `reduceOnly`.
2. **`runner.py`** — call `reconcile` after every `process_bar` (i.e. on
   each `hour_changed`), not just at startup. On any divergence > the
   existing 5% tolerance: set a new `engine.halt_trading=True` flag, log
   loudly, save state. The runner already sets `halt_entries`; the new
   flag suppresses *both* entries and exits because we don't know which
   side of the state mismatch is truth.
3. **`engine.py`** — add `halt_trading: bool = False`. `process_bar`
   returns an empty list when set. Document that `halt_trading` is the
   "manual intervention required" stop, distinct from the risk-driven
   `halt_entries`.
4. **`reconcile.py`** — no changes to the divergence math; expose a
   reusable `check_drift` that wraps `reconcile` for the runtime path.
5. **Tests** — failing-first:
   - close orders go out with `"r": False`
   - close-path leg-A flatten uses `"r": False` and sells/buys to restore
     leg A's pre-close size
   - `reconcile` called mid-run sets `halt_trading` on drift
   - happy path: two pairs with overlapping coins in opposite directions
     both open, both close, engine and HL net stay consistent
   - regression: existing entry-path leg-A flatten on entry-leg-B failure
     still works (no longer `reduceOnly` but still closes leg A)

## What changes vs what stays

- `OpenPosition` schema **unchanged**. Closes are still atomic-attempt: if
  leg B fails after leg A succeeded, leg A gets flattened (now reliably,
  without `reduceOnly`) and engine state stays open for retry next bar.
  Round-trip cost on the rare partial failure (~2× taker fee + slippage)
  is acceptable; the alternative — half-closed position schema — is
  larger surface for a rare failure mode.
- Backtest and paper paths **unchanged**. They never used `reduceOnly`.
- Per-pair P&L bookkeeping **unchanged**. Per-pair entry/exit prices,
  fees, and funding are still attributed to the pair leg as today.
- `tests/test_paired_configs.py` and `tests/test_repo_structure.py`
  invariants must continue to hold.

## Risks

- **No more `reduceOnly` tripwire.** A bug in engine sizing or a stale
  position turns into a real HL order in the wrong direction.
  Reconciliation is the replacement tripwire. Tolerance is the existing
  5%; the divergence window is one bar.
- **Reconciliation latency.** One bar (60s at current `poll_interval_sec`
  but only acted on at `hour_changed`) between drift and detection. For
  hourly stat-arb signals this is acceptable; faster cadence would add
  HL API load without much benefit.
- **Partial-close round-trip.** When leg A closes and leg B fails, the
  flatten round-trip costs ~9 bps + slippage. Empirically rare; if it
  becomes frequent we'd add per-leg-closed schema.

## Out of scope

- Sub-accounts (gated by HL volume).
- Auto-recovery from divergence (operator manually flattens + restarts).
- Strategy-level entry guard (no longer needed — signed-delta allows the
  conflicting signals to coexist).

## Cleanup of the current stuck state (separate from the code change)

1. Stop the live runner.
2. Back up `state.json` and `paper_trades.csv` with ISO timestamp suffix.
3. Edit `state.json` to set LINK/SOL and SOL/AVAX positions to `null`.
   (User chose: do not backfill close trades into `paper_trades.csv`.)
4. Verify all four pairs read `flat`.
5. Deploy the structural fix.
6. User refunds USDC on HL.
7. Restart runner with the fixed code.
