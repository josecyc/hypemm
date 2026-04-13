"""Backtest runner: drives the strategy engine over historical data."""

from __future__ import annotations

import logging
from dataclasses import replace

import numpy as np
import pandas as pd

from hypemm.config import StrategyConfig
from hypemm.math.correlation import rolling_correlation
from hypemm.math.pnl import compute_leg_pnl
from hypemm.math.zscore import compute_log_ratios, compute_z_scores
from hypemm.models import (
    CompletedTrade,
    EntryOrder,
    ExitOrder,
    PairConfig,
    Signal,
)
from hypemm.strategy.engine import StrategyEngine

logger = logging.getLogger(__name__)


def run_backtest(
    prices: pd.DataFrame,
    pair: PairConfig,
    config: StrategyConfig,
) -> list[CompletedTrade]:
    """Run full backtest on one pair using the strategy engine."""
    pa = prices[pair.coin_a].values
    pb = prices[pair.coin_b].values
    timestamps = prices.index
    n = len(pa)

    if n < config.lookback_hours + 10:
        return []

    log_ratios = compute_log_ratios(np.asarray(pa), np.asarray(pb))
    z_scores = compute_z_scores(log_ratios, config.lookback_hours)
    corr_values = _compute_rolling_corr(pa, pb, config.corr_window_hours)

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
                completed.append(trade)

    return completed


def run_backtest_all_pairs(
    prices: pd.DataFrame,
    config: StrategyConfig,
) -> list[CompletedTrade]:
    """Run backtest across all configured pairs."""
    all_trades: list[CompletedTrade] = []
    for pair in config.pairs:
        trades = run_backtest(prices, pair, config)
        all_trades.extend(trades)
        wins = sum(1 for t in trades if t.net_pnl > 0)
        net = sum(t.net_pnl for t in trades)
        wr = wins / len(trades) * 100 if trades else 0
        logger.info(
            "%s: %d trades, %.0f%% WR, $%+,.0f",
            pair.label,
            len(trades),
            wr,
            net,
        )
    return all_trades


def _compute_rolling_corr(pa: np.ndarray, pb: np.ndarray, window: int) -> np.ndarray:
    """Compute rolling correlation of hourly log returns.

    Returns an array aligned with the price arrays (one element per bar).
    The correlation at index i uses returns up to but not including bar i.
    """
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
