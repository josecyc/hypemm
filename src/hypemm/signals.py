"""Signal computation: z-scores and correlation gating."""

from __future__ import annotations

import numpy as np

from hypemm.config import StrategyConfig
from hypemm.math import compute_correlation_single, compute_log_ratios, compute_z_score_single
from hypemm.models import PairConfig, Signal


def compute_pair_signal(
    prices_a: np.ndarray,
    prices_b: np.ndarray,
    config: StrategyConfig,
    pair: PairConfig,
    timestamp_ms: int,
) -> Signal | None:
    """Compute signal for a single pair from price arrays.

    prices_a and prices_b are arrays of hourly close prices, newest last.
    Returns None if insufficient data for z-score computation.
    """
    n = min(len(prices_a), len(prices_b))
    if n < config.lookback_hours + 1:
        return None

    pa = np.asarray(prices_a[-n:], dtype=np.float64)
    pb = np.asarray(prices_b[-n:], dtype=np.float64)

    log_ratios = compute_log_ratios(pa, pb)
    z = compute_z_score_single(log_ratios, config.lookback_hours)
    if z is None:
        return None

    # Correlation of hourly log returns
    log_returns_a = np.diff(np.log(pa))
    log_returns_b = np.diff(np.log(pb))
    corr = compute_correlation_single(log_returns_a, log_returns_b, config.corr_window_hours)

    return Signal(
        pair=pair,
        z_score=z,
        correlation=corr,
        price_a=float(pa[-1]),
        price_b=float(pb[-1]),
        timestamp_ms=timestamp_ms,
        n_bars=n,
    )
