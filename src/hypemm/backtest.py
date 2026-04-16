"""Backtest runner, parameter sweep, and trade statistics."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from hypemm.config import GateConfig, StrategyConfig, SweepConfig
from hypemm.engine import StrategyEngine
from hypemm.funding import compute_funding_cost
from hypemm.math import (
    compute_leg_pnl,
    compute_log_ratios,
    compute_z_scores,
    rolling_correlation,
)
from hypemm.models import (
    BacktestResult,
    CompletedTrade,
    EntryOrder,
    ExitOrder,
    GateResult,
    PairConfig,
    Signal,
    SweepRow,
)

logger = logging.getLogger(__name__)


# -- Backtest --


def run_backtest(
    prices: pd.DataFrame,
    pair: PairConfig,
    config: StrategyConfig,
    funding: pd.DataFrame | None = None,
) -> list[CompletedTrade]:
    """Run full backtest on one pair using the strategy engine.

    If funding is provided, deducts per-hour funding cost from each trade's net_pnl.
    """
    pa = prices[pair.coin_a].values
    pb = prices[pair.coin_b].values
    timestamps = prices.index
    n = len(pa)

    if n < config.lookback_hours + 10:
        return []

    log_ratios = compute_log_ratios(np.asarray(pa), np.asarray(pb))
    z_scores = compute_z_scores(log_ratios, config.lookback_hours)
    corr_values = _compute_rolling_corr(pa, pb, config.corr_window_hours)

    funding_a = funding[pair.coin_a] if funding is not None else None
    funding_b = funding[pair.coin_b] if funding is not None else None

    engine = StrategyEngine(replace(config, pairs=(pair,)))
    completed: list[CompletedTrade] = []

    for i in range(config.lookback_hours + 1, n):
        z = z_scores[i]
        if np.isnan(z):
            continue

        corr = corr_values[i] if not np.isnan(corr_values[i]) else None
        ts_ms = int(timestamps[i].timestamp() * 1000)

        signal = Signal(
            pair=pair,
            z_score=float(z),
            correlation=corr,
            price_a=float(pa[i]),
            price_b=float(pb[i]),
            timestamp_ms=ts_ms,
            n_bars=i + 1,
        )

        orders = engine.process_bar({pair.label: signal}, ts_ms)

        for order in orders:
            if isinstance(order, EntryOrder):
                engine.confirm_entry(order, float(pa[i]), float(pb[i]), ts_ms)
            elif isinstance(order, ExitOrder):
                trade = engine.confirm_exit(order, float(pa[i]), float(pb[i]), ts_ms)
                trade = _add_mae(trade, pa, pb, i, engine.config)
                if funding_a is not None and funding_b is not None:
                    fc = compute_funding_cost(
                        trade.direction,
                        config.notional_per_leg,
                        trade.entry_ts,
                        trade.exit_ts,
                        funding_a,
                        funding_b,
                    )
                    trade = replace(trade, funding_cost=fc, net_pnl=trade.net_pnl - fc)
                completed.append(trade)

    return completed


def run_backtest_all_pairs(
    prices: pd.DataFrame,
    config: StrategyConfig,
    funding: pd.DataFrame | None = None,
) -> list[CompletedTrade]:
    """Run backtest across all configured pairs."""
    all_trades: list[CompletedTrade] = []
    for pair in config.pairs:
        trades = run_backtest(prices, pair, config, funding=funding)
        all_trades.extend(trades)
        wins = sum(1 for t in trades if t.net_pnl > 0)
        net = sum(t.net_pnl for t in trades)
        wr = wins / len(trades) * 100 if trades else 0
        logger.info(
            "%s: %d trades, %.0f%% WR, $%+.0f",
            pair.label,
            len(trades),
            wr,
            net,
        )
    return all_trades


def summarize_backtest(
    trades: list[CompletedTrade],
    prices: pd.DataFrame,
) -> BacktestResult:
    """Build a BacktestResult from completed trades."""
    net = sum(t.net_pnl for t in trades)
    wr = sum(1 for t in trades if t.net_pnl > 0) / len(trades) * 100 if trades else 0
    return BacktestResult(
        trades=trades,
        total_net=net,
        win_rate=wr,
        sharpe=compute_sharpe(trades),
        max_drawdown=max_drawdown(trades),
        monthly=monthly_breakdown(trades),
    )


def _compute_rolling_corr(pa: np.ndarray, pb: np.ndarray, window: int) -> np.ndarray:
    """Compute rolling correlation of hourly log returns."""
    log_ret_a = np.diff(np.log(pa))
    log_ret_b = np.diff(np.log(pb))
    corr = rolling_correlation(log_ret_a, log_ret_b, window)
    # Prepend NaN to realign: returns have n-1 elements, prices have n
    return np.concatenate([[np.nan], corr])


def _add_mae(
    trade: CompletedTrade,
    pa: np.ndarray,
    pb: np.ndarray,
    exit_idx: int,
    config: StrategyConfig,
) -> CompletedTrade:
    """Add max adverse excursion to a completed trade."""
    entry_idx = exit_idx - trade.hours_held
    ea, eb = trade.entry_price_a, trade.entry_price_b
    notional = config.notional_per_leg
    rt_cost = config.round_trip_cost

    mae = 0.0
    for k in range(entry_idx + 1, exit_idx + 1):
        pnl_a, pnl_b = compute_leg_pnl(
            trade.direction, notional, ea, eb, float(pa[k]), float(pb[k])
        )
        interim = pnl_a + pnl_b - rt_cost
        if interim < mae:
            mae = interim

    return replace(trade, max_adverse_excursion=mae)


# -- Parameter Sweep --


def run_parameter_sweep(
    prices: pd.DataFrame,
    base_config: StrategyConfig,
    sweep: SweepConfig | None = None,
    lookbacks: list[int] | None = None,
    entry_zs: list[float] | None = None,
    funding: pd.DataFrame | None = None,
) -> list[SweepRow]:
    """Run backtest across parameter grid. Returns list of SweepRow results."""
    defaults = sweep or SweepConfig()
    lookbacks = lookbacks or list(defaults.lookbacks)
    entry_zs = entry_zs or list(defaults.entry_zs)

    results: list[SweepRow] = []

    for lb in lookbacks:
        for ze in entry_zs:
            config = replace(base_config, lookback_hours=lb, entry_z=ze)
            trades = run_backtest_all_pairs(prices, config, funding=funding)

            net = sum(t.net_pnl for t in trades)
            wins = sum(1 for t in trades if t.net_pnl > 0)
            wr = wins / len(trades) * 100 if trades else 0
            sharpe = compute_sharpe(trades)
            dd = max_drawdown(trades)
            n_days = (prices.index[-1] - prices.index[0]).days
            daily = net / n_days if n_days > 0 else 0

            results.append(
                SweepRow(
                    lookback=lb,
                    entry_z=ze,
                    trades=len(trades),
                    win_rate=wr,
                    net=net,
                    daily=daily,
                    max_dd=dd,
                    sharpe=sharpe,
                )
            )

            logger.info(
                "  lb=%dh z=%.1f: %d trades, %.0f%% WR, $%+.0f, Sharpe %.2f",
                lb,
                ze,
                len(trades),
                wr,
                net,
                sharpe,
            )

    return results


# -- Statistics --


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
    return _intra_period_drawdown([daily[d] for d in sorted(daily)])


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


# -- Gate 1: Backtest --


def check_backtest_gate(result: BacktestResult, gate_config: GateConfig) -> GateResult:
    """Check whether backtest results pass the gate."""
    passed = result.sharpe >= gate_config.min_sharpe
    detail = f"sharpe={result.sharpe:.2f}, required={gate_config.min_sharpe}"
    logger.info("Backtest gate: %s (%s)", "PASS" if passed else "FAIL", detail)
    return GateResult(gate="backtest", passed=passed, detail=detail)
