# hypemm — Metrics Framework for a Robust, Profitable Stat Arb System

## What this project actually is

**Yes — this is classic quant.** Specifically: **statistical arbitrage / pairs trading**, the same family Morgan Stanley's Tartaglia/Bamberger desk pioneered in the 1980s and that funds like D.E. Shaw, Renaissance, and PDT industrialized. We trade the **mean reversion of a price ratio** between two cointegrated-ish assets, market-neutral, with a z-score signal and correlation gate. The crypto twist: hourly bars, perp funding instead of borrow, and Hyperliquid execution risk.

We currently track: **Sharpe, win rate, $ max drawdown, monthly P&L, MAE, funding cost, hold time, orderbook depth**. That's a solid first layer — but it's the layer that lets you ship a backtest, not the layer that keeps you alive when the market regime shifts.

Below is the gold-standard set, grouped by what failure mode each one prevents.

---

## 1. Statistical validity — "is the edge real, or did I overfit?"

This is where most stat-arb strategies die before they even ship. A Sharpe of 4.93 on 7 months / 659 trades is suspicious-good — the question is whether it survives correction for multiple testing.

| Metric | What it tells you | Why you need it |
|---|---|---|
| **Probabilistic Sharpe Ratio (PSR)** | Probability your true Sharpe > some threshold given sample length, skew, kurtosis | Sharpe is a point estimate; PSR is a confidence statement. With 208 days you almost certainly need PSR ≥ 95% before sizing up. |
| **Deflated Sharpe Ratio (DSR)** | PSR adjusted for *how many strategies you tried* (Bailey & López de Prado) | We ran a 9-cell parameter sweep + pair selection. DSR is the right deflation. Without it the reported Sharpe is biased upward. |
| **Probability of Backtest Overfitting (PBO)** | % chance your "best" config ranks below median out-of-sample | The whole point of the sweep is to pick a config — PBO tells you whether the picking process is itself overfitted. |
| **Walk-forward / CPCV out-of-sample Sharpe** | Performance on data the parameters never saw | Combinatorial Purged CV (López de Prado) is the gold standard for time-series strategies; avoids leakage that vanilla k-fold has. |
| **Minimum Track Record Length (MinTRL)** | How many more months needed to confirm Sharpe ≠ 0 at 95% | Tells you when you can stop calling it "promising" and start calling it "validated". |

**Concrete next move:** compute PSR/DSR on the 7-month series; if live test continues 1 month, recompute. These have closed-form solutions, so add them to `backtest.py` next to `compute_sharpe`.

---

## 2. Tail-risk metrics — "what kills me if it goes wrong?"

The current `max_drawdown` is the *historical worst*. The risks below are the *next* worst — the one we haven't seen yet.

| Metric | What it measures |
|---|---|
| **VaR (95%, 99%)** | Loss you exceed only X% of the time — daily and per-trade |
| **CVaR / Expected Shortfall** | Average loss *in the tail beyond VaR*. **More important than VaR** because it doesn't lie about tail shape. |
| **Cornish-Fisher VaR** | VaR adjusted for **skew and kurtosis** of returns (crypto returns are very fat-tailed; Gaussian VaR underestimates by 2-5×) |
| **Skewness & excess kurtosis** of trade P&L | Stat-arb classically has *negative skew*: many small wins, occasional big losses. The 5.5:1 win/loss ratio looks like positive skew, but verify. |
| **Tail ratio** (P95 / |P5|) | Ratio of right-tail to left-tail returns; <1 means losses are bigger than wins per unit |
| **Worst single trade vs. avg trade** | Stress check: assume 3× the worst happens simultaneously on 4 pairs |
| **Conditional drawdown at risk (CDaR)** | Expected drawdown in the worst α% of cases — drawdown's CVaR analogue |
| **Maximum Adverse Excursion distribution** | We compute MAE per trade — now look at its 95th/99th percentile, not just the average |
| **Time-under-water / longest drawdown duration** | Drawdown depth doesn't tell you if you'll be in the red for 3 days or 3 months. Crypto stat arb can sit in DD for weeks. |

**Concrete next move:** simulate a "September 2025 with no correlation filter" event hitting all 4 pairs at once. That's the real left-tail scenario, and it should size the max position limits.

---

## 3. Risk-adjusted return ratios — "is this good, or just lucky-and-volatile?"

Sharpe is the floor. These are the ones a real allocator will ask about:

| Ratio | Formula | Why it matters here |
|---|---|---|
| **Sortino** | Excess return / downside deviation | Doesn't penalize upside vol. Should be ~1.5-2× Sharpe for a healthy mean-reversion strategy. |
| **Calmar / MAR** | CAGR / Max DD | Industry standard. Calmar > 3 is "good", > 5 is "very good". We're at ~$180K / $11.7K ≈ 15 — flag this as too good, likely over-fit. |
| **Ulcer Index** | RMS of % drawdowns over time | Captures *depth × duration* of drawdowns. Sharpe doesn't care if you sit underwater for 60 days; the Ulcer Index does. |
| **Pain ratio** | Return / Ulcer Index | Calmar's smarter cousin — penalizes long flat/down stretches, not just the worst point |
| **Omega ratio (at threshold τ)** | P(ret > τ) × E[ret\|>τ] / P(ret < τ) × E[\|ret\|\|<τ] | Captures full distribution, not just first two moments |
| **Gain-to-pain ratio** | Sum of gains / sum of losses | Schwager's preferred metric; > 1 is profitable, > 2 is solid |

---

## 4. Strategy-specific health — "is the *premise* of the strategy still true?"

This is the layer most retail quants skip and it's where you'll get killed first. The edge depends on **mean reversion of the spread**. If that property dies, the edge dies — and Sharpe won't tell you, only the layer below will.

| Metric | What it tells you |
|---|---|
| **ADF test p-value on the spread** (rolling) | Tests **stationarity** of the log-ratio. p > 0.05 = spread is wandering, not mean-reverting. Compute weekly per pair. |
| **Hurst exponent of the spread** (rolling) | < 0.5 = mean-reverting, = 0.5 = random walk, > 0.5 = trending. **We need < 0.5.** Fall above 0.5 → halt that pair. |
| **Half-life of mean reversion** (Ornstein-Uhlenbeck fit) | How long until the spread reverts halfway. Should be < max-hold (48h). If it drifts to 80h, the time-stops are bleeding. |
| **Cointegration test (Engle-Granger or Johansen)** | Stronger than rolling correlation — tests for a stable long-run relationship. Correlation can be high while cointegration is broken. |
| **Spread variance** (rolling) | If σ collapses, z-scores get noisy and ±2 isn't a real signal. If σ explodes, $50K is too large. |
| **Beta / hedge-ratio drift** | We implicitly use a 1:1 dollar hedge. The OLS hedge ratio of A on B should be ~1; track it and re-hedge if it drifts. This is the #1 way "market neutral" stops being market neutral. |
| **Z-score crossing frequency** | Fewer entries per month = regime change toward trending. |
| **% of time spent in valid corr regime** (>0.7) | Already tracked implicitly; make it a real-time metric with an alert. |
| **Funding-rate skew** | Persistent one-sided funding eats one leg. Track basis (perp - index) per coin. |

**Concrete next move:** add a weekly job that runs ADF + Hurst + half-life per pair and emails the table. If any pair fails ADF or Hurst > 0.5 for 2 weeks, kill that pair until it recovers. This is the single best protection against a "September AVAX" repeat.

---

## 5. Execution / live-vs-backtest divergence — "is the model lying to me?"

Live underperforms backtest ~95% of the time, almost always for the same reason: execution costs are wrong. Track these from day one of live.

| Metric | What it catches |
|---|---|
| **Implementation shortfall** = decision price − fill price (per leg, per side) | Latency cost. Hourly eval means a slow fill can cost 3-5 bps. |
| **Realized vs. assumed slippage** (we assume 2 bps maker; what is it actually?) | Maker fills aren't guaranteed — quantify how often we cross the spread. |
| **Fill rate / partial-fill rate** | If half the size doesn't fill, we're un-hedged. Track per pair. |
| **Time-to-fill** distribution | Long fills = adverse selection |
| **Maker-to-taker ratio** | If paying taker fees more than 10% of the time, the assumed cost is wrong. |
| **Backtest replay vs. live P&L delta** (per trade) | Run the same signal through the backtester at the live entry/exit timestamp — the gap is the "implementation cost". This is the single most diagnostic number. |
| **Per-trade attribution**: gross spread P&L / fees / funding / slippage | Lets you see *what's eating you* when months underperform |

---

## 6. Live monitoring & decay detection — "is the edge still working?"

Treat live trading as a continuous A/B test against the backtest distribution.

| Metric | Trigger |
|---|---|
| **Rolling 30-day Sharpe vs. backtest Sharpe** | If 30-day SR < backtest SR − 2σ, investigate |
| **Rolling 30-day win rate** | Same — bands from backtest distribution |
| **Trade frequency vs. backtest** | If expected 100 trades/month and got 30, something's blocking entries (corr filter? data gap?) |
| **Per-pair P&L vs. expected** | Catches one pair silently breaking |
| **CUSUM / Page-Hinkley change-point detection on rolling P&L** | Statistical method for detecting regime breaks before they show up in Sharpe |
| **Equity curve correlation with backtest equity curve from same period** | If the *shape* diverges, not just the level, the strategy's edge is changing |
| **System health KPIs**: data-feed lag, missed hourly evals, state corruption flags | The Apr 1-6 buffer corruption is exactly this — make it observable |
| **Capacity test**: track price impact of fills | Will tell you when scaling from $5K → $50K legs starts paying for itself |

---

## 7. Portfolio-level (across the 4 pairs)

Currently "max exposure" is calculated as 4 × $400K assuming independence. It isn't.

| Metric | Why |
|---|---|
| **Pair-trade correlation matrix** of trade returns | If LINK/SOL and SOL/AVAX both blow up together because SOL is the common leg, we're not diversified — we're concentrated in SOL. |
| **Effective N** = trace(Σ)/eigenvalue concentration | "How many independent bets do I really have?" Spoiler: probably 1.5, not 4. |
| **Net exposure per coin** (across all simultaneous trades) | Long SOL on one pair + short SOL on another = naturally hedged. Compute the net per coin and cap it. |
| **Margin / liquidation buffer** at exchange level | One liquidation kills the whole book. Track distance-to-liquidation per leg. |
| **Concentration risk**: largest pair as % of total P&L | If 80% of P&L comes from one pair, we have one strategy with one bet, not four. |

---

## Implementation priority

In order, given current state (paper trading, about to go live small):

1. **DSR + PSR** on the existing backtest — one afternoon of work, blocking before live
2. **CVaR(95%, 99%) on daily P&L + per-trade MAE distribution** — sets real position limits
3. **Rolling ADF + Hurst + half-life per pair** — strategic kill-switch
4. **Per-coin net exposure tracker** — portfolio risk, currently invisible
5. **Backtest-replay vs. live-P&L delta** — diagnoses implementation drift
6. **Calmar, Sortino, Ulcer Index** alongside Sharpe — portable to allocators / sanity checks
7. **CUSUM change-point detector** on rolling P&L — early warning before drawdown
8. **CPCV / walk-forward re-validation** if parameters change (or every 3 months)

Items 1-4 protect from being wiped by a tail event. 5-8 keep the edge alive over years.

---

## Reality check

The strategy looks promising, but the evidence so far is **one favorable backtest period and 8 days of paper trading**. Sharpe 4.93 and Calmar ~15 are *too clean* to be real out-of-sample. Most likely we have a real edge of **Sharpe 1.5-2.5** with the rest being selection bias and a quiet sample period. The metrics above let us watch that gap close (or the strategy break) in real time, with enough lead time to act.

---

## References

- [The Deflated Sharpe Ratio — Bailey & López de Prado](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)
- [The Probability of Backtest Overfitting — Bailey, Borwein, López de Prado, Zhu](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253)
- [10 Reasons Most Machine Learning Funds Fail — López de Prado](https://www.garp.org/hubfs/Whitepapers/a1Z1W0000054x6lUAA.pdf)
- [Pairs Trading: Performance of a Relative-Value Arbitrage Rule — Gatev, Goetzmann, Rouwenhorst](http://stat.wharton.upenn.edu/~steele/Courses/434/434Context/PairsTrading/PairsTradingGGR.pdf)
- [Crypto Pairs Trading: Verifying Mean Reversion with ADF and Hurst Tests — Amberdata](https://blog.amberdata.io/crypto-pairs-trading-part-2-verifying-mean-reversion-with-adf-and-hurst-tests)
- [Crypto Pairs Trading: Why Cointegration Beats Correlation — Amberdata](https://blog.amberdata.io/crypto-pairs-trading-why-cointegration-beats-correlation)
- [Hurst Exponent Anticipates Mean Reversion in Pairs Trading: Cryptocurrencies — MDPI](https://www.mdpi.com/2227-7390/12/18/2911)
- [Conditional Value at Risk / Expected Shortfall — QuantInsti](https://blog.quantinsti.com/cvar-expected-shortfall/)
- [Implementation Shortfall — Wikipedia](https://en.wikipedia.org/wiki/Implementation_shortfall)
- [Strategy Decay Detection — VertoxQuant](https://www.vertoxquant.com/p/strategy-decay-detection)
- [TCA: Detecting drift in live trading — KX](https://kx.com/blog/drift-detections-blind-spot-how-live-tca-insights-help-firms-win-the-race-against-alpha-decay/)
- [Calmar Ratio and Ulcer Index — Wallible](https://www.wallible.com/en/blog/2025-09-22-calmar-ratio-ulcer-index/)
- [Tail risk measurement in crypto-asset markets — ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S1057521920302477)
- [Cryptocurrency Tail Risk Dynamics — MDPI](https://www.mdpi.com/2674-1032/5/2/28)
- [Funding Rate Arbitrage on CEX and DEX — ScienceDirect](https://www.sciencedirect.com/science/article/pii/S2096720925000818)
