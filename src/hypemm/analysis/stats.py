"""Statistical analysis for completed trades: monthly P&L, Sharpe, drawdown."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from hypemm.models import CompletedTrade


def monthly_breakdown(trades: list[CompletedTrade]) -> list[dict[str, object]]:
    """Compute monthly P&L breakdown from completed trades."""
    if not trades:
        return []

    by_month: dict[str, list[CompletedTrade]] = {}
    for t in trades:
        dt = datetime.fromtimestamp(t.exit_ts / 1000, tz=timezone.utc)
        key = dt.strftime("%Y-%m")
        by_month.setdefault(key, []).append(t)

    results = []
    for month in sorted(by_month):
        mtrades = by_month[month]
        nets = [t.net_pnl for t in mtrades]
        gross = sum(t.gross_pnl for t in mtrades)
        costs = sum(t.cost for t in mtrades)
        net = sum(nets)
        wins = sum(1 for t in mtrades if t.net_pnl > 0)
        max_dd = _intra_period_drawdown(nets)

        n_days = max(1, (mtrades[-1].exit_ts - mtrades[0].entry_ts) / 86_400_000)
        results.append(
            {
                "month": month,
                "trades": len(mtrades),
                "win_rate": wins / len(mtrades) * 100,
                "gross": gross,
                "costs": costs,
                "net": net,
                "net_per_day": net / n_days,
                "max_dd": max_dd,
            }
        )

    return results


def compute_sharpe(trades: list[CompletedTrade]) -> float:
    """Annualized Sharpe ratio from daily P&L."""
    daily = _daily_pnl_dict(trades)
    values = list(daily.values())
    if len(values) < 5:
        return 0.0

    mean = float(np.mean(values))
    std = float(np.std(values, ddof=1))
    if std < 1e-10:
        return 0.0

    return mean / std * float(np.sqrt(365))


def max_drawdown(trades: list[CompletedTrade]) -> float:
    """Maximum drawdown in dollars from peak equity."""
    daily = _daily_pnl_dict(trades)
    if not daily:
        return 0.0

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0

    for day in sorted(daily):
        cumulative += daily[day]
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    return max_dd


def daily_equity(trades: list[CompletedTrade]) -> list[dict[str, object]]:
    """Build a daily equity curve from trades."""
    daily = _daily_pnl_dict(trades)
    daily_trades: dict[str, int] = {}
    for t in trades:
        dt = datetime.fromtimestamp(t.exit_ts / 1000, tz=timezone.utc)
        day = dt.strftime("%Y-%m-%d")
        daily_trades[day] = daily_trades.get(day, 0) + 1

    rows = []
    cumulative = 0.0
    for day in sorted(daily):
        cumulative += daily[day]
        rows.append(
            {
                "date": day,
                "daily_pnl": round(daily[day], 2),
                "cumulative_pnl": round(cumulative, 2),
                "num_trades": daily_trades.get(day, 0),
            }
        )
    return rows


def _daily_pnl_dict(trades: list[CompletedTrade]) -> dict[str, float]:
    """Aggregate trade P&L by exit date."""
    daily: dict[str, float] = {}
    for t in trades:
        dt = datetime.fromtimestamp(t.exit_ts / 1000, tz=timezone.utc)
        day = dt.strftime("%Y-%m-%d")
        daily[day] = daily.get(day, 0) + t.net_pnl
    return daily


def _intra_period_drawdown(pnl_sequence: list[float]) -> float:
    """Max drawdown within a sequence of P&L values."""
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnl_sequence:
        cumulative += pnl
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    return max_dd
