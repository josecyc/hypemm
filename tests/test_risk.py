"""Tests for the portfolio-level risk monitor."""

from __future__ import annotations

from dataclasses import replace

import pytest

from hypemm.config import RiskConfig, StrategyConfig
from hypemm.engine import StrategyEngine
from hypemm.models import (
    CompletedTrade,
    Direction,
    ExitReason,
    OpenPosition,
    PairConfig,
    Signal,
)
from hypemm.risk import RiskStatus, compute_risk_report


PAIR = PairConfig("LINK", "SOL")
NOTIONAL = 50_000.0
NOW_MS = 1_700_000_000_000  # arbitrary fixed reference timestamp


def _signal(z: float = 0.0, corr: float | None = 0.85) -> Signal:
    return Signal(
        pair=PAIR,
        z_score=z,
        correlation=corr,
        price_a=10.0,
        price_b=100.0,
        timestamp_ms=NOW_MS,
        n_bars=100,
    )


def _trade(
    *,
    net: float,
    exit_offset_hours: int = 0,
    reason: ExitReason = ExitReason.MEAN_REVERT,
) -> CompletedTrade:
    exit_ts = NOW_MS - exit_offset_hours * 3_600_000
    return CompletedTrade(
        pair_label=PAIR.label,
        direction=Direction.LONG_RATIO,
        entry_ts=exit_ts - 5 * 3_600_000,
        exit_ts=exit_ts,
        entry_z=-2.5,
        exit_z=-0.4,
        hours_held=5,
        entry_price_a=10.0,
        entry_price_b=100.0,
        exit_price_a=10.1,
        exit_price_b=99.5,
        pnl_leg_a=net / 2,
        pnl_leg_b=net / 2,
        gross_pnl=net,
        cost=40.0,
        net_pnl=net,
        exit_reason=reason,
        entry_correlation=0.85,
    )


@pytest.fixture
def engine() -> StrategyEngine:
    return StrategyEngine(StrategyConfig(pairs=(PAIR,)))


@pytest.fixture
def risk_cfg() -> RiskConfig:
    return RiskConfig()


def _signal_named(report, name):
    matches = [s for s in report.signals if s.name == name]
    assert len(matches) == 1, f"expected one {name} signal, got {len(matches)}"
    return matches[0]


# -- daily_pnl -------------------------------------------------------------


def test_daily_pnl_ok_when_flat(engine, risk_cfg):
    report = compute_risk_report(engine, {}, [], risk_cfg, NOTIONAL, now_ms=NOW_MS)
    sig = _signal_named(report, "daily_pnl")
    assert sig.status == RiskStatus.OK
    assert sig.halts_entry is False


def test_daily_pnl_halts_on_loss_threshold(engine, risk_cfg):
    trades = [_trade(net=-6_000, exit_offset_hours=2)]  # within 24h, exceeds halt
    report = compute_risk_report(engine, {}, trades, risk_cfg, NOTIONAL, now_ms=NOW_MS)
    sig = _signal_named(report, "daily_pnl")
    assert sig.status == RiskStatus.HALT
    assert sig.halts_entry is True
    assert report.halts_entry is True


def test_daily_pnl_warns_at_half_threshold(engine, risk_cfg):
    trades = [_trade(net=-3_000, exit_offset_hours=1)]  # half of -5k halt
    report = compute_risk_report(engine, {}, trades, risk_cfg, NOTIONAL, now_ms=NOW_MS)
    sig = _signal_named(report, "daily_pnl")
    assert sig.status == RiskStatus.WARN
    assert sig.halts_entry is False


def test_daily_pnl_ignores_old_trades(engine, risk_cfg):
    trades = [_trade(net=-10_000, exit_offset_hours=48)]  # 48h ago — outside 24h
    report = compute_risk_report(engine, {}, trades, risk_cfg, NOTIONAL, now_ms=NOW_MS)
    sig = _signal_named(report, "daily_pnl")
    assert sig.status == RiskStatus.OK


# -- concurrent_unrealized -------------------------------------------------


def test_concurrent_unrealized_no_position(engine, risk_cfg):
    report = compute_risk_report(engine, {}, [], risk_cfg, NOTIONAL, now_ms=NOW_MS)
    sig = _signal_named(report, "concurrent_unrealized")
    assert sig.status == RiskStatus.OK
    assert sig.value == 0.0


def test_concurrent_unrealized_warn_then_halt(engine, risk_cfg):
    # Long ratio at entry; price A drops, price B rises → big loss
    engine.positions[PAIR.label] = OpenPosition(
        pair=PAIR,
        direction=Direction.LONG_RATIO,
        entry_z=-2.5,
        entry_price_a=10.0,
        entry_price_b=100.0,
        entry_time_ms=NOW_MS - 3_600_000,
        entry_correlation=0.85,
        hours_held=1,
    )
    # Loss: leg_a -10% (-$5K) + leg_b -10% (-$5K) = -$10K → WARN
    sig = _signal(corr=0.85)
    sig = replace(sig, price_a=9.0, price_b=110.0)
    report = compute_risk_report(
        engine, {PAIR.label: sig}, [], risk_cfg, NOTIONAL, now_ms=NOW_MS
    )
    s = _signal_named(report, "concurrent_unrealized")
    assert s.status == RiskStatus.WARN

    # Push to -$15K loss → HALT
    sig = replace(sig, price_a=8.5, price_b=115.0)
    report = compute_risk_report(
        engine, {PAIR.label: sig}, [], risk_cfg, NOTIONAL, now_ms=NOW_MS
    )
    s = _signal_named(report, "concurrent_unrealized")
    assert s.status == RiskStatus.HALT
    assert s.halts_entry is True


# -- win_rate_drift --------------------------------------------------------


def test_win_rate_warmup_under_min_trades(engine, risk_cfg):
    trades = [_trade(net=-100, exit_offset_hours=i + 1) for i in range(5)]
    report = compute_risk_report(engine, {}, trades, risk_cfg, NOTIONAL, now_ms=NOW_MS)
    sig = _signal_named(report, "win_rate_drift")
    assert sig.status == RiskStatus.OK
    assert "warming up" in sig.detail


def test_win_rate_warns_below_threshold(engine, risk_cfg):
    # 10 trades, only 3 wins = 30% WR (< 55%)
    trades = [_trade(net=100, exit_offset_hours=i + 100) for i in range(3)]
    trades += [_trade(net=-100, exit_offset_hours=i + 200) for i in range(7)]
    report = compute_risk_report(engine, {}, trades, risk_cfg, NOTIONAL, now_ms=NOW_MS)
    sig = _signal_named(report, "win_rate_drift")
    assert sig.status == RiskStatus.WARN
    assert sig.value == pytest.approx(0.3)


def test_win_rate_ok_above_threshold(engine, risk_cfg):
    trades = [_trade(net=100, exit_offset_hours=i + 100) for i in range(8)]
    trades += [_trade(net=-100, exit_offset_hours=i + 200) for i in range(2)]
    report = compute_risk_report(engine, {}, trades, risk_cfg, NOTIONAL, now_ms=NOW_MS)
    sig = _signal_named(report, "win_rate_drift")
    assert sig.status == RiskStatus.OK


# -- time_stop_drift -------------------------------------------------------


def test_time_stop_warns_when_too_many(engine, risk_cfg):
    # 10 trades, 4 time_stops = 40% (> 30% warn)
    trades = [
        _trade(net=10, exit_offset_hours=i + 100, reason=ExitReason.TIME_STOP)
        for i in range(4)
    ]
    trades += [_trade(net=10, exit_offset_hours=i + 200) for i in range(6)]
    report = compute_risk_report(engine, {}, trades, risk_cfg, NOTIONAL, now_ms=NOW_MS)
    sig = _signal_named(report, "time_stop_drift")
    assert sig.status == RiskStatus.WARN


def test_time_stop_ok_when_under(engine, risk_cfg):
    trades = [
        _trade(net=10, exit_offset_hours=i + 100, reason=ExitReason.TIME_STOP)
        for i in range(2)
    ]
    trades += [_trade(net=10, exit_offset_hours=i + 200) for i in range(8)]
    report = compute_risk_report(engine, {}, trades, risk_cfg, NOTIONAL, now_ms=NOW_MS)
    sig = _signal_named(report, "time_stop_drift")
    assert sig.status == RiskStatus.OK


# -- correlation_drift -----------------------------------------------------


def test_correlation_drift_only_flags_active_pairs(engine, risk_cfg):
    sig = _signal(corr=0.5)  # below warn (0.65), but no open position
    report = compute_risk_report(
        engine, {PAIR.label: sig}, [], risk_cfg, NOTIONAL, now_ms=NOW_MS
    )
    s = _signal_named(report, "correlation_drift")
    assert s.status == RiskStatus.OK


def test_correlation_drift_warns_when_active_breaks(engine, risk_cfg):
    engine.positions[PAIR.label] = OpenPosition(
        pair=PAIR,
        direction=Direction.LONG_RATIO,
        entry_z=-2.5,
        entry_price_a=10.0,
        entry_price_b=100.0,
        entry_time_ms=NOW_MS - 3_600_000,
        entry_correlation=0.85,
        hours_held=1,
    )
    sig = _signal(corr=0.4)
    report = compute_risk_report(
        engine, {PAIR.label: sig}, [], risk_cfg, NOTIONAL, now_ms=NOW_MS
    )
    s = _signal_named(report, "correlation_drift")
    assert s.status == RiskStatus.WARN
    assert s.halts_entry is False  # warn-only by design


# -- aggregate -------------------------------------------------------------


def test_report_halts_when_any_signal_halts(engine, risk_cfg):
    trades = [_trade(net=-7_000, exit_offset_hours=1)]
    report = compute_risk_report(engine, {}, trades, risk_cfg, NOTIONAL, now_ms=NOW_MS)
    assert report.halts_entry is True
    assert report.worst_status == RiskStatus.HALT


def test_report_worst_status_promotes_to_warn(engine, risk_cfg):
    trades = [_trade(net=-3_000, exit_offset_hours=1)]
    report = compute_risk_report(engine, {}, trades, risk_cfg, NOTIONAL, now_ms=NOW_MS)
    assert report.halts_entry is False
    assert report.worst_status == RiskStatus.WARN


def test_engine_halt_blocks_new_entries():
    cfg = StrategyConfig(pairs=(PAIR,), entry_z=2.0, corr_threshold=0.7)
    engine = StrategyEngine(cfg)
    sig = _signal(z=3.0, corr=0.85)  # would normally trigger SHORT_RATIO
    engine.halt_entries = True
    orders = engine.process_bar({PAIR.label: sig}, NOW_MS)
    assert orders == [], "halt_entries should block new entries"

    engine.halt_entries = False
    orders = engine.process_bar({PAIR.label: sig}, NOW_MS)
    assert len(orders) == 1
