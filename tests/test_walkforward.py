"""Tests for walk-forward statistical metrics."""

from __future__ import annotations

import math

import numpy as np
import pytest

from hypemm.models import CompletedTrade, Direction, ExitReason
from hypemm.walkforward import (
    _daily_pnl_series,
    _inv_norm_cdf,
    _kurtosis,
    _norm_cdf,
    _select_training_config,
    _skewness,
    _training_score,
    conditional_var,
    deflated_sharpe_ratio,
    probabilistic_sharpe_ratio,
    sortino_ratio,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trade(exit_ts: int, net_pnl: float) -> CompletedTrade:
    """Create a minimal CompletedTrade for testing."""
    return CompletedTrade(
        pair_label="LINK/SOL",
        direction=Direction.LONG_RATIO,
        entry_ts=exit_ts - 3_600_000,
        exit_ts=exit_ts,
        entry_z=-2.5,
        exit_z=-0.3,
        hours_held=1,
        entry_price_a=15.0,
        entry_price_b=150.0,
        exit_price_a=16.0,
        exit_price_b=148.0,
        pnl_leg_a=0.0,
        pnl_leg_b=0.0,
        gross_pnl=0.0,
        cost=40.0,
        net_pnl=net_pnl,
        exit_reason=ExitReason.MEAN_REVERT,
        entry_correlation=0.85,
    )


# ---------------------------------------------------------------------------
# _norm_cdf
# ---------------------------------------------------------------------------


class TestNormCdf:
    def test_zero_gives_half(self) -> None:
        assert _norm_cdf(0.0) == 0.5

    def test_large_positive_near_one(self) -> None:
        assert _norm_cdf(6.0) > 0.9999

    def test_large_negative_near_zero(self) -> None:
        assert _norm_cdf(-6.0) < 0.0001

    @pytest.mark.parametrize(
        "x,expected",
        [
            (1.0, 0.8413),
            (-1.0, 0.1587),
            (1.96, 0.975),
            (2.576, 0.995),
        ],
    )
    def test_known_values(self, x: float, expected: float) -> None:
        assert abs(_norm_cdf(x) - expected) < 0.002

    def test_symmetry(self) -> None:
        for x in [0.5, 1.0, 2.0, 3.0]:
            assert abs(_norm_cdf(x) + _norm_cdf(-x) - 1.0) < 1e-10


# ---------------------------------------------------------------------------
# _inv_norm_cdf
# ---------------------------------------------------------------------------


class TestInvNormCdf:
    def test_half_gives_zero(self) -> None:
        assert abs(_inv_norm_cdf(0.5)) < 1e-4

    def test_boundary_zero(self) -> None:
        assert _inv_norm_cdf(0.0) == -6.0

    def test_boundary_one(self) -> None:
        assert _inv_norm_cdf(1.0) == 6.0

    @pytest.mark.parametrize(
        "p,expected",
        [
            (0.975, 1.96),
            (0.025, -1.96),
            (0.8413, 1.0),
            (0.1587, -1.0),
        ],
    )
    def test_known_quantiles(self, p: float, expected: float) -> None:
        assert abs(_inv_norm_cdf(p) - expected) < 0.02

    def test_roundtrip_with_norm_cdf(self) -> None:
        """_norm_cdf(_inv_norm_cdf(p)) should recover p approximately."""
        for p in [0.1, 0.25, 0.5, 0.75, 0.9]:
            recovered = _norm_cdf(_inv_norm_cdf(p))
            assert abs(recovered - p) < 0.005


# ---------------------------------------------------------------------------
# _skewness
# ---------------------------------------------------------------------------


class TestSkewness:
    def test_symmetric_data_near_zero(self) -> None:
        arr = np.array([-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0])
        assert abs(_skewness(arr)) < 1e-10

    def test_right_skewed(self) -> None:
        arr = np.array([1.0, 1.0, 1.0, 1.0, 1.0, 10.0])
        assert _skewness(arr) > 0

    def test_left_skewed(self) -> None:
        arr = np.array([-10.0, 1.0, 1.0, 1.0, 1.0, 1.0])
        assert _skewness(arr) < 0

    def test_too_few_values(self) -> None:
        assert _skewness(np.array([1.0, 2.0])) == 0.0

    def test_constant_returns_zero(self) -> None:
        arr = np.array([5.0, 5.0, 5.0, 5.0, 5.0])
        assert _skewness(arr) == 0.0


# ---------------------------------------------------------------------------
# _kurtosis
# ---------------------------------------------------------------------------


class TestKurtosis:
    def test_normal_distribution_near_zero(self) -> None:
        """Large normal sample should have excess kurtosis near zero."""
        rng = np.random.default_rng(42)
        arr = rng.normal(0, 1, 100_000)
        kurt = _kurtosis(arr)
        assert abs(kurt) < 0.1

    def test_uniform_negative_kurtosis(self) -> None:
        """Uniform distribution has excess kurtosis of -1.2."""
        rng = np.random.default_rng(42)
        arr = rng.uniform(-1, 1, 100_000)
        kurt = _kurtosis(arr)
        assert abs(kurt - (-1.2)) < 0.1

    def test_too_few_values(self) -> None:
        assert _kurtosis(np.array([1.0, 2.0, 3.0])) == 0.0

    def test_constant_returns_zero(self) -> None:
        arr = np.array([7.0, 7.0, 7.0, 7.0, 7.0])
        assert _kurtosis(arr) == 0.0

    def test_heavy_tails_positive_kurtosis(self) -> None:
        """Data with extreme outliers should have positive excess kurtosis."""
        arr = np.array([0.0] * 100 + [100.0, -100.0])
        assert _kurtosis(arr) > 0


# ---------------------------------------------------------------------------
# probabilistic_sharpe_ratio
# ---------------------------------------------------------------------------


class TestProbabilisticSharpeRatio:
    def test_empty_list(self) -> None:
        assert probabilistic_sharpe_ratio([]) == 0.0

    def test_too_few_observations(self) -> None:
        assert probabilistic_sharpe_ratio([1.0, 2.0, 3.0, 4.0]) == 0.0

    def test_all_zeros(self) -> None:
        assert probabilistic_sharpe_ratio([0.0] * 30) == 0.0

    def test_strong_positive_pnl_high_psr(self) -> None:
        """Positive mean with enough noise should yield high PSR."""
        rng = np.random.default_rng(42)
        # SR ~0.2 avoids the negative-SE edge case in the formula
        pnl = list(rng.normal(1.0, 5.0, 200))
        psr = probabilistic_sharpe_ratio(pnl, benchmark_sr=0.0)
        assert psr > 0.95

    def test_mixed_pnl_moderate_psr(self) -> None:
        """Noisy P&L with slight positive mean should give moderate PSR."""
        rng = np.random.default_rng(42)
        pnl = list(rng.normal(0.5, 5.0, 200))
        psr = probabilistic_sharpe_ratio(pnl, benchmark_sr=0.0)
        assert 0.0 < psr < 1.0

    def test_negative_pnl_low_psr(self) -> None:
        """Consistently negative P&L should produce PSR near zero."""
        pnl = [-10.0] * 100
        psr = probabilistic_sharpe_ratio(pnl, benchmark_sr=0.0)
        assert psr < 0.5

    def test_higher_benchmark_lowers_psr(self) -> None:
        """Raising the benchmark SR should decrease PSR."""
        rng = np.random.default_rng(42)
        pnl = list(rng.normal(1.0, 3.0, 200))
        psr_low = probabilistic_sharpe_ratio(pnl, benchmark_sr=0.0)
        psr_high = probabilistic_sharpe_ratio(pnl, benchmark_sr=1.0)
        assert psr_low > psr_high

    def test_returns_value_between_zero_and_one(self) -> None:
        rng = np.random.default_rng(99)
        pnl = list(rng.normal(2.0, 5.0, 500))
        psr = probabilistic_sharpe_ratio(pnl)
        assert 0.0 <= psr <= 1.0


# ---------------------------------------------------------------------------
# deflated_sharpe_ratio
# ---------------------------------------------------------------------------


class TestDeflatedSharpeRatio:
    def test_empty_list(self) -> None:
        assert deflated_sharpe_ratio([]) == 0.0

    def test_too_few_observations(self) -> None:
        assert deflated_sharpe_ratio([1.0, 2.0]) == 0.0

    def test_all_zeros(self) -> None:
        assert deflated_sharpe_ratio([0.0] * 30) == 0.0

    def test_single_trial_equals_psr(self) -> None:
        """With n_trials=1, DSR should equal PSR(benchmark=0)."""
        rng = np.random.default_rng(42)
        pnl = list(rng.normal(1.0, 3.0, 200))
        dsr = deflated_sharpe_ratio(pnl, n_trials=1)
        psr = probabilistic_sharpe_ratio(pnl, benchmark_sr=0.0)
        assert abs(dsr - psr) < 1e-10

    def test_increasing_trials_decreases_dsr(self) -> None:
        """More trials means a higher benchmark, so DSR should decrease."""
        rng = np.random.default_rng(42)
        pnl = list(rng.normal(1.0, 3.0, 200))
        dsr_1 = deflated_sharpe_ratio(pnl, n_trials=1)
        dsr_10 = deflated_sharpe_ratio(pnl, n_trials=10)
        dsr_100 = deflated_sharpe_ratio(pnl, n_trials=100)
        dsr_1000 = deflated_sharpe_ratio(pnl, n_trials=1000)
        assert dsr_1 > dsr_10 > dsr_100 > dsr_1000

    def test_zero_trials_returns_zero(self) -> None:
        pnl = [10.0] * 50
        assert deflated_sharpe_ratio(pnl, n_trials=0) == 0.0

    def test_negative_trials_returns_zero(self) -> None:
        pnl = [10.0] * 50
        assert deflated_sharpe_ratio(pnl, n_trials=-5) == 0.0

    def test_strong_signal_survives_many_trials(self) -> None:
        """Strong positive signal with large sample should survive many trials."""
        rng = np.random.default_rng(42)
        pnl = list(rng.normal(3.0, 5.0, 1000))
        dsr = deflated_sharpe_ratio(pnl, n_trials=100)
        assert dsr > 0.9


# ---------------------------------------------------------------------------
# conditional_var
# ---------------------------------------------------------------------------


class TestConditionalVar:
    def test_too_few_observations(self) -> None:
        assert conditional_var([1.0, 2.0, 3.0]) == 0.0

    def test_known_sorted_data_alpha_05(self) -> None:
        """With 20 data points and alpha=0.05, the worst 1 value is the tail."""
        data = list(range(-10, 10))  # [-10, -9, ..., 8, 9]
        cvar = conditional_var(data, alpha=0.05)
        # ceil(20 * 0.05) = 1, so tail = [-10], CVaR = -mean([-10]) = 10.0
        assert cvar == 10.0

    def test_known_sorted_data_alpha_10(self) -> None:
        data = list(range(-10, 10))  # 20 values
        cvar = conditional_var(data, alpha=0.10)
        # ceil(20 * 0.10) = 2, tail = [-10, -9], CVaR = -mean([-10,-9]) = 9.5
        assert cvar == 9.5

    def test_alpha_01_stricter_than_05(self) -> None:
        """CVaR at 1% should be at least as large as CVaR at 5%."""
        rng = np.random.default_rng(42)
        data = list(rng.normal(0, 10, 1000))
        cvar_01 = conditional_var(data, alpha=0.01)
        cvar_05 = conditional_var(data, alpha=0.05)
        assert cvar_01 >= cvar_05

    def test_all_positive_returns_negative_cvar(self) -> None:
        """If all returns are positive, CVaR is negative (a good thing)."""
        data = [float(i) for i in range(1, 21)]
        cvar = conditional_var(data, alpha=0.05)
        # tail = [1.0], CVaR = -mean([1.0]) = -1.0
        assert cvar == -1.0

    def test_all_same_value(self) -> None:
        data = [5.0] * 20
        cvar = conditional_var(data, alpha=0.05)
        assert cvar == -5.0

    def test_hundred_values_alpha_05(self) -> None:
        """100 values, alpha=0.05, so worst 5 values form the tail."""
        data = list(range(100))  # [0, 1, ..., 99]
        cvar = conditional_var([float(x) for x in data], alpha=0.05)
        # ceil(100 * 0.05) = 5, tail = [0, 1, 2, 3, 4], mean = 2.0
        assert cvar == -2.0


# ---------------------------------------------------------------------------
# sortino_ratio
# ---------------------------------------------------------------------------


class TestSortinoRatio:
    def test_too_few_observations(self) -> None:
        assert sortino_ratio([1.0, 2.0]) == 0.0

    def test_all_positive_returns_zero(self) -> None:
        """No downside returns means downside_std cannot be computed."""
        pnl = [10.0, 20.0, 30.0, 40.0, 50.0]
        assert sortino_ratio(pnl) == 0.0

    def test_only_one_negative_returns_zero(self) -> None:
        """Need at least 2 negative returns for ddof=1 std."""
        pnl = [10.0, 20.0, -5.0, 30.0, 40.0]
        assert sortino_ratio(pnl) == 0.0

    def test_positive_mean_with_downside(self) -> None:
        """Positive mean with downside deviation should give positive Sortino."""
        pnl = [10.0, -2.0, 8.0, -1.0, 12.0, -3.0, 15.0]
        sortino = sortino_ratio(pnl)
        assert sortino > 0

    def test_negative_mean_gives_negative_sortino(self) -> None:
        """Negative mean should produce a negative Sortino ratio."""
        pnl = [-10.0, -20.0, -5.0, 1.0, -15.0, -8.0, -12.0]
        sortino = sortino_ratio(pnl)
        assert sortino < 0

    def test_annualization_factor(self) -> None:
        """Verify the sqrt(365) annualization is applied."""
        pnl = [5.0, -1.0, 3.0, -2.0, 4.0, -1.5, 6.0]
        arr = np.asarray(pnl, dtype=np.float64)
        mean = float(np.mean(arr))
        downside = arr[arr < 0]
        downside_std = float(np.std(downside, ddof=1))
        expected = mean / downside_std * math.sqrt(365)
        assert abs(sortino_ratio(pnl) - expected) < 1e-10

    def test_constant_negative_returns_zero(self) -> None:
        """Constant negative returns have zero std (ddof=1 with >1 same values)."""
        pnl = [-5.0, -5.0, -5.0, -5.0, -5.0]
        assert sortino_ratio(pnl) == 0.0


# ---------------------------------------------------------------------------
# _daily_pnl_series
# ---------------------------------------------------------------------------


class TestDailyPnlSeries:
    def test_empty_trades(self) -> None:
        assert _daily_pnl_series([]) == []

    def test_single_trade(self) -> None:
        # 2024-01-01 00:00:00 UTC
        t = _make_trade(exit_ts=1_704_067_200_000, net_pnl=500.0)
        result = _daily_pnl_series([t])
        assert result == [500.0]

    def test_aggregates_same_day(self) -> None:
        """Two trades exiting on the same day should be summed."""
        base_ts = 1_704_067_200_000  # 2024-01-01 00:00:00 UTC
        t1 = _make_trade(exit_ts=base_ts, net_pnl=200.0)
        t2 = _make_trade(exit_ts=base_ts + 3_600_000, net_pnl=300.0)  # +1 hour
        result = _daily_pnl_series([t1, t2])
        assert len(result) == 1
        assert result[0] == 500.0

    def test_separate_days_sorted(self) -> None:
        """Trades on different days should produce sorted daily P&L."""
        day1 = 1_704_067_200_000  # 2024-01-01
        day2 = 1_704_153_600_000  # 2024-01-02
        day3 = 1_704_240_000_000  # 2024-01-03
        t1 = _make_trade(exit_ts=day1, net_pnl=100.0)
        t2 = _make_trade(exit_ts=day2, net_pnl=-50.0)
        t3 = _make_trade(exit_ts=day3, net_pnl=200.0)
        # Pass out of order to verify sorting
        result = _daily_pnl_series([t3, t1, t2])
        assert result == [100.0, -50.0, 200.0]

    def test_multiple_trades_different_days(self) -> None:
        """Multiple trades across multiple days with aggregation."""
        day1 = 1_704_067_200_000  # 2024-01-01
        day2 = 1_704_153_600_000  # 2024-01-02
        t1 = _make_trade(exit_ts=day1, net_pnl=100.0)
        t2 = _make_trade(exit_ts=day1 + 7_200_000, net_pnl=50.0)  # same day
        t3 = _make_trade(exit_ts=day2, net_pnl=-30.0)
        result = _daily_pnl_series([t1, t2, t3])
        assert len(result) == 2
        assert result[0] == 150.0
        assert result[1] == -30.0

    def test_calendar_days_fill_missing_with_zero(self) -> None:
        day1 = 1_704_067_200_000  # 2024-01-01
        day3 = 1_704_240_000_000  # 2024-01-03
        t1 = _make_trade(exit_ts=day1, net_pnl=100.0)
        t2 = _make_trade(exit_ts=day3, net_pnl=-25.0)
        calendar = [
            np.datetime64("2024-01-01"),
            np.datetime64("2024-01-02"),
            np.datetime64("2024-01-03"),
        ]
        result = _daily_pnl_series([t1, t2], calendar_days=calendar)
        assert result == [100.0, 0.0, -25.0]


class TestTrainingSelection:
    def test_training_score_prefers_lower_drawdown_on_tie(self) -> None:
        trades_a = [_make_trade(1_704_067_200_000, 100.0), _make_trade(1_704_153_600_000, -80.0)]
        trades_b = [_make_trade(1_704_067_200_000, 100.0), _make_trade(1_704_153_600_000, -20.0)]
        assert _training_score(trades_b, "net") > _training_score(trades_a, "net")

    def test_select_training_config_by_net(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import pandas as pd

        import hypemm.walkforward as walkforward
        from hypemm.config import StrategyConfig
        from hypemm.models import PairConfig

        prices = pd.DataFrame({"A": [1.0] * 200, "B": [1.0] * 200})
        pair = PairConfig("A", "B")
        cfg_a = StrategyConfig(pairs=(pair,), entry_z=2.0)
        cfg_b = StrategyConfig(pairs=(pair,), entry_z=2.5)

        trade_map = {
            2.0: [_make_trade(1_704_067_200_000, 100.0)],
            2.5: [_make_trade(1_704_067_200_000, 250.0)],
        }

        def fake_run_backtest_all_pairs(
            _prices: pd.DataFrame,
            config: StrategyConfig,
            funding: pd.DataFrame | None = None,
        ) -> list[CompletedTrade]:
            return trade_map[config.entry_z]

        monkeypatch.setattr(walkforward, "run_backtest_all_pairs", fake_run_backtest_all_pairs)

        name, selected_cfg, trades = _select_training_config(
            prices,
            {"cfg_a": cfg_a, "cfg_b": cfg_b},
            funding=None,
            selection_metric="net",
        )
        assert name == "cfg_b"
        assert selected_cfg == cfg_b
        assert trades == trade_map[2.5]
