"""Rolling correlation computation for return series."""

from __future__ import annotations

import numpy as np


def rolling_correlation(
    returns_a: np.ndarray,
    returns_b: np.ndarray,
    window: int,
) -> np.ndarray:
    """Rolling Pearson correlation between two return series.

    Returns an array of the same length as the inputs, with NaN
    for positions where the window is incomplete.
    """
    n = len(returns_a)
    result = np.full(n, np.nan)

    for i in range(window, n):
        wa = returns_a[i - window : i]
        wb = returns_b[i - window : i]
        corr_matrix = np.corrcoef(wa, wb)
        result[i] = corr_matrix[0, 1]

    return result


def compute_correlation_single(
    returns_a: np.ndarray,
    returns_b: np.ndarray,
    window: int,
) -> float | None:
    """Compute correlation over the most recent `window` returns.

    This is meant for the live system: given all available returns,
    compute the Pearson correlation of the trailing window.

    Returns None if insufficient data.
    """
    if len(returns_a) < window or len(returns_b) < window:
        return None

    wa = returns_a[-window:]
    wb = returns_b[-window:]

    if len(wa) < 2:
        return None

    corr_matrix = np.corrcoef(wa, wb)
    return float(corr_matrix[0, 1])
