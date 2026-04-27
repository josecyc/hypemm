"""Tests for domain models."""

from __future__ import annotations

from hypemm.models import (
    CompletedTrade,
    Direction,
    ExitReason,
    HypeMMError,
    OpenPosition,
    PairConfig,
    Signal,
)


def test_pair_config_label() -> None:
    pair = PairConfig("LINK", "SOL")
    assert pair.label == "LINK/SOL"


def test_pair_config_coins() -> None:
    pair = PairConfig("DOGE", "AVAX")
    assert pair.coins == ("DOGE", "AVAX")


def test_pair_config_frozen() -> None:
    pair = PairConfig("LINK", "SOL")
    try:
        pair.coin_a = "ETH"  # type: ignore[misc]
        raised = False
    except AttributeError:
        raised = True
    assert raised


def test_direction_values() -> None:
    assert Direction.LONG_RATIO.value == 1
    assert Direction.SHORT_RATIO.value == -1


def test_signal_creation() -> None:
    pair = PairConfig("LINK", "SOL")
    sig = Signal(
        pair=pair,
        z_score=2.5,
        correlation=0.85,
        price_a=15.0,
        price_b=150.0,
        timestamp_ms=1000000,
        n_bars=100,
    )
    assert sig.z_score == 2.5
    assert sig.pair.label == "LINK/SOL"


def test_open_position_mutable_hours() -> None:
    pos = OpenPosition(
        pair=PairConfig("LINK", "SOL"),
        direction=Direction.LONG_RATIO,
        entry_z=-2.5,
        entry_price_a=15.0,
        entry_price_b=150.0,
        entry_time_ms=1000000,
        entry_correlation=0.85,
    )
    assert pos.hours_held == 0
    pos.hours_held = 5
    assert pos.hours_held == 5


def test_completed_trade_frozen() -> None:
    trade = CompletedTrade(
        pair_label="LINK/SOL",
        direction=Direction.LONG_RATIO,
        entry_ts=1000,
        exit_ts=2000,
        entry_z=-2.5,
        exit_z=-0.3,
        hours_held=10,
        entry_price_a=15.0,
        entry_price_b=150.0,
        exit_price_a=16.0,
        exit_price_b=148.0,
        pnl_leg_a=100.0,
        pnl_leg_b=50.0,
        gross_pnl=150.0,
        cost=40.0,
        net_pnl=110.0,
        exit_reason=ExitReason.MEAN_REVERT,
        entry_correlation=0.85,
    )
    assert trade.net_pnl == 110.0
    assert trade.max_adverse_excursion == 0.0


def test_exception_hierarchy() -> None:
    from hypemm.models import (
        ConfigurationError,
        DataFetchError,
        InsufficientDataError,
        StateCorruptionError,
    )

    assert issubclass(DataFetchError, HypeMMError)
    assert issubclass(InsufficientDataError, HypeMMError)
    assert issubclass(StateCorruptionError, HypeMMError)
    assert issubclass(ConfigurationError, HypeMMError)
    assert issubclass(HypeMMError, Exception)


def test_round_trip_cost_includes_fee_only_by_default() -> None:
    """Default config: fee=2 bps, slip=0 → $40 round-trip on $50K notional."""
    from hypemm.config import StrategyConfig

    cfg = StrategyConfig(pairs=(PairConfig("LINK", "SOL"),))
    # 4 sides × 50_000 × (2 + 0) bps / 10_000 = $40
    assert cfg.round_trip_cost == 40.0


def test_round_trip_cost_adds_slippage() -> None:
    """slippage_per_side_bps stacks on top of cost_per_side_bps."""
    from hypemm.config import StrategyConfig

    cfg = StrategyConfig(
        pairs=(PairConfig("LINK", "SOL"),),
        cost_per_side_bps=2.0,
        slippage_per_side_bps=3.0,
    )
    # 4 sides × 50_000 × (2 + 3) bps / 10_000 = $100
    assert cfg.round_trip_cost == 100.0
