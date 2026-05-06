"""Tests for the HL ledger reconcile report."""

from __future__ import annotations

import pytest

from hypemm.models import CompletedTrade, Direction, ExitReason
from hypemm.reconcile_pnl import build_report, suggest_taker_fee_bps


def _trade(
    *,
    pair_label: str,
    entry_ts: int,
    exit_ts: int,
    entry_oid_a: int,
    entry_oid_b: int,
    exit_oid_a: int,
    exit_oid_b: int,
    entry_size_a: float = 100.0,
    entry_size_b: float = 100.0,
    entry_price_a: float = 10.0,
    entry_price_b: float = 10.0,
    exit_price_a: float = 10.5,
    exit_price_b: float = 9.5,
    entry_fee_a: float = 0.1,
    entry_fee_b: float = 0.1,
    exit_fee_a: float = 0.1,
    exit_fee_b: float = 0.1,
    funding_cost: float = 0.0,
    pnl_leg_a: float = 50.0,
    pnl_leg_b: float = 50.0,
) -> CompletedTrade:
    cost = entry_fee_a + entry_fee_b + exit_fee_a + exit_fee_b
    gross = pnl_leg_a + pnl_leg_b
    return CompletedTrade(
        pair_label=pair_label,
        direction=Direction.LONG_RATIO,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        entry_z=-2.5,
        exit_z=-0.3,
        hours_held=1,
        entry_price_a=entry_price_a,
        entry_price_b=entry_price_b,
        exit_price_a=exit_price_a,
        exit_price_b=exit_price_b,
        pnl_leg_a=pnl_leg_a,
        pnl_leg_b=pnl_leg_b,
        gross_pnl=gross,
        cost=cost,
        net_pnl=gross - cost - funding_cost,
        exit_reason=ExitReason.MEAN_REVERT,
        entry_correlation=0.9,
        funding_cost=funding_cost,
        entry_size_a=entry_size_a,
        entry_size_b=entry_size_b,
        entry_fee_a=entry_fee_a,
        entry_fee_b=entry_fee_b,
        exit_fee_a=exit_fee_a,
        exit_fee_b=exit_fee_b,
        entry_oid_a=entry_oid_a,
        entry_oid_b=entry_oid_b,
        exit_oid_a=exit_oid_a,
        exit_oid_b=exit_oid_b,
    )


def _fill(
    oid: int,
    coin: str,
    direction: str,
    sz: float,
    px: float,
    fee: float,
    closed_pnl: float,
    time_ms: int,
) -> dict[str, object]:
    return {
        "oid": oid,
        "coin": coin,
        "dir": direction,
        "sz": str(sz),
        "px": str(px),
        "fee": str(fee),
        "closedPnl": str(closed_pnl),
        "time": time_ms,
    }


def test_matched_trade_zero_drift_when_modeled_equals_actual() -> None:
    trade = _trade(
        pair_label="LINK/SOL",
        entry_ts=1_000_000,
        exit_ts=2_000_000,
        entry_oid_a=11,
        entry_oid_b=12,
        exit_oid_a=21,
        exit_oid_b=22,
        entry_fee_a=0.05,
        entry_fee_b=0.05,
        exit_fee_a=0.05,
        exit_fee_b=0.05,
    )
    fills = [
        _fill(11, "LINK", "Open Long", 1, 10, 0.05, 0.0, 1_000_000),
        _fill(12, "SOL", "Open Short", 1, 10, 0.05, 0.0, 1_000_000),
        _fill(21, "LINK", "Close Long", 1, 10.5, 0.05, 50.0, 2_000_000),
        _fill(22, "SOL", "Close Short", 1, 9.5, 0.05, 50.0, 2_000_000),
    ]
    report = build_report([trade], fills, [], 1_000_000, 2_000_000)

    assert len(report.trades) == 1
    rec = report.trades[0]
    assert rec.modeled_fees == 0.20
    assert rec.actual_fees == 0.20
    assert rec.fee_drift == 0.0
    assert report.unmatched_fills == []


def test_fee_drift_surfaced_when_actual_exceeds_modeled() -> None:
    """Real-world case from the live run: modeled 2 bps but HL actually
    charged 4.5 bps. The drift should surface so the user can re-calibrate."""
    trade = _trade(
        pair_label="LINK/SOL",
        entry_ts=1_000_000,
        exit_ts=2_000_000,
        entry_oid_a=11,
        entry_oid_b=12,
        exit_oid_a=21,
        exit_oid_b=22,
        entry_fee_a=0.02,
        entry_fee_b=0.02,
        exit_fee_a=0.02,
        exit_fee_b=0.02,
    )
    fills = [
        _fill(11, "LINK", "Open Long", 1, 10, 0.045, 0.0, 1_000_000),
        _fill(12, "SOL", "Open Short", 1, 10, 0.045, 0.0, 1_000_000),
        _fill(21, "LINK", "Close Long", 1, 10.5, 0.045, 50.0, 2_000_000),
        _fill(22, "SOL", "Close Short", 1, 9.5, 0.045, 50.0, 2_000_000),
    ]
    report = build_report([trade], fills, [], 1_000_000, 2_000_000)

    rec = report.trades[0]
    assert rec.modeled_fees == pytest.approx(0.08)
    assert rec.actual_fees == pytest.approx(0.18)
    # Net underestimated by 0.10 → CSV reports too-rosy net_pnl
    assert rec.fee_drift == pytest.approx(0.10)


def test_unmatched_fill_flagged() -> None:
    """A residual flatten or manual intervention has no oid in any CSV trade."""
    trade = _trade(
        pair_label="LINK/SOL",
        entry_ts=1_000_000,
        exit_ts=2_000_000,
        entry_oid_a=11,
        entry_oid_b=12,
        exit_oid_a=21,
        exit_oid_b=22,
    )
    fills = [
        _fill(11, "LINK", "Open Long", 1, 10, 0.05, 0.0, 1_000_000),
        _fill(12, "SOL", "Open Short", 1, 10, 0.05, 0.0, 1_000_000),
        _fill(21, "LINK", "Close Long", 1, 10.5, 0.05, 50.0, 2_000_000),
        _fill(22, "SOL", "Close Short", 1, 9.5, 0.05, 50.0, 2_000_000),
        # Orphan: residual flatten 30 minutes later, oid that no CSV row knows about
        _fill(99, "AVAX", "Close Long", 0.03, 9.17, 0.0001, 0.002, 2_001_000),
    ]
    report = build_report([trade], fills, [], 1_000_000, 2_001_000)

    assert len(report.unmatched_fills) == 1
    assert report.unmatched_fills[0].coin == "AVAX"
    assert report.unmatched_fills[0].closed_pnl == 0.002


def test_funding_attribution_to_open_position_window() -> None:
    """userFunding events between entry_ts and exit_ts get attributed to the trade."""
    trade = _trade(
        pair_label="LINK/SOL",
        entry_ts=1_000_000,
        exit_ts=2_000_000,
        entry_oid_a=11,
        entry_oid_b=12,
        exit_oid_a=21,
        exit_oid_b=22,
        funding_cost=0.0,
    )
    fills = [
        _fill(11, "LINK", "Open Long", 1, 10, 0.05, 0.0, 1_000_000),
        _fill(12, "SOL", "Open Short", 1, 10, 0.05, 0.0, 1_000_000),
        _fill(21, "LINK", "Close Long", 1, 10.5, 0.05, 50.0, 2_000_000),
        _fill(22, "SOL", "Close Short", 1, 9.5, 0.05, 50.0, 2_000_000),
    ]
    funding = [
        {"time": 1_500_000, "delta": {"type": "funding", "coin": "LINK", "usdc": "0.01"}},
        {"time": 1_500_000, "delta": {"type": "funding", "coin": "SOL", "usdc": "-0.005"}},
        # outside trade window — should not be attributed
        {"time": 3_000_000, "delta": {"type": "funding", "coin": "LINK", "usdc": "0.99"}},
    ]
    report = build_report([trade], fills, funding, 1_000_000, 3_000_000)

    rec = report.trades[0]
    assert rec.actual_funding == pytest.approx(0.005)  # 0.01 - 0.005
    # hl_funding sums ALL funding events in the window, not just the matched ones
    assert report.hl_funding == pytest.approx(0.01 - 0.005 + 0.99)


def test_implied_taker_fee_bps_calibration() -> None:
    """suggest_taker_fee_bps returns observed fee / observed notional × 10_000."""
    trade = _trade(
        pair_label="LINK/SOL",
        entry_ts=1_000_000,
        exit_ts=2_000_000,
        entry_oid_a=11,
        entry_oid_b=12,
        exit_oid_a=21,
        exit_oid_b=22,
        entry_size_a=100.0,
        entry_size_b=100.0,
        entry_price_a=10.0,
        entry_price_b=10.0,
        exit_price_a=10.0,
        exit_price_b=10.0,
        entry_fee_a=0.02,
        entry_fee_b=0.02,
        exit_fee_a=0.02,
        exit_fee_b=0.02,
    )
    fills = [
        _fill(11, "LINK", "Open Long", 100, 10, 0.45, 0.0, 1_000_000),
        _fill(12, "SOL", "Open Short", 100, 10, 0.45, 0.0, 1_000_000),
        _fill(21, "LINK", "Close Long", 100, 10, 0.45, 0.0, 2_000_000),
        _fill(22, "SOL", "Close Short", 100, 10, 0.45, 0.0, 2_000_000),
    ]
    report = build_report([trade], fills, [], 1_000_000, 2_000_000)
    # 4 fills × $1000 each = $4000 notional; 4 × $0.45 = $1.80 fee.
    # Implied = 1.80 / 4000 × 10_000 = 4.5 bps → matches HL retail taker.
    bps = suggest_taker_fee_bps(report)
    assert bps == pytest.approx(4.5)
