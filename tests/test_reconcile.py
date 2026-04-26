"""Tests for startup reconciliation between engine state and exchange state."""

from __future__ import annotations

from hypemm.config import StrategyConfig
from hypemm.engine import StrategyEngine
from hypemm.models import Direction, OpenPosition, PairConfig
from hypemm.reconcile import reconcile

PAIR = PairConfig("LINK", "SOL")
NOTIONAL = 50_000.0


def _engine_with_long_ratio() -> StrategyEngine:
    eng = StrategyEngine(StrategyConfig(pairs=(PAIR,)))
    eng.positions[PAIR.label] = OpenPosition(
        pair=PAIR,
        direction=Direction.LONG_RATIO,
        entry_z=-2.5,
        entry_price_a=10.0,  # → 5000 LINK long
        entry_price_b=100.0,  # → 500 SOL short
        entry_time_ms=1_700_000_000_000,
        entry_correlation=0.85,
    )
    return eng


def test_reconcile_returns_empty_when_books_agree():
    eng = _engine_with_long_ratio()
    user_state = {
        "assetPositions": [
            {"position": {"coin": "LINK", "szi": "5000"}},
            {"position": {"coin": "SOL", "szi": "-500"}},
        ]
    }
    assert reconcile(eng, user_state, NOTIONAL) == []


def test_reconcile_flags_size_mismatch():
    eng = _engine_with_long_ratio()
    # Exchange has half the expected size
    user_state = {
        "assetPositions": [
            {"position": {"coin": "LINK", "szi": "2500"}},
            {"position": {"coin": "SOL", "szi": "-500"}},
        ]
    }
    divs = reconcile(eng, user_state, NOTIONAL)
    assert len(divs) == 1
    assert divs[0].coin == "LINK"
    assert divs[0].expected_size > divs[0].actual_size


def test_reconcile_flags_missing_exchange_position():
    eng = _engine_with_long_ratio()
    user_state = {"assetPositions": []}
    divs = reconcile(eng, user_state, NOTIONAL)
    assert {d.coin for d in divs} == {"LINK", "SOL"}


def test_reconcile_flags_phantom_exchange_position():
    eng = StrategyEngine(StrategyConfig(pairs=(PAIR,)))  # flat
    user_state = {"assetPositions": [{"position": {"coin": "LINK", "szi": "1000"}}]}
    divs = reconcile(eng, user_state, NOTIONAL)
    assert len(divs) == 1
    assert divs[0].coin == "LINK"
    assert divs[0].expected_size == 0
    assert divs[0].actual_size == 1000


def test_reconcile_within_tolerance_is_silent():
    eng = _engine_with_long_ratio()
    user_state = {
        "assetPositions": [
            {"position": {"coin": "LINK", "szi": "5050"}},  # +1%
            {"position": {"coin": "SOL", "szi": "-499"}},  # within rounding
        ]
    }
    assert reconcile(eng, user_state, NOTIONAL, size_tolerance_pct=0.05) == []


def test_reconcile_short_ratio_signs_correctly():
    eng = StrategyEngine(StrategyConfig(pairs=(PAIR,)))
    eng.positions[PAIR.label] = OpenPosition(
        pair=PAIR,
        direction=Direction.SHORT_RATIO,  # short A, long B
        entry_z=2.5,
        entry_price_a=10.0,
        entry_price_b=100.0,
        entry_time_ms=1_700_000_000_000,
        entry_correlation=0.85,
    )
    user_state = {
        "assetPositions": [
            {"position": {"coin": "LINK", "szi": "-5000"}},
            {"position": {"coin": "SOL", "szi": "500"}},
        ]
    }
    assert reconcile(eng, user_state, NOTIONAL) == []
