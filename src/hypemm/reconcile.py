"""Startup reconciliation: check that on-exchange positions match state.json.

If the runner crashed mid-trade, or someone manually closed positions, the
local engine state and the exchange's state can diverge. Detecting this at
startup and refusing to proceed (unless explicitly forced) prevents the engine
from operating on a phantom position book.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from hypemm.engine import StrategyEngine
from hypemm.models import Direction

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Divergence:
    """One mismatch between engine state and exchange state."""

    coin: str
    expected_direction: str  # "long", "short", or "flat"
    expected_size: float
    actual_size: float  # signed; positive = long, negative = short, 0 = flat


def _expected_size_per_coin(
    engine: StrategyEngine, notional_per_leg: float
) -> dict[str, float]:
    """Sum signed expected position size per coin across all open pair positions.

    For LONG_RATIO: long coin_a, short coin_b.
    For SHORT_RATIO: short coin_a, long coin_b.
    Size is in coins, computed at entry price. (We compare in coins, not USD,
    because the exchange reports szi which is a coin-denominated size.)
    """
    out: dict[str, float] = {}
    for label, pos in engine.positions.items():
        if pos is None:
            continue
        sign_a = 1 if pos.direction == Direction.LONG_RATIO else -1
        sign_b = -sign_a
        out[pos.pair.coin_a] = out.get(pos.pair.coin_a, 0.0) + sign_a * (
            notional_per_leg / pos.entry_price_a
        )
        out[pos.pair.coin_b] = out.get(pos.pair.coin_b, 0.0) + sign_b * (
            notional_per_leg / pos.entry_price_b
        )
    return out


def _exchange_size_per_coin(user_state: dict[str, Any]) -> dict[str, float]:
    """Extract signed position size per coin from clearinghouseState."""
    out: dict[str, float] = {}
    for ap in user_state.get("assetPositions", []):
        position = ap.get("position", {})
        coin = position.get("coin")
        szi = position.get("szi")  # signed; HL convention
        if coin is None or szi is None:
            continue
        try:
            size = float(szi)
        except (TypeError, ValueError):
            continue
        if size != 0:
            out[str(coin)] = size
    return out


def reconcile(
    engine: StrategyEngine,
    user_state: dict[str, Any],
    notional_per_leg: float,
    *,
    size_tolerance_pct: float = 0.05,
) -> list[Divergence]:
    """Compare engine positions vs exchange positions per coin.

    Returns a list of divergences. Empty list means the books agree.
    Tolerance is per-coin: |expected - actual| / max(|expected|, 1) <= size_tolerance_pct.
    """
    expected = _expected_size_per_coin(engine, notional_per_leg)
    actual = _exchange_size_per_coin(user_state)

    divergences: list[Divergence] = []
    for coin in set(expected) | set(actual):
        exp = expected.get(coin, 0.0)
        act = actual.get(coin, 0.0)
        baseline = max(abs(exp), 1.0)  # avoid div-by-zero on flat expectation
        if abs(exp - act) / baseline > size_tolerance_pct:
            direction = "long" if exp > 0 else ("short" if exp < 0 else "flat")
            divergences.append(
                Divergence(
                    coin=coin,
                    expected_direction=direction,
                    expected_size=exp,
                    actual_size=act,
                )
            )
    return divergences
