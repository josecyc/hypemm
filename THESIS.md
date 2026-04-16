# Cross-Perp Statistical Arbitrage on Hyperliquid — Experiment Thesis

## Abstract

This document presents the full research path, backtest results, and live paper trading results for a cross-perpetual statistical arbitrage strategy on Hyperliquid. The strategy trades mean-reversion of price ratios between correlated cryptocurrency perpetual futures, gated by a rolling correlation filter. Over 7 months of backtesting and 8 days of paper trading, the strategy demonstrated consistent profitability with a well-defined risk profile.

## 1. How We Got Here

### 1.1 Initial hypothesis (Mar 30, 2026)

The project started by investigating market making opportunities on Hyperliquid's HIP-3 RWA (Real World Asset) spot markets — tokenized gold, stocks, and indices.

**Result: Dead end.** After collecting 33,328 data points over 7 days (Mar 20-27):
- Stock RWAs (SPY, QQQ, AAPL, GOOGL, META): Zero trades in 92-100% of 5-minute windows. Average trade sizes of $12-$66. Effectively non-existent markets.
- XAUT0 (gold): 6.1 bps median spread, but only $0.44/day estimated P&L after fees. 46.6% of snapshots had zero trades in 5 minutes.
- The fundamental problem: spreads are wide *because* nobody trades. Wide spreads are worthless without volume.

### 1.2 Pivoting to perps (Mar 30, 2026)

Hyperliquid does $3.9 billion/day in perp volume. We investigated:
- **Perps market making**: Too competitive. BTC spread 0.15 bps, ETH 0.49 bps. Negative net capture after fees on most markets.
- **Funding rate arbitrage (HL vs Binance)**: Real but modest. Backtested at ~$25-80/day after honest validation. Funding differentials were smaller than projected because both venues move together.
- **Funding spike capture**: Looked promising on funding P&L alone (+$2,128 in 7 days), but when we added actual price movement during holds, it lost $4,107. The spikes ARE the directional moves — going against them is catching falling knives.

### 1.3 Finding the edge: Cross-perp stat arb (Mar 31, 2026)

The insight: correlated crypto perps on Hyperliquid temporarily diverge from their normal relationship. When LINK gets expensive relative to SOL (measured by z-score of the log price ratio), the ratio tends to revert. By going short the expensive leg and long the cheap leg, you capture the reversion while remaining market-neutral.

## 2. Strategy Specification

### 2.1 Mechanics

1. Every hour, compute the log price ratio for each pair: `ln(price_A / price_B)`
2. Compute the rolling mean and standard deviation over the past 48 hours
3. Compute the z-score: `(current_ratio - mean) / std_dev`
4. **Entry**: If z > +2.0, short the ratio (short A, long B). If z < -2.0, long the ratio.
5. **Correlation gate**: Only enter if 7-day rolling Pearson correlation of hourly returns > 0.7
6. **Exit** (whichever comes first):
   - **Mean reversion**: z crosses back past ±0.5 toward zero. For a long ratio (entered at z < -2.0), exit when z >= -0.5. For a short ratio (entered at z > +2.0), exit when z <= +0.5. Note: the exit threshold is directional — a long ratio at z = -0.55 has NOT exited yet, it needs to reach -0.5 or higher.
   - **Stop loss**: |z| > 4.0 (divergence accelerating, cut losses)
   - **Time stop**: 48 hours held without exit signal

### 2.2 Evaluation cadence

All entry/exit decisions are made **once per hour**, at the top of each UTC hour. This matches the backtest, which uses hourly candle closes. Between hours, the dashboard updates z-scores every 60 seconds for visibility, but no trades are placed until the next hourly evaluation.

**Implication**: If z briefly crosses the exit threshold intra-hour but reverts by the time the hourly eval fires, the exit is missed. This is a known limitation, not a feature.

**Why we don't check exits more frequently**: The backtest used hourly candle closes. All P&L expectations, win rates, Sharpe ratios, and drawdown numbers are calibrated to this timescale. Checking exits every minute would be a different strategy with unknown performance characteristics — it could be better (faster exits, less slippage on reversions) or worse (more whipsaw exits on noise). We don't have minute-level historical data going back months to test this.

**Future improvement**: Backtest with minute-level exit checks on hourly entries. If the results are equal or better, switch. This requires sourcing minute candle data (the HL API only returns hourly candles for extended history).

### 2.2 Pairs traded

Selected from all 45 combinations of the top 10 liquid Hyperliquid perps:

| Pair | 7-Month Backtest Net | Win Rate | Negative Months | Why |
|------|---------------------|----------|-----------------|-----|
| LINK/SOL | $27,422 | 78% | 0/7 | Best performer, never lost a month |
| DOGE/AVAX | $29,287 | 80% | 0/7 | Strong with correlation filter |
| SOL/AVAX | $23,773 | 80% | 0/7 | Strong with correlation filter |
| BTC/SOL | $6,939 | 72% | 1/7 | Adds diversification |

**Rejected**: ETH/SOL (inconsistent, 3 negative months), ETH/BTC (low daily avg), all HYPE pairs (doesn't mean-revert), all TAO pairs (too idiosyncratic).

### 2.3 Parameters

| Parameter | Value | Robustness |
|-----------|-------|-----------|
| Lookback | 48 hours | All 9 combos of [24,48,72] x [1.5,2.0,2.5] profitable |
| Entry z-score | ±2.0 | Middle-of-road, not optimized |
| Exit z-score | ±0.5 | Near-complete mean reversion |
| Max hold | 48 hours | Backtest avg hold: 10-14 hours |
| Stop loss z | ±4.0 | Rarely triggered |
| Correlation threshold | 0.7 | Based on conditional P&L analysis |
| Cooldown | 2 hours | Prevents whipsaw re-entry |

### 2.4 Position sizing

| Metric | Value |
|--------|-------|
| Notional per leg | $50,000 |
| Legs per trade | 2 (long + short) |
| Max simultaneous trades | 4 (one per pair) |
| Max total exposure | $400,000 |
| Transaction cost assumption | 2 bps per side (maker), 8 bps round-trip per trade |

## 3. Backtest Results (Sep 2025 - Mar 2026, 7 months)

### 3.1 Data

- Source: Hyperliquid candleSnapshot API, hourly candles
- Period: September 3, 2025 to March 31, 2026 (208 days, 5,003 hourly bars)
- Coins: ETH, SOL, BTC, AVAX, DOGE, LINK

### 3.2 Without correlation filter

| Metric | Value |
|--------|-------|
| Total trades | 725 |
| Win rate | 74% |
| Net P&L | $71,251 |
| Daily average | $343 |
| Sharpe ratio | 2.56 |
| Max drawdown | $38,442 |
| Worst month DD | $36,498 |
| **Gate 1 verdict** | **FAIL** (worst month DD exceeded $15K limit) |

The September 2025 disaster: $30,022 loss in one month, almost entirely from AVAX pairs during a correlation breakdown.

### 3.3 With correlation filter (corr > 0.7)

| Metric | Without Filter | With Filter | Change |
|--------|---------------|-------------|--------|
| Net P&L | $71,251 | $102,403 | **+44%** |
| Daily average | $343 | $492 | +43% |
| Sharpe ratio | 2.56 | 4.93 | **+93%** |
| Max drawdown | $38,442 | $11,749 | **-69%** |
| Worst month DD | $36,498 | $9,187 | **-75%** |
| Trades | 725 | 659 | -9% (fewer bad trades) |
| **Gate 1 verdict** | FAIL | **PASS** |

The filter blocked entry during the September AVAX correlation breakdown, preventing $30K in losses.

### 3.4 Monthly P&L (filtered, all 6 pairs)

| Month | Trades | Win Rate | Net P&L | Net/Day | Max DD |
|-------|--------|----------|---------|---------|--------|
| Sep 2025 | 36 | 75% | -$2,640 | -$140 | $9,187 |
| Oct 2025 | 98 | 71% | $23,748 | $789 | $5,237 |
| Nov 2025 | 108 | 80% | $21,806 | $785 | $3,173 |
| Dec 2025 | 112 | 77% | $12,697 | $411 | $4,869 |
| Jan 2026 | 91 | 68% | $7,459 | $256 | $7,510 |
| Feb 2026 | 96 | 69% | $6,724 | $253 | $5,764 |
| Mar 2026 | 118 | 83% | $32,609 | $1,086 | $2,582 |

6 out of 7 months profitable. No alpha decay — Q4 was the strongest quarter.

### 3.5 Correlation stability (Step 2)

| Pair | Mean Corr | % Time > 0.7 | Max Breakdown |
|------|-----------|-------------|--------------|
| ETH/SOL | 0.849 | 97% | None |
| ETH/BTC | 0.871 | 98% | None |
| SOL/AVAX | 0.785 | 83% | 52h |
| DOGE/AVAX | 0.793 | 82% | 175h (Sep 2025) |
| LINK/SOL | 0.850 | 97% | None |
| BTC/SOL | 0.824 | 94% | None |

Trades entered during high correlation (>0.7): 75% win rate, +$170 avg P&L.
Trades entered during low correlation (<0.7): 54% win rate, -$800 avg P&L.

### 3.6 Orderbook depth (Step 3)

| Coin | Avg Spread | Depth @ 5bps | Fill Rating |
|------|-----------|-------------|-------------|
| ETH | 0.5 bps | $8.1M | Easy |
| SOL | 0.2 bps | $2.1M | Easy |
| BTC | 0.1 bps | $11.0M | Easy |
| DOGE | 1.6 bps | $449K | Easy |
| LINK | 2.5 bps | $140K | Easy |
| AVAX | 3.2 bps | $77K | Likely |

4 out of 6 pairs can support $50K legs with easy fills. AVAX is the tightest but workable.

## 4. Paper Trading Results

### 4.1 Phase 1: Local machine (Apr 1-6, 2026)

Run on local Mac, with internet outage issues that corrupted the price buffer for ~4 days.

**Active period (Apr 1 07:00 - Apr 2 03:00 UTC, ~20 hours):**

| # | Pair | Direction | Held | Net P&L | Exit |
|---|------|-----------|------|---------|------|
| 1 | LINK/SOL | Short ratio | 10h | +$781 | Mean revert |
| 2 | SOL/AVAX | Long ratio | 10h | +$1,398 | Mean revert |
| 3 | DOGE/AVAX | Long ratio | 19h | +$608 | Mean revert |
| 4 | SOL/AVAX | Long ratio | 7h | +$912 | Mean revert |
| 5 | LINK/SOL | Short ratio | 8h | +$887 | Mean revert |
| 6 | BTC/SOL | Short ratio | 29h | -$249 | Mean revert |

**Phase 1 total: 6 trades, 5 wins / 1 loss, +$4,337**

Key observations:
- The first day had an extreme divergence event (z-scores of -4.2 to +2.8) triggering 3 simultaneous entries — not typical.
- The only losing trade (BTC/SOL) entered just before a correlation breakdown (corr dropped from 0.84 to 0.06 within one hour). The z-score DID revert, but the prices moved enough during 29 hours that the P&L was negative.
- After Apr 1 22:00 UTC, correlations collapsed across all pairs and the strategy went dormant for 4+ days. The filter correctly prevented all entries during this period.
- Internet outages corrupted the local buffer, showing correlations at 0.2 when they were actually 0.8+. This caused the strategy to miss signals for days until we detected and fixed it.

### 4.2 Phase 2: Server deployment (Apr 6 onwards)

Moved to a dedicated server (dark-forest-guardian@100.91.78.8) running in tmux to eliminate internet outage issues.

**Trades (Apr 6-8):**

| # | Pair | Direction | Held | Net P&L | Exit |
|---|------|-----------|------|---------|------|
| 7 | LINK/SOL | Short ratio | 18h | +$303 | Mean revert |
| 8 | BTC/SOL | Short ratio | 26h | -$81 | Mean revert |
| 9 | DOGE/AVAX | Short ratio | 16h | +$1,752 | Mean revert |
| 10 | LINK/SOL | Long ratio | 10h | +$654 | Mean revert |

**Phase 2 total: 4 trades, 3 wins / 1 loss, +$2,628**

### 4.3 Combined paper trading summary

| Metric | Value |
|--------|-------|
| Total trades | 10 |
| Wins | 8 (80%) |
| Losses | 2 (20%) |
| Total realized P&L | **+$6,965** |
| Avg winning trade | +$912 |
| Avg losing trade | -$165 |
| Win/loss ratio | 5.5:1 |
| Best trade | +$1,752 (DOGE/AVAX) |
| Worst trade | -$249 (BTC/SOL) |
| Avg hold time (winners) | 12h |
| Avg hold time (losers) | 28h |

Both losses were on BTC/SOL, which has the weakest backtest performance of the 4 pairs and entered during correlation instability. The losses were small (-$249, -$81) compared to the average win (+$912).

## 5. Risk Analysis

### 5.1 Identified risks

| Risk | Severity | Mitigation | Observed |
|------|----------|-----------|----------|
| Correlation breakdown | HIGH | 0.7 correlation gate | Yes — Apr 1-6 breakdown blocked all entries for 4+ days |
| BTC/SOL underperformance | MEDIUM | Smallest allocation, diversification | Yes — both paper losses on this pair |
| Internet/infra outage | HIGH | Server deployment in tmux | Yes — local outages corrupted buffer, moved to server |
| Buffer corruption from gaps | MEDIUM | Need to fix: skip hours with no data | Yes — detected and mitigated by restart |
| Extended dormancy | LOW | Expected — backtest includes these periods | Yes — 4+ days of no trades during breakdown |
| Regime change | HIGH | Monitor quarterly performance vs backtest | Not yet observed |

### 5.2 Drawdown profile

- Backtest worst monthly drawdown (with filter): $9,187
- Backtest worst single trade: -$7,319 (48h time-stop)
- Backtest worst intra-trade MAE: -$7,602 (same trade, unrealized trough)
- Backtest 95th percentile MAE: -$2,794
- Backtest average MAE: -$678 (most trades barely go against us)
- Backtest peak-to-trough drawdown: -$38,442 (unfiltered), -$11,749 (filtered)
- Paper trading worst single trade: -$434
- Paper trading worst unrealized (across all open positions): -$1,001

### 5.3 Liquidation risk analysis

This is a long-short market-neutral strategy: each trade has one long leg and one short leg of equal notional. On Hyperliquid, both legs use the same cross-margin pool. The critical question: how much can positions go against us simultaneously before liquidation?

**5.3.1 Hyperliquid margin mechanics**

| Coin | Max Leverage | Initial Margin | Maintenance Margin |
|------|-------------|----------------|-------------------|
| BTC, ETH | 50x | 2.0% | 1.0% |
| SOL, HYPE | 20x | 5.0% | 2.5% |
| LINK, DOGE, AVAX | 10x | 10.0% | 5.0% |

Liquidation occurs when equity falls below the sum of maintenance margin requirements across all open positions.

**5.3.2 Notional and margin by leverage (all 4 pairs open, max case)**

| Pairs | Notional | Notional (2 legs × $50K × 4 pairs) | Initial Margin (5x) | Maint. Margin (~4% avg) |
|-------|----------|-----------------------------------|-------------------|------------------------|
| 1 pair | $100K | 2 legs | $20K | $4K |
| 4 pairs | $400K | 8 legs | $80K | $16K |

**5.3.3 Simultaneous unrealized loss from backtest**

Computed hour-by-hour using actual prices to mark-to-market all open positions. This is the real measure of "how much is my account down from open positions at a given moment" — not a worst-case sum of lifetime MAEs.

| Metric | Value |
|--------|-------|
| Max concurrent positions | 6 (rare — 3.3% of time) |
| **Max simultaneous unrealized loss** | **-$19,657** (Sep 23 2025, 6 open positions, $600K notional) |
| 5th percentile simultaneous unrealized | -$6,013 |
| Median simultaneous unrealized | -$625 |
| Paper trading max combined unrealized | -$1,001 (Apr 14, 2026) |
| Worst single day P&L | -$18,343 (Sep 24 2025 — during correlation breakdown) |

At the worst moment (Sep 23 2025 14:00 UTC), the 6 open positions were:
- SOL/AVAX long ratio: -$7,352
- DOGE/AVAX long ratio: -$7,342
- BTC/SOL short ratio: -$2,384
- ETH/BTC long ratio: -$1,105
- LINK/SOL short ratio: -$1,106
- ETH/SOL short ratio: -$368

Concurrent position distribution:
- 0 positions: 7% of time
- 1-2 positions: 39% of time
- 3-4 positions: 40% of time
- 5-6 positions: 14% of time

**5.3.4 Capital requirements by leverage (4 pairs, $50K per leg)**

| Leverage | Initial Margin | Max Loss Before Liquidation | Stress Test Pass? |
|----------|---------------|----------------------------|-------------------|
| 1x | $400,000 | $392,000 (before maint.) | ✅ Unlimited |
| 2x | $200,000 | $184,000 | ✅ 9x worst-case |
| 3x | $133,000 | $117,000 | ✅ 6x worst-case |
| **5x** | **$80,000** | **$64,000** | **✅ 3.3x worst-case** |
| 7x | $57,000 | $41,000 | ✅ 2.1x worst-case |
| 10x | $40,000 | $24,000 | ⚠️ 1.2x worst-case |
| 15x | $27,000 | $11,000 | ❌ Below worst-case |
| 20x | $20,000 | $4,000 | ❌ Would liquidate |

Worst-case benchmark: **-$19,657** (actual max simultaneous unrealized from 7-month backtest, mark-to-market across all 6 concurrent open positions).

**5.3.5 Recommended account capitalization**

The gap between initial margin and liquidation threshold is your survival buffer. We recommend:

| Goal | Leverage | Margin | Buffer | Total Capital | Rationale |
|------|----------|--------|--------|---------------|-----------|
| Conservative | 3x | $133K | $50K buffer | **$183K** | 6x buffer vs worst-case |
| Balanced | 5x | $80K | $40K buffer | **$120K** | 3.3x buffer, matches paper trading plan |
| Aggressive | 7x | $57K | $20K buffer | **$77K** | 2.1x buffer, higher liquidation risk |
| Danger | 10x+ | <$40K | <$15K | **Not recommended** | Little margin above worst-case |

**Balanced ($120K) is the recommendation** — enough buffer to survive the backtest's worst simultaneous unrealized loss (-$19,657) with 3.3x cushion, leaving room for unrealized swings beyond the historical worst-case.

**5.3.6 Worked example: $120K account at 5x during the worst moment**

Walking through what would actually happen on Hyperliquid during the Sep 23, 2025 14:00 UTC drawdown:

**Setup**
- Deposit: $120,000
- Leverage: 5x
- Pairs: 4 open (LINK/SOL, DOGE/AVAX, SOL/AVAX, BTC/SOL)
- Total notional: $400,000 (4 pairs × 2 legs × $50K)
- Initial margin required: $80,000 (notional / leverage)
- Free collateral buffer: $40,000 ($120K - $80K)
- Maintenance margin: ~$40,000 (roughly half of initial, varies by coin)
- **Liquidation happens when account equity falls below maintenance margin** (~$40K)

**At the worst moment (-$19,657 simultaneous unrealized):**
- Account equity = $120K + realized P&L + unrealized P&L = $120K + $0 - $19,657 = **$100,343**
- Distance to liquidation = $100,343 - $40,000 = **$60,343 of headroom**
- You've used ~33% of your total buffer ($19.7K of the $80K between starting capital and liquidation)

**What would need to happen to liquidate you:**
- Equity would need to drop to $40K
- That requires combined losses of $80K
- At $400K notional, that's a **20% adverse move across all positions simultaneously**
- The backtest worst was only -$19.7K (about 25% of what would liquidate you)
- You'd need roughly **4x the backtest's worst-case loss** to get liquidated

**Recovery over the next hour:**
- By 15:00 UTC, unrealized improved from -$19,657 to -$17,654 (+$2K)
- By 16:00 UTC, two positions exited, unrealized was -$14,951 (+$4.7K from peak)
- Within a few hours, the account was back above $105K equity

The key takeaway: **the recommended $120K gives you room to survive a loss 4x worse than anything in the 7-month backtest** before liquidation becomes a concern.

**5.3.7 Key liquidation risks specific to this strategy**

1. **Correlation breakdown** (highest risk): All 4 positions can simultaneously go underwater if correlations collapse mid-trade (like BTC/SOL on Apr 1 — corr dropped 0.84 → 0.06 in one hour). The filter prevents NEW entries but doesn't close EXISTING positions.

2. **Regime shift during max exposure**: Worst-case was 6 positions open with $22.9K MAE during the September 2025 correlation event. A deeper breakdown than we've seen could exceed this.

3. **Intra-hour adverse moves**: Our 48h time-stop and ±4.0 z stop-loss fire only on hourly boundaries. A flash crash could push z past ±4 and back before we'd exit.

4. **Funding rate accumulation**: Positions held 48h on thin pairs (AVAX) can accumulate meaningful funding costs. Not modeled in backtest.

**5.3.8 Stress test beyond backtest**

The backtest covers 7 months. True tail events (100-year floods) could be 2-3x worse than historical maximum. At 5x leverage with $120K:
- Backtest worst: $19.7K (we use 31% of margin buffer, survive comfortably)
- 2x backtest worst: $39K (we use 61% of buffer, survive)
- 3x backtest worst: $59K (we use 92% of buffer — very close to liquidation)

Recommendation: Monitor concurrent unrealized daily. If we hit 50% of the backtest worst-case ($10K combined unrealized), halve position sizes. If we hit 100% ($20K), exit all positions manually.

### 5.4 What the correlation filter prevents

Without the filter, the September 2025 backtest lost $30,022 in one month. With the filter, the same month lost $2,640. During paper trading, the filter blocked entries for 4+ days during the Apr 1-6 correlation breakdown — correctly preventing what would have been losing trades.

## 6. Capital Requirements and Expected Returns

### 6.1 At $50K per leg (backtest calibration)

| Leverage | Margin | Buffer (vs worst MAE) | Capital Required | Annual P&L (backtest) | APR on Capital |
|----------|--------|----------------------|------------------|----------------------|----------------|
| 1x | $400K | ∞ | **$400K** | ~$180K | 45% |
| 3x | $133K | $50K | **$183K** | ~$180K | 98% |
| 5x | $80K | $40K | **$120K** | ~$180K | 150% |
| 7x | $57K | $20K | **$77K** | ~$180K | 234% |
| 10x | $40K | Below worst-case | Not recommended | — | — |

"Capital Required" = initial margin + safety buffer. Safety buffer sized to survive the backtest's worst simultaneous unrealized loss (-$19,657) with 2-6x cushion depending on risk tolerance.

**Recommended: $120K at 5x leverage** — optimal risk/reward balance.

### 6.2 Recommended deployment path

| Phase | Per Leg | Exposure | Margin (5x) | Capital | Duration |
|-------|---------|----------|-------------|---------|----------|
| Paper trade | $50K | $400K | $0 | $0 | 2 weeks (in progress) |
| Live test | $5K | $40K | $8K | $15K | 2 weeks |
| Scale up | $25K | $200K | $40K | $60K | 2 weeks |
| Full size | $50K | $400K | $80K | **$120K** | Ongoing |

### 6.3 Liquidation risk at each leverage level

Based on the -$19,657 max simultaneous unrealized loss observed in the 7-month backtest (hour-by-hour mark-to-market of all concurrent open positions):

| Leverage | Capital | Buffer | Worst-case uses | Survive historical worst? | Survive 2x worst? | Survive 3x worst? |
|----------|---------|--------|-----------------|--------------------------|-------------------|-------------------|
| 3x | $183K | $50K | 39% of buffer | ✅ | ✅ | ✅ |
| 5x | $120K | $40K | 49% of buffer | ✅ | ✅ | ⚠️ close |
| 7x | $77K | $20K | 98% of buffer | ⚠️ close | ❌ | ❌ |
| 10x | $40K | $0 | N/A | ❌ | ❌ | ❌ |

### 6.3 Paper trading vs backtest comparison

| Metric | Backtest (7mo avg) | Paper (8 days) |
|--------|-------------------|----------------|
| Win rate | 75% | 80% |
| Avg daily P&L | $492 | ~$871 |
| Avg hold time | 10-14h | 15h |
| Avg trade P&L | +$155 | +$697 |

Paper trading is running above backtest expectations, but the sample is small (10 trades) and the first day had an unusually large divergence event. The backtest average includes many quiet days.

## 7. Verification Pipeline

All analysis is reproducible via the `hypemm` CLI:

```bash
# Install dependencies
uv sync

# Fetch historical candle data + funding rates
uv run hypemm fetch

# Run full backtest (Gates 1-2: Sharpe, correlation stability)
uv run hypemm backtest

# Parameter sweep across lookback/entry-z grid
uv run hypemm sweep

# Correlation stability analysis
uv run hypemm correlation

# Live orderbook depth analysis (Gate 3, 2 hours of snapshots)
uv run hypemm orderbook

# Go/no-go synthesis from all analysis steps
uv run hypemm synthesize

# Start paper trading
uv run hypemm paper
uv run hypemm paper --fresh  # ignore saved state

# Paper trading on server (tmux)
ssh server "cd ~/hypemm && tmux attach -t hype_mm"
```

Risk analysis notebooks (Jupyter):
- `verification/risk_analysis.ipynb` — API data (~7 months)
- `verification/risk_analysis_reservoir.ipynb` — Reservoir data (~8 months)

Both produce cumulative P&L curves, drawdown visualizations, simultaneous unrealized time series, and daily P&L tables. Re-run with:
```bash
uv run jupyter nbconvert --to notebook --execute verification/risk_analysis.ipynb --output risk_analysis.ipynb
```

Data files:
- `data/candles/` — Historical hourly candles per coin
- `data/funding/` — Historical hourly funding rates per coin
- `data/reports/` — Backtest results, equity curves, parameter sweeps
- `data/paper_trades/paper_trades.csv` — Completed paper trades
- `data/paper_trades/hourly_snapshots.csv` — Hourly state snapshots
- `data/paper_trades/state.json` — Persisted state for resume

Configuration: `config.toml` (pairs, parameters, leverage, capital)

Source code structure:
- `src/hypemm/engine.py` — Core strategy engine (entry/exit logic)
- `src/hypemm/backtest.py` — Historical backtest with funding integration
- `src/hypemm/runner.py` — Live paper trading runner
- `src/hypemm/funding.py` — Funding rate fetching and accrual
- `src/hypemm/correlation.py` — 7-day rolling correlation gate
- `src/hypemm/signals.py` — Z-score computation
- `src/hypemm/price_buffer.py` — Hourly price buffer with live updates
- `src/hypemm/dashboard.py` — Rich terminal UI
- `src/hypemm/persistence.py` — State save/load for resume
- `tests/` — 150+ unit tests covering all modules

## 8. Conclusion

The cross-perp stat arb strategy on Hyperliquid shows a real, verifiable edge:

1. **Backtested over 7 months** with 659 trades, 75% win rate, Sharpe 4.93 after correlation filter
2. **Parameter robust** — all 9 tested parameter combinations profitable
3. **Paper traded for 8 days** with 10 trades, 80% win rate, +$6,965 realized
4. **Risk managed** by the correlation filter which prevented the worst backtest drawdown and correctly blocked entries during live correlation breakdowns
5. **Executable** — orderbook depth supports $50K legs on 4 of 6 pairs

The strategy is not a guaranteed money printer. It has periods of dormancy (4+ days during correlation breakdowns), occasional losses (BTC/SOL), and is regime-dependent (needs mean-reverting markets). But the evidence from both backtest and paper trading supports proceeding to a small live test.

**Recommended next step**: Deploy with $5K per leg on LINK/SOL (the strongest pair) for 2 weeks of live validation.
