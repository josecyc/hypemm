"""Correlation stability analysis (Gate 2)."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from hypemm.config import GateConfig, StrategyConfig
from hypemm.math import rolling_correlation
from hypemm.models import GateResult

logger = logging.getLogger(__name__)

CORR_LOW = 0.5


def compute_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """Compute hourly log returns."""
    result: pd.DataFrame = np.log(prices / prices.shift(1)).dropna()
    return result


def correlation_regimes(
    corr_series: np.ndarray,
    high_threshold: float,
) -> dict[str, float]:
    """Classify correlation into HIGH/MEDIUM/LOW regimes."""
    valid = corr_series[~np.isnan(corr_series)]
    total = len(valid)
    if total == 0:
        return {"high_pct": 0, "med_pct": 0, "low_pct": 0, "mean": 0}

    high = int(np.sum(valid > high_threshold))
    low = int(np.sum(valid < CORR_LOW))
    med = total - high - low

    return {
        "high_pct": high / total * 100,
        "med_pct": med / total * 100,
        "low_pct": low / total * 100,
        "mean": float(np.nanmean(valid)),
        "std": float(np.nanstd(valid)),
        "min": float(np.nanmin(valid)),
    }


def find_breakdowns(
    corr_series: np.ndarray,
    timestamps: pd.DatetimeIndex,
    threshold: float = CORR_LOW,
) -> list[dict[str, object]]:
    """Find continuous periods where correlation is below threshold."""
    below = corr_series < threshold
    breakdowns: list[dict[str, object]] = []
    in_breakdown = False
    start_idx = 0

    for i in range(len(corr_series)):
        if np.isnan(corr_series[i]):
            continue
        if below[i] and not in_breakdown:
            in_breakdown = True
            start_idx = i
        elif not below[i] and in_breakdown:
            in_breakdown = False
            duration_hours = i - start_idx
            min_corr = float(np.nanmin(corr_series[start_idx:i]))
            breakdowns.append(
                {
                    "start": str(timestamps[start_idx]),
                    "end": str(timestamps[i]),
                    "duration_hours": duration_hours,
                    "min_corr": min_corr,
                }
            )

    if in_breakdown:
        duration_hours = len(corr_series) - start_idx
        min_corr = float(np.nanmin(corr_series[start_idx:]))
        breakdowns.append(
            {
                "start": str(timestamps[start_idx]),
                "end": str(timestamps[-1]),
                "duration_hours": duration_hours,
                "min_corr": min_corr,
            }
        )

    return breakdowns


def compute_correlation_stability(
    prices: pd.DataFrame,
    config: StrategyConfig,
) -> tuple[list[dict[str, object]], dict[str, list[dict[str, object]]]]:
    """Compute correlation regimes and breakdowns for all pairs.

    Returns (regime_results, all_breakdowns).
    """
    returns = compute_returns(prices)

    regime_results: list[dict[str, object]] = []
    all_breakdowns: dict[str, list[dict[str, object]]] = {}

    for pair in config.pairs:
        ret_a = returns[pair.coin_a].values
        ret_b = returns[pair.coin_b].values
        corr = rolling_correlation(ret_a, ret_b, config.corr_window_hours)
        regimes = correlation_regimes(corr, config.corr_threshold)
        regime_results.append({"pair": pair.label, **regimes})

        bds = find_breakdowns(corr, returns.index, CORR_LOW)
        all_breakdowns[pair.label] = bds
        logger.info(
            "%s: mean=%.3f, HIGH=%.0f%%, breakdowns=%d",
            pair.label,
            regimes["mean"],
            regimes["high_pct"],
            len(bds),
        )

    return regime_results, all_breakdowns


def check_correlation_gate(
    regimes: list[dict[str, object]],
    breakdowns: dict[str, list[dict[str, object]]],
    gate_config: GateConfig,
) -> GateResult:
    """Check whether correlation stability passes the gate."""
    all_high = all(float(str(r["high_pct"])) >= gate_config.min_high_corr_pct for r in regimes)
    max_breakdown = 0
    for bds_list in breakdowns.values():
        for bd in bds_list:
            dur = int(str(bd["duration_hours"]))
            if dur > max_breakdown:
                max_breakdown = dur

    passed = all_high and max_breakdown <= gate_config.max_breakdown_hours
    detail = f"all_high={all_high}, max_breakdown={max_breakdown}h"
    logger.info("Correlation gate: %s (%s)", "PASS" if passed else "FAIL", detail)
    return GateResult(gate="correlation", passed=passed, detail=detail)
