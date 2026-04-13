"""Z-score computation for log price ratios."""

from __future__ import annotations

import numpy as np


def compute_log_ratios(prices_a: np.ndarray, prices_b: np.ndarray) -> np.ndarray:
    """Compute log price ratios: ln(A/B)."""
    return np.log(prices_a / prices_b)  # type: ignore[no-any-return]


def compute_z_scores(log_ratios: np.ndarray, lookback: int) -> np.ndarray:
    """Rolling z-scores of log price ratios.

    For each bar i >= lookback, the z-score is computed using
    the window [i-lookback : i] for mean/std, then scoring
    the current bar's log ratio against that window.

    Returns an array of the same length as log_ratios, with NaN
    for bars where insufficient history exists.
    """
    n = len(log_ratios)
    z_scores = np.full(n, np.nan)

    for i in range(lookback, n):
        window = log_ratios[i - lookback : i]
        mean = np.mean(window)
        std = float(np.std(window, ddof=1))
        if std > 1e-10:
            z_scores[i] = (log_ratios[i] - mean) / std

    return z_scores


def compute_z_score_single(log_ratios: np.ndarray, lookback: int) -> float | None:
    """Compute a single z-score for the last bar in the array.

    Uses the previous `lookback` bars as the window and scores the
    final bar against it. Returns None if insufficient data.
    """
    n = len(log_ratios)
    if n < lookback + 1:
        return None

    window = log_ratios[-(lookback + 1) : -1]
    mean = np.mean(window)
    std = float(np.std(window, ddof=1))
    if std <= 1e-10:
        return None

    return float((log_ratios[-1] - mean) / std)
