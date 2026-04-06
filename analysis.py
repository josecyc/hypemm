"""Spread calculations, edge scoring, volume analysis, and market helpers."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

# ── Market pair definitions ───────────────────────────────────────────
# Hyperliquid HIP-3 RWA assets are spot pairs using "@INDEX" format.
# These are the active RWA/USDC pairs on Hyperliquid.

MARKET_PAIRS = [
    {"hl": "@182", "hl_name": "XAUT0",  "binance": "paxgusdt", "name": "Gold"},
    {"hl": "@265", "hl_name": "SLV",    "binance": None,       "name": "Silver"},
    {"hl": "@288", "hl_name": "QQQ",    "binance": None,       "name": "Nasdaq 100"},
    {"hl": "@279", "hl_name": "SPY",    "binance": None,       "name": "S&P 500"},
    {"hl": "@268", "hl_name": "AAPL",   "binance": None,       "name": "Apple"},
    {"hl": "@266", "hl_name": "GOOGL",  "binance": None,       "name": "Google"},
    {"hl": "@287", "hl_name": "META",   "binance": None,       "name": "Meta"},
]

CONTROL_PAIR = {"hl": "BTC", "hl_name": "BTC", "binance": "btcusdt", "name": "BTC (ctrl)"}


def is_spot(coin: str) -> bool:
    """Spot HIP-3 pairs start with @."""
    return coin.startswith("@")


# ── Trade statistics ──────────────────────────────────────────────────

def trade_stats(
    trades: dict,
    coin: str,
    windows: tuple[int, ...] = (60, 300, 3600),
) -> dict[int, dict]:
    """Compute trade count and avg size for multiple time windows (seconds)."""
    now_ms = int(time.time() * 1000)
    coin_trades = trades.get(coin, [])
    result = {}
    for w in windows:
        cutoff = now_ms - w * 1000
        recent = [t for t in coin_trades if t.timestamp > cutoff]
        count = len(recent)
        total_usd = sum(t.usd_value for t in recent)
        result[w] = {
            "count": count,
            "avg_size_usd": total_usd / count if count else 0.0,
            "total_usd": total_usd,
        }
    return result


def volume_imbalance(trades: dict, coin: str, window_sec: int = 300) -> float:
    """Rolling buy-volume percentage (0-100). 50 = balanced."""
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - window_sec * 1000
    recent = [t for t in trades.get(coin, []) if t.timestamp > cutoff]
    if not recent:
        return 50.0
    buy_vol = sum(t.usd_value for t in recent if t.is_buy)
    total = sum(t.usd_value for t in recent)
    return (buy_vol / total * 100) if total > 0 else 50.0


# ── Edge verdict ──────────────────────────────────────────────────────

def edge_verdict(
    hl_spread: float | None,
    ref_spread: float | None,
    trades_5m: int,
    depth_10bps: float,
) -> tuple[str, str]:
    """
    Returns (label, color).
    GREEN:  HL spread > 3x ref AND >2 trades/min AND depth < $200K
    YELLOW: HL spread > 2x ref AND >1 trade/min
    RED:    HL spread < 1.5x ref OR no trades OR depth > $1M
    For markets without a ref exchange, use absolute spread thresholds.
    """
    if hl_spread is None:
        return "NONE", "red"

    tpm = trades_5m / 5  # trades per minute

    if ref_spread is not None and ref_spread > 0:
        ratio = hl_spread / ref_spread
        if ratio > 3 and tpm > 2 and depth_10bps < 200_000:
            return "STRONG", "green"
        if ratio > 2 and tpm > 1:
            return "MODERATE", "yellow"
        if ratio < 1.5 or tpm < 0.5 or depth_10bps > 1_000_000:
            return "NONE", "red"
        return "NONE", "red"

    # No reference exchange — use absolute thresholds
    if hl_spread > 15 and tpm > 2:
        return "CHECK", "yellow"
    if hl_spread > 10 and tpm > 0.5:
        return "CHECK", "yellow"
    return "NONE", "red"


# ── CME / market schedule ────────────────────────────────────────────

def is_cme_open() -> bool:
    """
    CME Globex hours: Sunday 5 PM CT → Friday 4 PM CT.
    Daily maintenance break: 4:00–5:00 PM CT (Mon–Thu).
    """
    ct = ZoneInfo("America/Chicago")
    now = datetime.now(ct)
    wd = now.weekday()  # Mon=0 … Sun=6
    td = now.hour + now.minute / 60

    if wd == 5:              # Saturday — closed all day
        return False
    if wd == 6:              # Sunday — opens at 5 PM CT
        return td >= 17.0
    if wd == 4:              # Friday — closes at 4 PM CT for the week
        return td < 16.0
    # Mon–Thu — open except 4–5 PM CT break
    return not (16.0 <= td < 17.0)


def is_us_market_open() -> bool:
    """US stock market hours: Mon–Fri 9:30 AM – 4:00 PM ET."""
    et = ZoneInfo("America/New_York")
    now = datetime.now(et)
    wd = now.weekday()
    if wd >= 5:
        return False
    td = now.hour + now.minute / 60
    return 9.5 <= td < 16.0


# ── Alerts ────────────────────────────────────────────────────────────

def check_alerts(hl_feed) -> list[str]:
    """Return list of alert strings for current conditions."""
    alerts: list[str] = []
    now_ms = int(time.time() * 1000)

    for pair in MARKET_PAIRS:
        coin = pair["hl"]
        label = pair["hl_name"]
        book = hl_feed.books.get(coin)

        # Extreme spread
        if book:
            s = book.spread_bps()
            if s is not None and s > 30:
                alerts.append(f"⚠️  {label} spread {s:.1f} bps (>30 bps threshold)")

        # Informed flow
        imb = volume_imbalance(hl_feed.trades, coin)
        if imb > 75 or imb < 25:
            direction = "buy" if imb > 75 else "sell"
            alerts.append(
                f"⚠️  {label} volume {imb:.0f}% {direction} — informed flow warning"
            )

        # Dead market
        coin_trades = hl_feed.trades.get(coin, [])
        if coin_trades:
            last = max(t.timestamp for t in coin_trades)
            gap_ms = now_ms - last
            if gap_ms > 600_000:
                mins = gap_ms // 60_000
                if mins > 1440:
                    alerts.append(f"⚠️  {label} inactive ({mins // 1440}d since last trade)")
                elif mins > 60:
                    alerts.append(f"⚠️  {label} no trades for {mins // 60}h {mins % 60}m")
                else:
                    alerts.append(f"⚠️  {label} no trades for {mins} min")

    return alerts


# ── Formatting ────────────────────────────────────────────────────────

def format_usd(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:.0f}"
