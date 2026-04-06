# hypemm — Hyperliquid RWA Market Making Edge Detector

Real-time monitoring tool that compares Hyperliquid HIP-3 RWA spot orderbook spreads against Binance equivalents to identify market making opportunities.

**No API keys needed** — all data is read-only from public endpoints.

## Setup

```bash
pip install -r requirements.txt
python monitor.py
```

## What it monitors

HIP-3 spot pairs on Hyperliquid (USDC denominated):

| Pair | Token | Binance Reference | Asset |
|---|---|---|---|
| @182 | XAUT0 | PAXGUSDT | Gold |
| @265 | SLV | — | Silver |
| @288 | QQQ | — | Nasdaq 100 |
| @279 | SPY | — | S&P 500 |
| @268 | AAPL | — | Apple |
| @266 | GOOGL | — | Google |
| @287 | META | — | Meta |
| BTC | BTC (perp) | BTCUSDT | Control |

## Metrics

- **Spread (bps)** — bid-ask spread on both venues
- **Ratio** — HL spread / Binance spread (higher = more MM edge)
- **Edge** — theoretical max capture in bps
- **Book depth** — USD liquidity at 5/10/25/50 bps from mid
- **Trade frequency** — rolling 1m/5m/60m trade counts
- **Volume imbalance** — buy/sell ratio (informed flow detector)
- **Funding rate** — directional bias indicator (perps only)

## Edge verdict

- 🟢 **STRONG**: HL spread > 3x reference, >10 trades/min, depth < $200K
- 🟡 **MODERATE/CHECK**: HL spread > 2x reference, >5 trades/min (or >15 bps absolute)
- 🔴 **NONE**: Spread ratio < 1.5x or too few trades or too deep

## CSV logging

Metrics are logged every 30 seconds to `edge_log_YYYYMMDD.csv` for historical pattern analysis.

## Architecture

- `monitor.py` — main entry point, startup summary, orchestration
- `feeds/hyperliquid.py` — HL WebSocket + REST (L2 books, trades, metadata)
- `feeds/binance.py` — Binance futures WebSocket (depth snapshots)
- `analysis.py` — spread calcs, edge scoring, market hours, alerts
- `display.py` — Rich terminal UI
- `logger.py` — CSV logging
