"""Parameter sweep for strategy optimization."""

from __future__ import annotations

import logging
from dataclasses import replace

import pandas as pd

from hypemm.analysis.backtest import run_backtest_all_pairs
from hypemm.analysis.stats import compute_sharpe, max_drawdown
from hypemm.config import SWEEP_ENTRY_Z, SWEEP_LOOKBACKS, StrategyConfig

logger = logging.getLogger(__name__)


def run_parameter_sweep(
    prices: pd.DataFrame,
    base_config: StrategyConfig,
    lookbacks: list[int] | None = None,
    entry_zs: list[float] | None = None,
) -> list[dict[str, object]]:
    """Run backtest across parameter grid. Returns list of result dicts."""
    lookbacks = lookbacks or SWEEP_LOOKBACKS
    entry_zs = entry_zs or SWEEP_ENTRY_Z

    results: list[dict[str, object]] = []

    for lb in lookbacks:
        for ze in entry_zs:
            config = replace(base_config, lookback_hours=lb, entry_z=ze)
            trades = run_backtest_all_pairs(prices, config)

            net = sum(t.net_pnl for t in trades)
            wins = sum(1 for t in trades if t.net_pnl > 0)
            wr = wins / len(trades) * 100 if trades else 0
            sharpe = compute_sharpe(trades)
            dd = max_drawdown(trades)
            n_days = (prices.index[-1] - prices.index[0]).days
            daily = net / n_days if n_days > 0 else 0

            results.append(
                {
                    "lookback": lb,
                    "entry_z": ze,
                    "trades": len(trades),
                    "win_rate": wr,
                    "net": net,
                    "daily": daily,
                    "max_dd": dd,
                    "sharpe": sharpe,
                }
            )

            logger.info(
                "  lb=%dh z=%.1f: %d trades, %.0f%% WR, $%+,.0f, Sharpe %.2f",
                lb,
                ze,
                len(trades),
                wr,
                net,
                sharpe,
            )

    return results
