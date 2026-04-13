"""Tests for P&L calculations."""

from __future__ import annotations

import pytest

from hypemm.math.pnl import compute_leg_pnl, compute_trade_pnl, compute_unrealized_pnl
from hypemm.models import Direction, OpenPosition, PairConfig


class TestComputeLegPnl:
    def test_long_ratio_both_legs_profit(self) -> None:
        """Long ratio: A goes up, B goes down -> both legs profit."""
        pnl_a, pnl_b = compute_leg_pnl(
            Direction.LONG_RATIO,
            notional=50_000,
            entry_price_a=100.0,
            entry_price_b=200.0,
            exit_price_a=110.0,
            exit_price_b=190.0,
        )
        assert pnl_a == pytest.approx(5000.0)
        assert pnl_b == pytest.approx(2500.0)

    def test_short_ratio_both_legs_profit(self) -> None:
        """Short ratio: A goes down, B goes up -> both legs profit."""
        pnl_a, pnl_b = compute_leg_pnl(
            Direction.SHORT_RATIO,
            notional=50_000,
            entry_price_a=100.0,
            entry_price_b=200.0,
            exit_price_a=90.0,
            exit_price_b=210.0,
        )
        assert pnl_a == pytest.approx(5000.0)
        assert pnl_b == pytest.approx(2500.0)

    def test_long_ratio_loss(self) -> None:
        """Long ratio: A goes down -> leg A loses."""
        pnl_a, pnl_b = compute_leg_pnl(
            Direction.LONG_RATIO,
            notional=50_000,
            entry_price_a=100.0,
            entry_price_b=200.0,
            exit_price_a=95.0,
            exit_price_b=200.0,
        )
        assert pnl_a == pytest.approx(-2500.0)
        assert pnl_b == pytest.approx(0.0)

    def test_flat_prices_zero_pnl(self) -> None:
        """No price change -> zero P&L."""
        pnl_a, pnl_b = compute_leg_pnl(
            Direction.LONG_RATIO,
            notional=50_000,
            entry_price_a=100.0,
            entry_price_b=200.0,
            exit_price_a=100.0,
            exit_price_b=200.0,
        )
        assert pnl_a == pytest.approx(0.0)
        assert pnl_b == pytest.approx(0.0)

    def test_symmetry(self) -> None:
        """Long and short on same move should have opposite signs."""
        long_a, long_b = compute_leg_pnl(
            Direction.LONG_RATIO,
            notional=50_000,
            entry_price_a=100.0,
            entry_price_b=200.0,
            exit_price_a=110.0,
            exit_price_b=200.0,
        )
        short_a, short_b = compute_leg_pnl(
            Direction.SHORT_RATIO,
            notional=50_000,
            entry_price_a=100.0,
            entry_price_b=200.0,
            exit_price_a=110.0,
            exit_price_b=200.0,
        )
        assert long_a == pytest.approx(-short_a)
        assert long_b == pytest.approx(-short_b)


class TestComputeTradePnl:
    def test_includes_cost(self) -> None:
        """Net P&L should deduct round-trip costs."""
        pnl_a, pnl_b, gross, net = compute_trade_pnl(
            Direction.LONG_RATIO,
            notional=50_000,
            cost_per_side_bps=2.0,
            entry_price_a=100.0,
            entry_price_b=200.0,
            exit_price_a=110.0,
            exit_price_b=190.0,
        )
        expected_cost = 50_000 * 2 * 2.0 / 10_000 * 2  # $40
        assert gross == pytest.approx(pnl_a + pnl_b)
        assert net == pytest.approx(gross - expected_cost)

    def test_cost_calculation(self) -> None:
        """Verify the round-trip cost formula: notional * 2 * bps/10000 * 2."""
        _, _, _, net = compute_trade_pnl(
            Direction.LONG_RATIO,
            notional=50_000,
            cost_per_side_bps=2.0,
            entry_price_a=100.0,
            entry_price_b=200.0,
            exit_price_a=100.0,
            exit_price_b=200.0,
        )
        # Zero gross, minus $40 cost
        assert net == pytest.approx(-40.0)


class TestComputeUnrealizedPnl:
    def test_unrealized_long_profit(self) -> None:
        pos = OpenPosition(
            pair=PairConfig("A", "B"),
            direction=Direction.LONG_RATIO,
            entry_z=-2.5,
            entry_price_a=100.0,
            entry_price_b=200.0,
            entry_time_ms=0,
            entry_correlation=0.85,
        )
        upnl = compute_unrealized_pnl(pos, 110.0, 190.0, 50_000)
        assert upnl == pytest.approx(7500.0)

    def test_unrealized_short_loss(self) -> None:
        pos = OpenPosition(
            pair=PairConfig("A", "B"),
            direction=Direction.SHORT_RATIO,
            entry_z=2.5,
            entry_price_a=100.0,
            entry_price_b=200.0,
            entry_time_ms=0,
            entry_correlation=0.85,
        )
        # Prices move against short ratio: A up, B down
        upnl = compute_unrealized_pnl(pos, 110.0, 190.0, 50_000)
        assert upnl == pytest.approx(-7500.0)
