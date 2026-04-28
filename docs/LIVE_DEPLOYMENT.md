# Live Deployment Guide

End-to-end checklist to take the optimized 4-pair stat arb strategy from paper
to real money on Hyperliquid. **Do not skip the gating steps** — the THESIS
section 8.5 ramp is calibrated for tail-event survival.

## 1. Pre-flight checks

| Check | Required state |
|---|---|
| `hypemm walkforward` SR ≥ 1.5 OOS | Yes, currently 2.11 |
| Paper trading parity within 15% of backtest | Verify after switching to optimized config |
| `tests/` all green | Run `uv run pytest` |
| Server tmux session healthy | `ssh dark-forest-guardian@100.91.78.8 "tmux ls"` |
| Risk monitor wired in dashboard | Confirm RIsk panel renders in `hypemm run` |

## 2. Hyperliquid account setup

1. Create a fresh subaccount on Hyperliquid for this strategy. Do not reuse a
   wallet that holds other positions — cross-margin contamination is a real risk.
2. Generate an API wallet via the Hyperliquid web UI (`Account → API`). The
   API wallet's private key is what signs orders on behalf of the main account.
   Never paste the main wallet's private key into a server.
3. Whitelist the API wallet for the subaccount only.
4. Deposit USDC for ramp phase 1 (see section 5).

## 3. Environment variables

Set these on the server before starting the live runner. Do not commit them.

```bash
export HYPERLIQUID_PRIVATE_KEY="0x..."   # API wallet private key
export HYPERLIQUID_ACCOUNT="0x..."       # main subaccount address
export HYPERLIQUID_API_URL="https://api.hyperliquid.xyz"  # use testnet first
```

Place them in `~/.hypemm.env` and source it from the tmux session, not in
`.zshrc` (avoids leaking into other shells).

## 4. Live execution config

Knobs live in `configs/live/min_size_4pair.toml` under `[infra]` (defaults shown):

```toml
leverage = 5                # 5x cross-margin per coin
is_cross_margin = true
max_slippage_bps = 5.0      # abort fill if VWAP > N bps from mid
ioc_aggression_bps = 10.0   # IoC limit price = mid +/- this many bps
fill_poll_seconds = 0.5
fill_timeout_seconds = 30.0
```

Tighten `max_slippage_bps` to be conservative; loosen if your $50K legs
sweep too much depth on illiquid coins (AVAX in particular). 5 bps is a
defensible default for the THESIS pairs given the orderbook depth in
section 3.6.

## 5. Risk thresholds

Defined in `configs/live/min_size_4pair.toml` under `[risk]`. Calibrated against THESIS
section 5.3.8 (worst backtest concurrent unrealized −$19,657).

| Signal | WARN | HALT | Action on HALT |
|---|---|---|---|
| `concurrent_unrealized` | −$10K | −$15K | Block new entries |
| `daily_pnl` | −$2.5K (50% of halt) | −$5K | Block new entries for 24h |
| `win_rate_drift` | <55% on last 30 trades | — | Warn-only |
| `time_stop_drift` | >30% on last 20 trades | — | Warn-only (THESIS: reduce size) |
| `correlation_drift` | active pair corr <0.65 | — | Warn-only |

`HALT` blocks new entries only. Existing positions are still managed by the
engine's exit logic. To force-flatten, kill the runner and close manually.

## 5. Ramp schedule

From THESIS section 8.5. **Do not skip stages.**

| Phase | Per leg | Total notional | Margin (5x) | Capital | Duration | Go/no-go to next |
|---|---|---|---|---|---|---|
| Paper | $50K | $400K | — | — | 2 weeks | 11–15% gap to backtest |
| Live small | $25K | $200K | $40K | $60K | 4 weeks | WR > 60%, no anomalies |
| Live full | $50K | $400K | $80K | $120K | Ongoing | Rolling SR > 1.0 |

Edit `notional_per_leg` in `configs/live/min_size_4pair.toml` between phases. Restart
the runner.

## 6. Starting the live runner

```bash
ssh dark-forest-guardian@100.91.78.8
tmux new -s hype_mm_live
source ~/.hypemm.env
cd ~/hypemm
uv run hypemm run --config configs/live/min_size_4pair.toml --live --confirm-live
```

The `--confirm-live` flag is required and intentionally redundant — protects
against accidental real-money runs.

The dashboard title turns red in live mode. The Risk panel sits below the
signals table and color-codes each kill switch.

## 7. Operational checklist

| Cadence | Action |
|---|---|
| Every hour | Glance at the dashboard via tmux. Look for any WARN/HALT row. |
| Daily | Compare realized P&L vs backtest expectation ($170/day @ $50K legs). |
| Weekly | Re-run `hypemm walkforward --train-years 2` to confirm SR has not decayed. |
| Monthly | Reconcile Hyperliquid statement vs `paper_trades.csv` net P&L. |
| On any HALT event | Read the runner log, decide whether to flatten manually or wait for the engine to exit normally. |

## 8. Manual kill

If you need to stop everything immediately:

```bash
# Server-side
tmux send-keys -t hype_mm_live C-c

# Then close any open positions from the Hyperliquid web UI directly —
# the runner does not auto-flatten.
```

The runner saves state on Ctrl+C, so it can be resumed without losing
position context.

## 9. Testnet smoke test (do this before mainnet)

The live adapter supports both networks via `HYPERLIQUID_API_URL`. Mainnet
sigs use `source: "a"`, testnet uses `source: "b"`; the adapter switches
automatically based on the URL.

### Setup

```bash
# Get testnet USDC: https://app.hyperliquid-testnet.xyz/drip
export HYPERLIQUID_API_URL="https://api.hyperliquid-testnet.xyz"
export HYPERLIQUID_PRIVATE_KEY="0x..."   # testnet API wallet
export HYPERLIQUID_ACCOUNT="0x..."       # testnet account
```

### Run

```bash
cd ~/hypemm
uv run hypemm run \
  --config configs/testnet/optimized_3pair.toml \
  --live --confirm-live \
  --log-file data/runs/testnet/optimized_3pair/runner.log
```

### What to verify (24-48h)

| Check | How |
|---|---|
| Asset meta loads | First log line: `Fetched HL meta: N assets` |
| Leverage set per coin | `Set LINK leverage to 5x (cross)` for each pair leg |
| Orders place | First entry: 2 lines from `_post_signed`, no errors |
| Fills arrive | `_await_fill` returns within `fill_timeout_seconds` |
| Slippage stays in budget | No `ExecutionError: ... slippage > 5 bps cap` |
| Reconciliation passes restart | Kill the runner mid-position, restart, no divergence |
| Risk monitor renders | Risk panel shows OK across the board |

If any step fails on testnet, fix it before going to mainnet. Mainnet starts
at $25K/leg per phase 1 — testnet bugs cost $0, mainnet bugs cost real money.

## 10. Known limitations

- `LiveExecutionAdapter` uses **IoC limit orders** with a configurable
  aggression band (default 10 bps from mid). Realized fill must be within
  `max_slippage_bps` (default 5 bps) of mid or `ExecutionError` aborts.
  No retry-as-market fallback yet.
- The runner places legs **sequentially** (not as an HL "grouping": "normalTpsl"
  bundle). If leg A fills and leg B fails, you're temporarily unhedged. The
  reconciliation gate catches this on next startup; the kill switches catch it
  intra-run via `concurrent_unrealized`. A future improvement is to bundle.
- Funding accrual uses Hyperliquid's `fundingHistory` endpoint (hourly). On
  the actual exchange, funding is paid every hour at :00 UTC.
- The 36h max-hold and progress-exit fire on hourly boundaries only. A flash
  move can push z past stop-loss intra-hour and back without exiting. THESIS
  section 2.2 documents this trade-off.
- Reconciliation tolerance is 5% by default; smaller mismatches go silent.
  Adjust by editing `reconcile()` call in `runner.py` if you need stricter
  parity (e.g. testnet smoke).
