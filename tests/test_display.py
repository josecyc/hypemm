"""Tests for dashboard display."""

from __future__ import annotations

from rich.console import Console

from hypemm.config import RiskConfig, StrategyConfig
from hypemm.dashboard import (
    _baseline_lines,
    _format_corr,
    _format_signal,
    _format_z,
    build_dashboard,
    build_trades_log_table,
)
from hypemm.dashboard_loader import BacktestBaseline, DashboardSnapshot
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
        snap = DashboardSnapshot(**{**snap.__dict__, "signals": {pair.label: sig}})
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


def _baseline() -> BacktestBaseline:
    return BacktestBaseline(
        date_range="2025-09-03 → 2026-03-31",
        n_days=208,
        total_trades=725,
        win_rate_pct=74.0,
        total_net=71250.0,
        sharpe=2.56,
        max_drawdown=38442.0,
    )


class TestBaselineLines:
    def test_renders_baseline_metrics(self) -> None:
        lines = _baseline_lines(
            _baseline(), live_trades=20, live_wr_pct=70.0, live_daily_rate=300.0
        )
        joined = "\n".join(lines)
        assert "Backtest baseline" in joined
        assert "208d" in joined
        assert "74%" in joined  # baseline WR
        assert "Sharpe" in joined and "2.56" in joined

    def test_below_threshold_shows_hint(self) -> None:
        lines = _baseline_lines(
            _baseline(), live_trades=3, live_wr_pct=66.0, live_daily_rate=100.0
        )
        joined = "\n".join(lines)
        assert "need ≥10 live trades" in joined
        assert "3 so far" in joined

    def test_delta_red_when_below_baseline(self) -> None:
        lines = _baseline_lines(
            _baseline(), live_trades=20, live_wr_pct=60.0, live_daily_rate=100.0
        )
        delta = lines[-1]
        assert "vs baseline" in delta
        # Live WR (60) < baseline WR (74) → delta is negative → red
        assert "red" in delta
        assert "-14pp" in delta

    def test_delta_green_when_at_or_above_baseline(self) -> None:
        lines = _baseline_lines(
            _baseline(), live_trades=20, live_wr_pct=80.0, live_daily_rate=400.0
        )
        delta = lines[-1]
        assert "green" in delta
        assert "+6pp" in delta

    def test_dashboard_renders_with_baseline(self, default_config: StrategyConfig) -> None:
        snap = _empty_snapshot(default_config)
        snap = DashboardSnapshot(**{**snap.__dict__, "baseline": _baseline()})
        panel = build_dashboard(snap)
        assert panel is not None


def _make_trade(
    pair: str = "LINK/SOL",
    *,
    entry_ts: int = 1_700_000_000_000,
    exit_ts: int = 1_700_000_036_000_000 // 1000,  # ~10h later
    entry_z: float = -2.5,
    exit_z: float = -0.3,
    entry_corr: float = 0.85,
    net: float = 110.0,
) -> CompletedTrade:
    return CompletedTrade(
        pair_label=pair,
        direction=Direction.LONG_RATIO,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        entry_z=entry_z,
        exit_z=exit_z,
        hours_held=10,
        entry_price_a=15.0,
        entry_price_b=150.0,
        exit_price_a=16.0,
        exit_price_b=148.0,
        pnl_leg_a=net / 2,
        pnl_leg_b=net / 2,
        gross_pnl=net,
        cost=40.0,
        net_pnl=net,
        exit_reason=ExitReason.MEAN_REVERT,
        entry_correlation=entry_corr,
    )


def _render_table(table) -> str:  # type: ignore[no-untyped-def]
    """Render a Rich Table to a plain string for assertion."""
    console = Console(width=200, record=True, file=None)
    console.print(table)
    return console.export_text()


class TestBuildTradesLogTable:
    def test_full_mode_includes_full_datetime_z_and_corr(self) -> None:
        # 2023-11-14 22:13:20 UTC for entry_ts=1_700_000_000_000
        trade = _make_trade(
            entry_ts=1_700_000_000_000,
            exit_ts=1_700_000_000_000 + 10 * 3600 * 1000,
            entry_z=-2.50,
            exit_z=-0.31,
            entry_corr=0.87,
        )
        out = _render_table(build_trades_log_table([trade]))
        assert "2023-11-14 22:13" in out  # entry datetime
        assert "2023-11-15 08:13" in out  # exit datetime, 10h later
        assert "-2.50" in out  # entry z
        assert "-0.31" in out  # exit z
        assert "0.87" in out  # entry corr
        assert "Hold" in out  # full mode keeps Hold column

    def test_compact_mode_drops_hold_and_uses_short_dates(self) -> None:
        trade = _make_trade(
            entry_ts=1_700_000_000_000,
            exit_ts=1_700_000_000_000 + 10 * 3600 * 1000,
        )
        out = _render_table(build_trades_log_table([trade], compact=True))
        assert "Hold" not in out
        assert "11-14 22:13" in out  # MM-DD HH:MM
        assert "2023-11-14" not in out  # year stripped in compact mode

    def test_max_rows_caps_output(self) -> None:
        trades = [_make_trade(pair=f"P{i}/X") for i in range(30)]
        out = _render_table(build_trades_log_table(trades, max_rows=5))
        # Only the last 5 (P25..P29) should appear
        assert "P29/X" in out
        assert "P25/X" in out
        assert "P24/X" not in out

    def test_max_rows_none_renders_all(self) -> None:
        trades = [_make_trade(pair=f"P{i}/X") for i in range(12)]
        out = _render_table(build_trades_log_table(trades, max_rows=None))
        assert "P0/X" in out
        assert "P11/X" in out

    def test_custom_title(self) -> None:
        out = _render_table(build_trades_log_table([_make_trade()], title="My Trades (3 of 99)"))
        assert "My Trades (3 of 99)" in out
