"""Tests for P&L statistics: monthly breakdown, Sharpe, drawdown."""

from __future__ import annotations

from hypemm.backtest import (
    _daily_pnl_dict,
    _intra_period_drawdown,
    compute_sharpe,
    max_drawdown,
    monthly_breakdown,
)
from hypemm.models import CompletedTrade, Direction, ExitReason


def _make_trade(exit_ts: int, net_pnl: float) -> CompletedTrade:
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


class TestMonthlyBreakdown:
    def test_empty_trades(self) -> None:
        assert monthly_breakdown([]) == []

    def test_groups_by_month(self) -> None:
        jan = _make_trade(exit_ts=1_704_067_200_000, net_pnl=500.0)  # 2024-01-01
        feb = _make_trade(exit_ts=1_706_745_600_000, net_pnl=-200.0)  # 2024-02-01
        result = monthly_breakdown([jan, feb])
        assert len(result) == 2
        assert result[0]["month"] == "2024-01"
        assert result[1]["month"] == "2024-02"

    def test_aggregates_within_month(self) -> None:
        t1 = _make_trade(exit_ts=1_704_067_200_000, net_pnl=500.0)
        t2 = _make_trade(exit_ts=1_704_153_600_000, net_pnl=300.0)
        result = monthly_breakdown([t1, t2])
        assert len(result) == 1
        assert result[0]["trades"] == 2
        assert result[0]["net"] == 800.0

    def test_win_rate(self) -> None:
        t1 = _make_trade(exit_ts=1_704_067_200_000, net_pnl=500.0)
        t2 = _make_trade(exit_ts=1_704_153_600_000, net_pnl=-100.0)
        t3 = _make_trade(exit_ts=1_704_240_000_000, net_pnl=200.0)
        result = monthly_breakdown([t1, t2, t3])
        assert abs(result[0]["win_rate"] - 66.66666) < 1  # type: ignore[operator]


class TestComputeSharpe:
    def test_returns_zero_for_few_trades(self) -> None:
        trades = [_make_trade(exit_ts=1_704_067_200_000, net_pnl=100.0)]
        assert compute_sharpe(trades) == 0.0

    def test_positive_for_consistent_winners(self) -> None:
        base_ts = 1_704_067_200_000
        trades = [
            _make_trade(exit_ts=base_ts + i * 86_400_000, net_pnl=100.0 + i * 10)
            for i in range(10)
        ]
        sharpe = compute_sharpe(trades)
        assert sharpe > 0

    def test_zero_for_constant_returns(self) -> None:
        base_ts = 1_704_067_200_000
        trades = [_make_trade(exit_ts=base_ts + i * 86_400_000, net_pnl=100.0) for i in range(10)]
        assert compute_sharpe(trades) == 0.0


class TestMaxDrawdown:
    def test_empty(self) -> None:
        assert max_drawdown([]) == 0.0

    def test_no_drawdown_monotonic_wins(self) -> None:
        base_ts = 1_704_067_200_000
        trades = [_make_trade(exit_ts=base_ts + i * 86_400_000, net_pnl=100.0) for i in range(5)]
        assert max_drawdown(trades) == 0.0

    def test_drawdown_from_loss(self) -> None:
        base_ts = 1_704_067_200_000
        trades = [
            _make_trade(exit_ts=base_ts, net_pnl=1000.0),
            _make_trade(exit_ts=base_ts + 86_400_000, net_pnl=-500.0),
            _make_trade(exit_ts=base_ts + 2 * 86_400_000, net_pnl=-300.0),
        ]
        assert max_drawdown(trades) == 800.0


class TestHelpers:
    def test_daily_pnl_dict_aggregates(self) -> None:
        base_ts = 1_704_067_200_000
        t1 = _make_trade(exit_ts=base_ts, net_pnl=100.0)
        t2 = _make_trade(exit_ts=base_ts + 1000, net_pnl=50.0)
        result = _daily_pnl_dict([t1, t2])
        assert len(result) == 1
        assert list(result.values())[0] == 150.0

    def test_intra_period_drawdown(self) -> None:
        pnls = [100.0, -200.0, 50.0, -100.0]
        dd = _intra_period_drawdown(pnls)
        assert dd == 250.0

    def test_intra_period_drawdown_no_loss(self) -> None:
        pnls = [100.0, 200.0, 300.0]
        assert _intra_period_drawdown(pnls) == 0.0
