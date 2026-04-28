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


# -- Stationarity --


def hurst_exponent(series: np.ndarray, max_lag: int = 20) -> float:
    """Estimate the Hurst exponent via variance of lagged differences.

    For each lag τ, compute std(X_{t+τ} - X_t).  For fractional Brownian
    motion this scales as τ^H, so H = slope of log(std) vs log(τ).

    H < 0.5 → mean-reverting, H = 0.5 → random walk, H > 0.5 → trending.
    Returns NaN if insufficient data.
    """
    n = len(series)
    if n < max_lag * 2:
        return float("nan")

    lags = range(2, min(max_lag + 1, n // 2))
    log_lags = []
    log_stds = []

    for lag in lags:
        diffs = series[lag:] - series[:-lag]
        std = float(np.std(diffs))
        if std > 1e-15:
            log_lags.append(np.log(lag))
            log_stds.append(np.log(std))

    if len(log_lags) < 3:
        return float("nan")

    # Linear regression: log(std) = H * log(lag) + c
    x = np.array(log_lags)
    y = np.array(log_stds)
    n_pts = len(x)
    sx = np.sum(x)
    sy = np.sum(y)
    sxx = np.sum(x * x)
    sxy = np.sum(x * y)
    denom = n_pts * sxx - sx * sx
    if abs(denom) < 1e-20:
        return float("nan")

    h = (n_pts * sxy - sx * sy) / denom
    return float(np.clip(h, 0.0, 1.0))


def rolling_hurst(log_ratios: np.ndarray, window: int, max_lag: int = 20) -> np.ndarray:
    """Compute rolling Hurst exponent on log price ratios."""
    n = len(log_ratios)
    result = np.full(n, np.nan)
    for i in range(window, n):
        result[i] = hurst_exponent(log_ratios[i - window : i], max_lag=max_lag)
    return result


def adf_test(series: np.ndarray, max_lag: int = 1) -> float:
    """Augmented Dickey-Fuller test statistic (no p-value table needed).

    Tests H0: unit root (non-stationary) vs H1: stationary.
    Returns the t-statistic. Critical values (approx, no trend, n>250):
      1%: -3.43, 5%: -2.86, 10%: -2.57
    More negative = stronger evidence of stationarity.
    """
    n = len(series)
    if n < max_lag + 10:
        return 0.0

    # Δy_t = α * y_{t-1} + Σ β_i * Δy_{t-i} + ε_t
    dy = np.diff(series)
    y_lag = series[max_lag:-1]
    m = len(y_lag)

    # Build design matrix: [y_{t-1}, Δy_{t-1}, ..., Δy_{t-max_lag}]
    x_cols = [y_lag]
    for lag in range(1, max_lag + 1):
        x_cols.append(dy[max_lag - lag : m + max_lag - lag])

    x = np.column_stack(x_cols)
    y_dep = dy[max_lag : m + max_lag]

    if len(y_dep) != x.shape[0]:
        min_len = min(len(y_dep), x.shape[0])
        y_dep = y_dep[:min_len]
        x = x[:min_len]

    # OLS: β = (X'X)^{-1} X'y
    xtx = x.T @ x
    try:
        xtx_inv = np.linalg.inv(xtx)
    except np.linalg.LinAlgError:
        return 0.0

    beta = xtx_inv @ (x.T @ y_dep)
    residuals = y_dep - x @ beta
    s2 = float(np.sum(residuals**2) / (len(y_dep) - x.shape[1]))
    if s2 < 1e-20:
        return -10.0  # perfectly stationary

    se = np.sqrt(np.diag(xtx_inv) * s2)
    if se[0] < 1e-15:
        return -10.0

    return float(beta[0] / se[0])


def rolling_adf(log_ratios: np.ndarray, window: int, max_lag: int = 1) -> np.ndarray:
    """Compute rolling ADF t-statistic on log price ratios."""
    n = len(log_ratios)
    result = np.full(n, np.nan)
    for i in range(window, n):
        result[i] = adf_test(log_ratios[i - window : i], max_lag=max_lag)
    return result


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
