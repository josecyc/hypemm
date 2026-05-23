"""Tests for the runtime drift detection that fires after each process_bar.

The runtime reconcile loop is the safety net that replaces per-order
reduceOnly. After every hour boundary the runner queries HL's
clearinghouseState and compares it to the engine's expected per-coin net;
any divergence flips halt_trading and the runner stops emitting orders
until an operator investigates.
"""

from __future__ import annotations

from typing import Any

from hypemm.config import StrategyConfig
from hypemm.engine import StrategyEngine
from hypemm.models import Direction, OpenPosition, PairConfig
from hypemm.runner import _check_drift_and_halt


class _FakeLiveAdapter:
    """Minimal adapter that just hands back a canned clearinghouseState."""

    def __init__(self, user_state: dict[str, Any]) -> None:
        self._user_state = user_state

    def fetch_user_state(self) -> dict[str, Any]:
        return self._user_state


class _FakePaperAdapter:
    """No fetch_user_state — paper path. _check_drift_and_halt must be a no-op."""


def _engine_with_long_link_sol() -> StrategyEngine:
    pair = PairConfig("LINK", "SOL")
    eng = StrategyEngine(StrategyConfig(pairs=(pair,), notional_per_leg=25.0))
    eng.positions[pair.label] = OpenPosition(
        pair=pair,
        direction=Direction.LONG_RATIO,
        entry_z=-2.5,
        entry_price_a=10.0,
        entry_price_b=100.0,
        entry_time_ms=1_700_000_000_000,
        entry_correlation=0.85,
        filled_size_a=2.5,  # +2.5 LINK long
        filled_size_b=0.25,  # -0.25 SOL short
    )
    return eng


def test_no_drift_does_not_halt() -> None:
    eng = _engine_with_long_link_sol()
    adapter = _FakeLiveAdapter(
        {
            "assetPositions": [
                {"position": {"coin": "LINK", "szi": "2.5"}},
                {"position": {"coin": "SOL", "szi": "-0.25"}},
            ]
        }
    )
    _check_drift_and_halt(eng, adapter, notional_per_leg=25.0)  # type: ignore[arg-type]
    assert eng.halt_trading is False


def test_drift_on_shared_coin_halts_trading() -> None:
    """The actual incident shape: engine thinks LINK/SOL is short LINK 2.6
    but HL has zero LINK. Should halt."""
    eng = _engine_with_long_link_sol()
    # HL has no positions at all — the post-incident state on the live account
    adapter = _FakeLiveAdapter({"assetPositions": []})
    _check_drift_and_halt(eng, adapter, notional_per_leg=25.0)  # type: ignore[arg-type]
    assert eng.halt_trading is True


def test_paper_adapter_skipped() -> None:
    """Paper adapter has no fetch_user_state; the runtime check must no-op
    rather than raise. Otherwise the paper twin would crash on every bar."""
    eng = _engine_with_long_link_sol()
    adapter = _FakePaperAdapter()
    _check_drift_and_halt(eng, adapter, notional_per_leg=25.0)  # type: ignore[arg-type]
    assert eng.halt_trading is False


def test_transient_fetch_failure_does_not_halt() -> None:
    """A clearinghouseState fetch error is treated as transient — we don't
    want a single HL hiccup to permanently halt the runner. Startup reconcile
    on next restart catches any persistent drift."""

    class _FlakyAdapter:
        def fetch_user_state(self) -> dict[str, Any]:
            raise RuntimeError("transient HL error")

    eng = _engine_with_long_link_sol()
    _check_drift_and_halt(eng, _FlakyAdapter(), notional_per_leg=25.0)  # type: ignore[arg-type]
    assert eng.halt_trading is False
