"""Z-score, correlation, and P&L computations."""

from __future__ import annotations

import numpy as np

from hypemm.models import Direction, OpenPosition

# -- Z-score --


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


# -- Correlation --


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


# -- P&L --


def compute_leg_pnl(
    direction: Direction,
    notional: float,
    entry_price_a: float,
    entry_price_b: float,
    exit_price_a: float,
    exit_price_b: float,
) -> tuple[float, float]:
    """Compute P&L for each leg of a stat arb trade.

    Returns (pnl_leg_a, pnl_leg_b).

    LONG_RATIO = long A, short B.
    SHORT_RATIO = short A, long B.
    """
    if direction == Direction.LONG_RATIO:
        pnl_a = notional * (exit_price_a - entry_price_a) / entry_price_a
        pnl_b = notional * (entry_price_b - exit_price_b) / entry_price_b
    else:
        pnl_a = notional * (entry_price_a - exit_price_a) / entry_price_a
        pnl_b = notional * (exit_price_b - entry_price_b) / entry_price_b
    return pnl_a, pnl_b


def compute_unrealized_pnl(
    position: OpenPosition,
    current_price_a: float,
    current_price_b: float,
    notional: float,
) -> float:
    """Unrealized P&L for an open position (before costs)."""
    pnl_a, pnl_b = compute_leg_pnl(
        position.direction,
        notional,
        position.entry_price_a,
        position.entry_price_b,
        current_price_a,
        current_price_b,
    )
    return pnl_a + pnl_b
