# Cross-Perp Statistical Arbitrage — Strategy Documentation

## Overview

Trade the mean-reversion of price ratios between correlated cryptocurrency perpetual futures on Hyperliquid. When two normally-correlated coins diverge significantly (measured by z-score), take the opposite side and wait for convergence.

## Pair Selection

### Why these 4 pairs?

Pairs were selected from the top 10 most liquid Hyperliquid perps (BTC, ETH, SOL, HYPE, XRP, DOGE, LINK, AVAX, SUI, TAO) by testing all 45 possible combinations over 7 months of hourly data (Sep 2025 - Mar 2026).

**Selected pairs (with correlation filter):**

| Pair | 7-Month Net | Daily Avg | Win Rate | Neg Months | Why Selected |
|------|-------------|-----------|----------|------------|-------------|
| LINK/SOL | $30,157 | $145 | 79% | 0/7 | Best performer, never had a losing month |
| DOGE/AVAX | $29,287 | $141 | 80% | 0/7 | Strong and consistent with corr filter |
| SOL/AVAX | $23,773 | $114 | 80% | 0/7 | Strong with corr filter (was -$13K in Sep without it) |
| BTC/SOL | $6,939 | $33 | 72% | 1/7 | Decent, adds diversification with major pair |

**Rejected pairs:**

| Pair | 7-Month Net | Why Rejected |
|------|-------------|-------------|
| ETH/SOL | $10,060 | 3 negative months out of 7, inconsistent |
| ETH/BTC | $4,921 | 3 negative months, low daily avg ($24) |
| All HYPE pairs | -$5K to -$25K | HYPE doesn't mean-revert with anything |
| All TAO pairs | Mostly negative | TAO too idiosyncratic |

### Correlation filter

Only enter trades when the 7-day (168-hour) rolling Pearson correlation of hourly returns between the two coins is above 0.7. This prevents entries during regime breakdowns when the mean-reversion assumption doesn't hold.

**Impact of the filter (7-month backtest):**

| Metric | Without Filter | With Filter |
|--------|---------------|-------------|
| Net P&L | $71,251 | $102,403 |
| Sharpe | 2.56 | 4.93 |
| Max Drawdown | $38,442 | $11,749 |
| Worst Month DD | $36,498 | $9,187 |

The filter eliminated the September 2025 drawdown ($30K loss) which was caused by AVAX pair correlation breakdowns.

## Strategy Parameters

All defined in `verification/config.py`:

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `LOOKBACK_HOURS` | 48 | Rolling window for z-score. 48h balances sensitivity vs noise. Tested 24/48/72, all profitable. |
| `ENTRY_Z` | 2.0 | Enter when z-score exceeds +/-2.0 std devs. Conservative — captures only significant divergences. |
| `EXIT_Z` | 0.5 | Exit when z-score reverts to within +/-0.5. Near-complete mean reversion. |
| `MAX_HOLD_HOURS` | 48 | Time-stop. If no reversion in 48h, cut losses. Backtest avg hold: 10-14h. |
| `STOP_LOSS_Z` | 4.0 | Cut if divergence accelerates to 4 std devs. Rarely triggered. |
| `NOTIONAL_PER_LEG` | $50,000 | Per-leg position size. Total exposure per trade: $100K. |
| `COST_PER_SIDE_BPS` | 2 | Assumed maker fee. 4 legs per round-trip = 8 bps total cost per trade. |
| `COOLDOWN_HOURS` | 2 | Wait 2 hours after exit before re-entering same pair. Prevents whipsaw. |
| `CORR_WINDOW_HOURS` | 168 | 7-day rolling window for correlation calculation. |
| `CORR_HIGH` | 0.7 | Minimum correlation to allow entry. |

### Parameter robustness

All 9 combinations of lookback [24, 48, 72] x entry_z [1.5, 2.0, 2.5] were profitable over the full 7-month backtest. The chosen parameters (48h, 2.0) are middle-of-road, not optimized to the best-performing combination.

## Capital Requirements

| Leverage | Margin Required | Annual P&L (backtest) | APR |
|----------|----------------|----------------------|-----|
| 1x | $400,000 | ~$180,000 | 45% |
| 3x | $133,000 | ~$180,000 | 135% |
| 5x | $80,000 | ~$180,000 | 225% |

Note: Higher leverage increases liquidation risk if positions move against you. The worst single trade in the backtest was -$7,319. At 5x leverage on $80K margin, that's ~9% of capital on one trade.

## How It Works

1. Every hour, compute the log price ratio for each pair: `ln(price_A / price_B)`
2. Compute the rolling mean and standard deviation over the past 48 hours
3. Compute the z-score: `(current_ratio - mean) / std_dev`
4. **Entry**: If z > +2.0, short the ratio (short coin A, long coin B). If z < -2.0, long the ratio.
5. **Entry gate**: Only enter if 7-day rolling correlation > 0.7
6. **Exit**: When |z| < 0.5 (mean reversion), or |z| > 4.0 (stop loss), or 48h held (time stop)
7. **P&L**: Long leg earns/loses on its price change, short leg earns/loses inversely. Net P&L = sum of both legs minus transaction costs.

## Known Risks

1. **Correlation breakdown**: When correlations collapse, divergences don't revert. The filter mitigates this but can't prevent entries just before a breakdown (BTC/SOL on 2026-04-01 entered at corr=0.84, which dropped to 0.06 within an hour).
2. **Regime dependence**: The strategy works best in range-bound/mean-reverting markets. Strong directional trends can cause persistent divergences.
3. **Execution risk**: Both legs must be filled near-simultaneously. A delay between fills creates directional exposure.
4. **Alpha decay**: The second half of the backtest's first month was weaker. Returns may diminish as more participants discover the pattern.

## Verification Pipeline

- `verification/fetch_data.py` — Fetch hourly candles from Hyperliquid API
- `verification/step1_backtest.py` — Extended history backtest with parameter sweep
- `verification/step1b_filtered.py` — Re-run with correlation filter
- `verification/step2_correlation.py` — Correlation stability analysis
- `verification/step3_orderbook.py` — Live orderbook depth assessment
- `verification/synthesize.py` — Final go/no-go synthesis
- `verification/paper_trade.py` — Live paper trading monitor

## Data Files

- `data/candles/` — Historical hourly candle CSVs per coin
- `data/reports/` — Backtest results, equity curves, correlation analysis
- `data/paper_trades/paper_trades.csv` — Completed paper trades
- `data/paper_trades/hourly_snapshots.csv` — Hourly state snapshots
- `data/paper_trades/state.json` — Persisted state for resume after restart
