"""Walk-forward validation and statistical robustness metrics.

Implements:
- Anchored walk-forward backtesting (expanding training window)
- Probabilistic Sharpe Ratio (PSR)
- Deflated Sharpe Ratio (DSR)
- Conditional Value at Risk (CVaR / Expected Shortfall)
- Sortino ratio
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from hypemm.backtest import (
    compute_sharpe,
    max_drawdown,
    run_backtest_all_pairs,
)
from hypemm.config import StrategyConfig
from hypemm.models import CompletedTrade

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Walk-forward data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WalkForwardWindow:
    """Result of one walk-forward fold."""

    fold: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    train_trades: int
    train_sharpe: float
    train_net: float
    test_trades: int
    test_sharpe: float
    test_net: float
    test_win_rate: float
    test_max_dd: float
    test_daily_avg: float
    selected_config: str | None = None


@dataclass(frozen=True)
class WalkForwardResult:
    """Aggregate walk-forward validation result."""

    windows: list[WalkForwardWindow]
    oos_trades: int
    oos_net: float
    oos_sharpe: float
    oos_win_rate: float
    oos_max_dd: float
    oos_daily_avg: float
    psr: float
    dsr: float
    cvar_95: float
    cvar_99: float
    sortino: float
    skewness: float
    kurtosis: float
    n_trials: int


# ---------------------------------------------------------------------------
# Walk-forward engine
# ---------------------------------------------------------------------------


def run_walk_forward(
    prices: pd.DataFrame,
    config: StrategyConfig,
    funding: pd.DataFrame | None = None,
    train_years: int = 2,
    test_months: int = 12,
    step_months: int = 12,
    candidate_configs: dict[str, StrategyConfig] | None = None,
    selection_metric: str = "sharpe",
) -> WalkForwardResult:
    """Run anchored walk-forward validation.

    Splits the price series into expanding train windows and fixed-length
    test windows.  Each fold trains on [start, train_end) and tests on
    [train_end, train_end + test_months).

    Returns aggregate OOS statistics and per-window breakdowns.
    """
    start = prices.index[0]
    end = prices.index[-1]
    windows: list[WalkForwardWindow] = []
    all_oos_trades: list[CompletedTrade] = []
    oos_calendar_days: list[pd.Timestamp] = []

    candidates = candidate_configs or {"baseline": config}
    if not candidates:
        raise ValueError("candidate_configs must contain at least one strategy")

    fold = 0
    train_end = start + pd.DateOffset(years=train_years)

    while True:
        test_end = train_end + pd.DateOffset(months=test_months)
        if train_end >= end:
            break
        if test_end > end:
            test_end = end

        # Slice data
        train_prices = prices[start:train_end]
        test_prices = prices[train_end:test_end]

        if len(train_prices) < config.lookback_hours + 100:
            train_end += pd.DateOffset(months=step_months)
            continue
        if len(test_prices) < config.lookback_hours + 50:
            break

        # Need lookback_hours + corr_window of warm-up before test period
        warmup = max(config.lookback_hours, config.corr_window_hours) + 10
        test_with_warmup_start = test_prices.index[0] - pd.Timedelta(hours=warmup)
        test_prices_warm = prices[test_with_warmup_start:test_end]

        train_funding = _slice_funding(funding, start, train_end)
        test_funding = _slice_funding(funding, test_with_warmup_start, test_end)

        selected_name, selected_config, train_trades = _select_training_config(
            train_prices,
            candidates,
            train_funding,
            selection_metric,
        )
        test_trades_all = run_backtest_all_pairs(
            test_prices_warm,
            selected_config,
            funding=test_funding,
        )

        # Filter test trades to only those entered during the test period
        test_start_ms = int(test_prices.index[0].timestamp() * 1000)
        test_trades = [t for t in test_trades_all if t.entry_ts >= test_start_ms]

        train_sharpe = compute_sharpe(train_trades)
        train_net = sum(t.net_pnl for t in train_trades)
        test_sharpe = compute_sharpe(test_trades)
        test_net = sum(t.net_pnl for t in test_trades)
        test_wins = sum(1 for t in test_trades if t.net_pnl > 0)
        test_wr = test_wins / len(test_trades) * 100 if test_trades else 0
        test_dd = max_drawdown(test_trades)

        test_days = (test_end - train_end).days
        test_daily = test_net / test_days if test_days > 0 else 0

        window = WalkForwardWindow(
            fold=fold,
            train_start=str(start.date()),
            train_end=str(train_end.date()),
            test_start=str(train_end.date()),
            test_end=str(test_end.date()),
            train_trades=len(train_trades),
            train_sharpe=train_sharpe,
            train_net=train_net,
            test_trades=len(test_trades),
            test_sharpe=test_sharpe,
            test_net=test_net,
            test_win_rate=test_wr,
            test_max_dd=test_dd,
            test_daily_avg=test_daily,
            selected_config=selected_name,
        )
        windows.append(window)
        all_oos_trades.extend(test_trades)
        oos_calendar_days.extend(
            list(
                pd.date_range(
                    test_prices.index[0].floor("D"),
                    (test_end - pd.Timedelta(days=1)).floor("D"),
                    freq="D",
                )
            )
        )

        logger.info(
            "Fold %d: train [%s → %s] SR %.2f | " "test [%s → %s] %d trades, SR %.2f, $%+.0f",
            fold,
            start.date(),
            train_end.date(),
            train_sharpe,
            train_end.date(),
            test_end.date(),
            len(test_trades),
            test_sharpe,
            test_net,
        )

        fold += 1
        train_end += pd.DateOffset(months=step_months)

    # Aggregate OOS statistics
    oos_net = sum(t.net_pnl for t in all_oos_trades)
    oos_sharpe = compute_sharpe(all_oos_trades)
    oos_wins = sum(1 for t in all_oos_trades if t.net_pnl > 0)
    oos_wr = oos_wins / len(all_oos_trades) * 100 if all_oos_trades else 0
    oos_dd = max_drawdown(all_oos_trades)
    total_test_days = sum(
        (pd.Timestamp(w.test_end) - pd.Timestamp(w.test_start)).days for w in windows
    )
    oos_daily = oos_net / total_test_days if total_test_days > 0 else 0

    # Compute advanced metrics on OOS daily P&L
    daily_pnl = _daily_pnl_series(all_oos_trades, calendar_days=oos_calendar_days)
    psr_val = probabilistic_sharpe_ratio(daily_pnl, benchmark_sr=0.0)
    n_trials = _estimate_trials(config) * len(candidates)
    dsr_val = deflated_sharpe_ratio(daily_pnl, n_trials=n_trials)
    cvar95 = conditional_var(daily_pnl, alpha=0.05)
    cvar99 = conditional_var(daily_pnl, alpha=0.01)
    sortino_val = sortino_ratio(daily_pnl)
    skew = float(pd.Series(daily_pnl).skew()) if len(daily_pnl) > 2 else 0.0
    kurt = float(pd.Series(daily_pnl).kurtosis()) if len(daily_pnl) > 3 else 0.0

    return WalkForwardResult(
        windows=windows,
        oos_trades=len(all_oos_trades),
        oos_net=oos_net,
        oos_sharpe=oos_sharpe,
        oos_win_rate=oos_wr,
        oos_max_dd=oos_dd,
        oos_daily_avg=oos_daily,
        psr=psr_val,
        dsr=dsr_val,
        cvar_95=cvar95,
        cvar_99=cvar99,
        sortino=sortino_val,
        skewness=skew,
        kurtosis=kurt,
        n_trials=n_trials,
    )


# ---------------------------------------------------------------------------
# Statistical metrics
# ---------------------------------------------------------------------------


def probabilistic_sharpe_ratio(
    daily_pnl: list[float],
    benchmark_sr: float = 0.0,
) -> float:
    """Probability that the true Sharpe exceeds a benchmark.

    PSR accounts for sample length, skewness, and kurtosis of returns.
    Returns a value in [0, 1]; > 0.95 is considered significant.

    Reference: Bailey & López de Prado (2012).
    """
    n = len(daily_pnl)
    if n < 5:
        return 0.0

    arr = np.asarray(daily_pnl, dtype=np.float64)
    mean = float(np.mean(arr))
    std = float(np.std(arr, ddof=1))
    if std < 1e-10:
        return 0.0

    sr = mean / std
    skew = float(_skewness(arr))
    kurt = float(_kurtosis(arr))

    # Standard error of the Sharpe ratio (Lo, 2002 adjusted for non-normality)
    # kurt here is EXCESS kurtosis (_kurtosis returns m4/m2^2 - 3).
    # The formula uses regular kurtosis κ: SE = sqrt((1 - γ3*SR + (κ-1)/4 * SR^2) / (T-1))
    # Since κ = excess + 3, (κ-1)/4 = (excess+2)/4.
    se_sq = (1 - skew * sr + (kurt + 2) / 4 * sr**2) / (n - 1)
    if se_sq < 1e-20:
        return 0.0
    se = np.sqrt(se_sq)
    if se < 1e-12:
        return 0.0

    z = (sr - benchmark_sr) / se
    return float(_norm_cdf(z))


def deflated_sharpe_ratio(
    daily_pnl: list[float],
    n_trials: int = 1,
) -> float:
    """Sharpe deflated for multiple testing (Bailey & López de Prado, 2014).

    Adjusts the benchmark SR upward based on how many strategy variants
    were tried, then computes PSR against that deflated benchmark.

    n_trials: total number of independent strategies tested (pair selection
    + parameter sweep combinations).
    """
    n = len(daily_pnl)
    if n < 5 or n_trials < 1:
        return 0.0

    arr = np.asarray(daily_pnl, dtype=np.float64)
    std = float(np.std(arr, ddof=1))
    if std < 1e-10:
        return 0.0

    # Expected maximum SR under the null (all strategies have SR=0)
    # E[max(Z_1,...,Z_k)] ≈ sqrt(2 * ln(k)) for k independent normals
    # Adjusted: SR_0 = sqrt(V(SR)) * E[max] where V(SR) ≈ 1/(n-1)
    sr_std = 1.0 / np.sqrt(n - 1)

    if n_trials <= 1:
        expected_max_sr = 0.0
    else:
        euler_mascheroni = 0.5772156649
        expected_max_sr = sr_std * (
            (1 - euler_mascheroni) * _inv_norm_cdf(1 - 1.0 / n_trials)
            + euler_mascheroni * _inv_norm_cdf(1 - 1.0 / (n_trials * np.e))
        )

    return probabilistic_sharpe_ratio(daily_pnl, benchmark_sr=expected_max_sr)


def conditional_var(
    daily_pnl: list[float],
    alpha: float = 0.05,
) -> float:
    """Conditional Value at Risk (Expected Shortfall).

    Returns the expected loss (as a positive number) in the worst alpha
    fraction of days.  E.g., alpha=0.05 gives CVaR at the 95% level.
    """
    if len(daily_pnl) < 10:
        return 0.0

    arr = np.sort(daily_pnl)
    cutoff = int(np.ceil(len(arr) * alpha))
    if cutoff < 1:
        cutoff = 1

    tail = arr[:cutoff]
    return float(-np.mean(tail))


def sortino_ratio(daily_pnl: list[float]) -> float:
    """Annualized Sortino ratio (downside deviation only)."""
    if len(daily_pnl) < 5:
        return 0.0

    arr = np.asarray(daily_pnl, dtype=np.float64)
    mean = float(np.mean(arr))
    downside = arr[arr < 0]
    if len(downside) < 2:
        return 0.0

    downside_std = float(np.std(downside, ddof=1))
    if downside_std < 1e-10:
        return 0.0

    return mean / downside_std * float(np.sqrt(365))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _daily_pnl_series(
    trades: list[CompletedTrade],
    calendar_days: list[pd.Timestamp] | None = None,
) -> list[float]:
    """Convert trades to daily P&L series.

    When calendar_days is provided, include every day in that calendar and
    fill missing trade days with zero P&L so risk metrics reflect idle periods.
    """
    daily: dict[pd.Timestamp, float] = {}
    for t in trades:
        dt = datetime.fromtimestamp(t.exit_ts / 1000, tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day = pd.Timestamp(dt)
        daily[day] = daily.get(day, 0) + t.net_pnl
    if calendar_days is None:
        return [daily[d] for d in sorted(daily)]

    normalized_days = sorted(
        {
            pd.Timestamp(day).tz_convert("UTC") if pd.Timestamp(day).tzinfo else pd.Timestamp(day, tz="UTC")
            for day in calendar_days
        }
    )
    return [daily.get(day, 0.0) for day in normalized_days]


def _select_training_config(
    train_prices: pd.DataFrame,
    candidates: dict[str, StrategyConfig],
    funding: pd.DataFrame | None,
    selection_metric: str,
) -> tuple[str, StrategyConfig, list[CompletedTrade]]:
    """Pick the best config on the training window and return its trades."""
    scored: list[tuple[tuple[float, float, float, float], str, StrategyConfig, list[CompletedTrade]]] = []
    for name, candidate in candidates.items():
        trades = run_backtest_all_pairs(train_prices, candidate, funding=funding)
        score = _training_score(trades, selection_metric)
        scored.append((score, name, candidate, trades))

    scored.sort(key=lambda item: item[0], reverse=True)
    _, name, candidate, trades = scored[0]
    return name, candidate, trades


def _training_score(
    trades: list[CompletedTrade],
    selection_metric: str,
) -> tuple[float, float, float, float]:
    """Return a sortable training score tuple.

    Higher is better for the overall tuple ordering.
    """
    sharpe = compute_sharpe(trades)
    net = sum(t.net_pnl for t in trades)
    drawdown = max_drawdown(trades)
    wins = sum(1 for t in trades if t.net_pnl > 0)
    win_rate = wins / len(trades) if trades else 0.0

    if selection_metric == "net":
        primary = net
    elif selection_metric == "win_rate":
        primary = win_rate
    else:
        primary = sharpe

    return (primary, sharpe, net, -drawdown)


def _slice_funding(
    funding: pd.DataFrame | None,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame | None:
    if funding is None:
        return None
    mask = (funding.index >= start) & (funding.index < end)
    sliced = funding[mask]
    return sliced if not sliced.empty else None


def _estimate_trials(config: StrategyConfig) -> int:
    """Estimate the number of independent strategy trials.

    Accounts for pair selection (chose 4 from ~45 possible combinations
    of 10 coins) and the 3x3 parameter sweep.
    """
    n_pair_combos = 45
    n_param_combos = 9  # 3 lookbacks x 3 entry_z values
    return n_pair_combos * n_param_combos


def _skewness(arr: np.ndarray) -> float:
    """Sample skewness (Fisher's definition)."""
    n = len(arr)
    if n < 3:
        return 0.0
    mean = np.mean(arr)
    m2 = np.sum((arr - mean) ** 2) / n
    m3 = np.sum((arr - mean) ** 3) / n
    if m2 < 1e-20:
        return 0.0
    return float(m3 / m2**1.5)


def _kurtosis(arr: np.ndarray) -> float:
    """Excess kurtosis (Fisher's definition)."""
    n = len(arr)
    if n < 4:
        return 0.0
    mean = np.mean(arr)
    m2 = np.sum((arr - mean) ** 2) / n
    m4 = np.sum((arr - mean) ** 4) / n
    if m2 < 1e-20:
        return 0.0
    return float(m4 / m2**2 - 3)


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    import math

    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _inv_norm_cdf(p: float) -> float:
    """Inverse standard normal CDF (rational approximation).

    Abramowitz & Stegun 26.2.23, accurate to ~4.5e-4.
    """
    if p <= 0:
        return -6.0
    if p >= 1:
        return 6.0

    if p < 0.5:
        return -_inv_norm_cdf_upper(p)
    return _inv_norm_cdf_upper(1 - p)


def _inv_norm_cdf_upper(p: float) -> float:
    """Helper for inverse normal CDF, p in (0, 0.5]."""
    import math

    if p <= 0:
        return 6.0
    t = math.sqrt(-2 * math.log(p))
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    return t - (c0 + c1 * t + c2 * t**2) / (1 + d1 * t + d2 * t**2 + d3 * t**3)
