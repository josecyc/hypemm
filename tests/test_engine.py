"""Tests for the strategy engine."""

from __future__ import annotations

import pytest

from hypemm.config import StrategyConfig
from hypemm.engine import StrategyEngine
from hypemm.models import (
    Direction,
    EntryOrder,
    ExitOrder,
    ExitReason,
    FillReport,
    PairConfig,
    Signal,
)


def _make_signal(
    pair: PairConfig,
    z: float,
    corr: float | None = 0.9,
    price_a: float = 15.0,
    price_b: float = 150.0,
) -> Signal:
    return Signal(
        pair=pair,
        z_score=z,
        correlation=corr,
        price_a=price_a,
        price_b=price_b,
        timestamp_ms=0,
        n_bars=100,
    )


def _fill(price_a: float, price_b: float) -> FillReport:
    """Build a FillReport for tests that only care about prices."""
    return FillReport(
        price_a=price_a,
        price_b=price_b,
        size_a=1.0,
        size_b=1.0,
        fee_a=0.0,
        fee_b=0.0,
    )


def _config_with_pair(pair: PairConfig) -> StrategyConfig:
    return StrategyConfig(pairs=(pair,))


class TestEntryLogic:
    def test_entry_short_ratio_on_high_z(self) -> None:
        pair = PairConfig("LINK", "SOL")
        engine = StrategyEngine(_config_with_pair(pair))
        signal = _make_signal(pair, z=2.5, corr=0.9)
        orders = engine.process_bar({pair.label: signal}, timestamp_ms=0)

        assert len(orders) == 1
        assert isinstance(orders[0], EntryOrder)
        assert orders[0].direction == Direction.SHORT_RATIO

    def test_entry_long_ratio_on_low_z(self) -> None:
        pair = PairConfig("LINK", "SOL")
        engine = StrategyEngine(_config_with_pair(pair))
        signal = _make_signal(pair, z=-2.5, corr=0.9)
        orders = engine.process_bar({pair.label: signal}, timestamp_ms=0)

        assert len(orders) == 1
        assert isinstance(orders[0], EntryOrder)
        assert orders[0].direction == Direction.LONG_RATIO

    def test_no_entry_when_z_below_threshold(self) -> None:
        pair = PairConfig("LINK", "SOL")
        engine = StrategyEngine(_config_with_pair(pair))
        signal = _make_signal(pair, z=1.5, corr=0.9)
        orders = engine.process_bar({pair.label: signal}, timestamp_ms=0)
        assert len(orders) == 0

    def test_no_entry_when_correlation_below_threshold(self) -> None:
        pair = PairConfig("LINK", "SOL")
        engine = StrategyEngine(_config_with_pair(pair))
        signal = _make_signal(pair, z=2.5, corr=0.5)
        orders = engine.process_bar({pair.label: signal}, timestamp_ms=0)
        assert len(orders) == 0

    def test_no_entry_when_correlation_none(self) -> None:
        pair = PairConfig("LINK", "SOL")
        engine = StrategyEngine(_config_with_pair(pair))
        signal = _make_signal(pair, z=2.5, corr=None)
        orders = engine.process_bar({pair.label: signal}, timestamp_ms=0)
        assert len(orders) == 0


class TestExitLogic:
    def test_mean_revert_exit_long(self) -> None:
        """Long ratio entered at z < -2.0, exits when z >= -0.5."""
        pair = PairConfig("LINK", "SOL")
        engine = StrategyEngine(_config_with_pair(pair))

        entry_sig = _make_signal(pair, z=-2.5, corr=0.9, price_a=15.0, price_b=150.0)
        orders = engine.process_bar({pair.label: entry_sig}, timestamp_ms=1000)
        engine.confirm_entry(orders[0], _fill(15.0, 150.0), 1000)

        exit_sig = _make_signal(pair, z=-0.3, corr=0.9, price_a=16.0, price_b=149.0)
        orders = engine.process_bar({pair.label: exit_sig}, timestamp_ms=2000)
        assert len(orders) == 1
        assert isinstance(orders[0], ExitOrder)
        assert orders[0].reason == ExitReason.MEAN_REVERT

    def test_mean_revert_exit_short(self) -> None:
        """Short ratio entered at z > 2.0, exits when z <= 0.5."""
        pair = PairConfig("LINK", "SOL")
        engine = StrategyEngine(_config_with_pair(pair))

        entry_sig = _make_signal(pair, z=2.5, corr=0.9, price_a=15.0, price_b=150.0)
        orders = engine.process_bar({pair.label: entry_sig}, timestamp_ms=1000)
        engine.confirm_entry(orders[0], _fill(15.0, 150.0), 1000)

        exit_sig = _make_signal(pair, z=0.3, corr=0.9, price_a=14.0, price_b=151.0)
        orders = engine.process_bar({pair.label: exit_sig}, timestamp_ms=2000)
        assert len(orders) == 1
        assert isinstance(orders[0], ExitOrder)
        assert orders[0].reason == ExitReason.MEAN_REVERT

    def test_no_exit_when_z_not_reverted(self) -> None:
        """Long ratio at z=-0.6, should NOT exit (threshold is -0.5)."""
        pair = PairConfig("LINK", "SOL")
        engine = StrategyEngine(_config_with_pair(pair))

        entry_sig = _make_signal(pair, z=-2.5, corr=0.9)
        orders = engine.process_bar({pair.label: entry_sig}, timestamp_ms=1000)
        engine.confirm_entry(orders[0], _fill(15.0, 150.0), 1000)

        hold_sig = _make_signal(pair, z=-0.6)
        orders = engine.process_bar({pair.label: hold_sig}, timestamp_ms=2000)
        assert len(orders) == 0

    def test_stop_loss_with_wide_exit(self) -> None:
        """Stop loss triggers when exit_z is tight enough that mean_revert doesn't catch it."""
        pair = PairConfig("LINK", "SOL")
        config = StrategyConfig(pairs=(pair,), exit_z=-5.0, stop_loss_z=4.0)
        engine = StrategyEngine(config)

        entry_sig = _make_signal(pair, z=-2.5, corr=0.9)
        orders = engine.process_bar({pair.label: entry_sig}, timestamp_ms=1000)
        engine.confirm_entry(orders[0], _fill(15.0, 150.0), 1000)

        stop_sig = _make_signal(pair, z=4.5)
        orders = engine.process_bar({pair.label: stop_sig}, timestamp_ms=2000)
        assert len(orders) == 1
        assert isinstance(orders[0], ExitOrder)
        assert orders[0].reason == ExitReason.STOP_LOSS

    def test_time_stop(self) -> None:
        pair = PairConfig("LINK", "SOL")
        config = StrategyConfig(pairs=(pair,), max_hold_hours=3)
        engine = StrategyEngine(config)

        entry_sig = _make_signal(pair, z=-2.5, corr=0.9)
        orders = engine.process_bar({pair.label: entry_sig}, timestamp_ms=1000)
        engine.confirm_entry(orders[0], _fill(15.0, 150.0), 1000)

        for i in range(3):
            hold_sig = _make_signal(pair, z=-1.5)
            orders = engine.process_bar({pair.label: hold_sig}, timestamp_ms=2000 + i * 3600_000)
            if orders:
                assert orders[0].reason == ExitReason.TIME_STOP  # type: ignore[union-attr]
                return

        raise AssertionError("Time stop should have triggered")


class TestCooldown:
    def test_cooldown_prevents_reentry(self) -> None:
        pair = PairConfig("LINK", "SOL")
        config = StrategyConfig(pairs=(pair,), cooldown_hours=2)
        engine = StrategyEngine(config)

        entry_sig = _make_signal(pair, z=-2.5, corr=0.9)
        orders = engine.process_bar({pair.label: entry_sig}, timestamp_ms=1000)
        engine.confirm_entry(orders[0], _fill(15.0, 150.0), 1000)

        exit_sig = _make_signal(pair, z=-0.3, corr=0.9)
        orders = engine.process_bar({pair.label: exit_sig}, timestamp_ms=2000)
        engine.confirm_exit(orders[0], _fill(16.0, 149.0), 2000)

        re_entry_sig = _make_signal(pair, z=-2.5, corr=0.9)
        orders = engine.process_bar({pair.label: re_entry_sig}, timestamp_ms=3000)
        assert len(orders) == 0

        orders = engine.process_bar({pair.label: re_entry_sig}, timestamp_ms=4000)
        assert len(orders) == 0

        orders = engine.process_bar({pair.label: re_entry_sig}, timestamp_ms=5000)
        assert len(orders) == 1


class TestConfirmFills:
    def test_confirm_entry_registers_position(self) -> None:
        pair = PairConfig("LINK", "SOL")
        engine = StrategyEngine(_config_with_pair(pair))

        entry_sig = _make_signal(pair, z=-2.5, corr=0.85)
        orders = engine.process_bar({pair.label: entry_sig}, timestamp_ms=1000)
        pos = engine.confirm_entry(orders[0], _fill(15.0, 150.0), 1000)

        assert pos.pair == pair
        assert pos.direction == Direction.LONG_RATIO
        assert engine.positions[pair.label] is not None

    def test_confirm_exit_returns_completed_trade(self) -> None:
        pair = PairConfig("LINK", "SOL")
        engine = StrategyEngine(_config_with_pair(pair))

        entry_sig = _make_signal(pair, z=-2.5, corr=0.85, price_a=15.0, price_b=150.0)
        orders = engine.process_bar({pair.label: entry_sig}, timestamp_ms=1000)
        # Non-zero fees on the entry leg so the cost-deduction assertion
        # exercises the new FillReport-driven cost computation.
        entry_fill = FillReport(
            price_a=15.0, price_b=150.0, size_a=1.0, size_b=1.0, fee_a=0.5, fee_b=0.5
        )
        engine.confirm_entry(orders[0], entry_fill, 1000)

        exit_sig = _make_signal(pair, z=-0.3, corr=0.85, price_a=16.0, price_b=149.0)
        orders = engine.process_bar({pair.label: exit_sig}, timestamp_ms=2000)
        exit_fill = FillReport(
            price_a=16.0, price_b=149.0, size_a=1.0, size_b=1.0, fee_a=0.5, fee_b=0.5
        )
        trade = engine.confirm_exit(orders[0], exit_fill, 2000)

        assert trade.pair_label == "LINK/SOL"
        assert trade.direction == Direction.LONG_RATIO
        assert trade.exit_reason == ExitReason.MEAN_REVERT
        # cost = sum of the four leg fees → net_pnl = gross_pnl − 2.0
        assert trade.cost == pytest.approx(2.0)
        assert trade.net_pnl == pytest.approx(trade.gross_pnl - 2.0)
        assert engine.positions[pair.label] is None


class TestStatePersistence:
    def test_get_and_load_state_round_trip(self) -> None:
        pair = PairConfig("LINK", "SOL")
        engine = StrategyEngine(_config_with_pair(pair))

        entry_sig = _make_signal(pair, z=-2.5, corr=0.85)
        orders = engine.process_bar({pair.label: entry_sig}, timestamp_ms=1000)
        engine.confirm_entry(orders[0], _fill(15.0, 150.0), 1000)

        state = engine.get_state()

        engine2 = StrategyEngine(_config_with_pair(pair))
        engine2.load_state(state)

        pos = engine2.positions[pair.label]
        assert pos is not None
        assert pos.direction == Direction.LONG_RATIO
        assert pos.entry_price_a == 15.0
        assert pos.hours_held == 0

    def test_state_round_trip_preserves_funding_paid(self) -> None:
        pair = PairConfig("LINK", "SOL")
        engine = StrategyEngine(_config_with_pair(pair))

        entry_sig = _make_signal(pair, z=-2.5, corr=0.85)
        orders = engine.process_bar({pair.label: entry_sig}, timestamp_ms=1000)
        pos = engine.confirm_entry(orders[0], _fill(15.0, 150.0), 1000)
        pos.funding_paid = 12.34

        state = engine.get_state()

        engine2 = StrategyEngine(_config_with_pair(pair))
        engine2.load_state(state)
        restored = engine2.positions[pair.label]
        assert restored is not None
        assert restored.funding_paid == 12.34

    def test_load_state_defaults_funding_paid_when_missing(self) -> None:
        """Backward compat: state files written before funding_paid field exists."""
        pair = PairConfig("LINK", "SOL")
        engine = StrategyEngine(_config_with_pair(pair))
        legacy_state = {
            "positions": {
                pair.label: {
                    "coin_a": "LINK",
                    "coin_b": "SOL",
                    "direction": int(Direction.LONG_RATIO),
                    "entry_z": -2.5,
                    "entry_price_a": 15.0,
                    "entry_price_b": 150.0,
                    "entry_time_ms": 1000,
                    "entry_correlation": 0.85,
                    "hours_held": 2,
                    # funding_paid intentionally missing
                }
            },
            "cooldowns": {pair.label: 0},
        }
        engine.load_state(legacy_state)
        pos = engine.positions[pair.label]
        assert pos is not None
        assert pos.funding_paid == 0.0
