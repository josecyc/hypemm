"""Tests for dashboard display."""

from __future__ import annotations

from hypemm.config import RiskConfig, StrategyConfig
from hypemm.dashboard import (
    _format_corr,
    _format_signal,
    _format_z,
    build_dashboard,
)
from hypemm.dashboard_loader import DashboardSnapshot
from hypemm.models import (
    CompletedTrade,
    Direction,
    ExitReason,
    OpenPosition,
    PairConfig,
    Signal,
)


def _empty_snapshot(config: StrategyConfig) -> DashboardSnapshot:
    return DashboardSnapshot(
        config=config,
        risk_config=RiskConfig(),
        start_time="2025-01-01T00:00:00+00:00",
        completed_trades=[],
        positions={p.label: None for p in config.pairs},
        cooldowns={p.label: 0 for p in config.pairs},
    )


class TestBuildDashboard:
    def test_renders_without_crash(self, default_config: StrategyConfig) -> None:
        panel = build_dashboard(_empty_snapshot(default_config))
        assert panel is not None

    def test_renders_with_signals(self, default_config: StrategyConfig) -> None:
        pair = default_config.pairs[0]
        sig = Signal(
            pair=pair,
            z_score=2.5,
            correlation=0.85,
            price_a=15.0,
            price_b=150.0,
            timestamp_ms=1000000,
            n_bars=100,
        )
        snap = _empty_snapshot(default_config)
        snap = DashboardSnapshot(
            **{**snap.__dict__, "signals": {pair.label: sig}}
        )
        panel = build_dashboard(snap)
        assert panel is not None

    def test_renders_with_trades(self, default_config: StrategyConfig) -> None:
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
        snap = _empty_snapshot(default_config)
        snap = DashboardSnapshot(**{**snap.__dict__, "completed_trades": [trade]})
        panel = build_dashboard(snap)
        assert panel is not None


class TestFormatZ:
    def test_none(self) -> None:
        assert "---" in _format_z(None, 2.0, 0.5)

    def test_high_z(self) -> None:
        result = _format_z(2.5, 2.0, 0.5)
        assert "yellow" in result
        assert "+2.50" in result

    def test_low_z(self) -> None:
        result = _format_z(0.2, 2.0, 0.5)
        assert "dim" in result

    def test_normal_z(self) -> None:
        result = _format_z(1.0, 2.0, 0.5)
        assert "yellow" not in result
        assert "dim" not in result


class TestFormatCorr:
    def test_none(self) -> None:
        assert "warm" in _format_corr(None, 0.7)

    def test_below_threshold(self) -> None:
        result = _format_corr(0.5, 0.7)
        assert "red" in result

    def test_above_threshold(self) -> None:
        result = _format_corr(0.85, 0.7)
        assert "red" not in result
        assert "0.850" in result


class TestFormatSignal:
    def test_in_position(self) -> None:
        pos = OpenPosition(
            pair=PairConfig("LINK", "SOL"),
            direction=Direction.LONG_RATIO,
            entry_z=-2.5,
            entry_price_a=15.0,
            entry_price_b=150.0,
            entry_time_ms=1000,
            entry_correlation=0.85,
        )
        config = StrategyConfig(pairs=(PairConfig("LINK", "SOL"),))
        result = _format_signal(1.0, 0.8, pos, 0, config)
        assert "in pos" in result

    def test_cooldown(self) -> None:
        config = StrategyConfig(pairs=(PairConfig("LINK", "SOL"),))
        result = _format_signal(1.0, 0.8, None, 2, config)
        assert "cool" in result

    def test_corr_blocked(self) -> None:
        config = StrategyConfig(pairs=(PairConfig("LINK", "SOL"),))
        result = _format_signal(2.5, 0.3, None, 0, config)
        assert "blocked" in result

    def test_short_signal(self) -> None:
        config = StrategyConfig(pairs=(PairConfig("LINK", "SOL"),))
        result = _format_signal(2.5, 0.85, None, 0, config)
        assert "SHORT" in result

    def test_long_signal(self) -> None:
        config = StrategyConfig(pairs=(PairConfig("LINK", "SOL"),))
        result = _format_signal(-2.5, 0.85, None, 0, config)
        assert "LONG" in result

    def test_flat(self) -> None:
        config = StrategyConfig(pairs=(PairConfig("LINK", "SOL"),))
        result = _format_signal(0.5, 0.85, None, 0, config)
        assert "flat" in result
