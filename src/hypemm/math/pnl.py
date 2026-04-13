"""P&L calculations for stat arb trades."""

from __future__ import annotations

from hypemm.models import Direction, OpenPosition


def compute_leg_pnl(
    direction: Direction,
    notional: float,
    entry_price_a: float,
    entry_price_b: float,
    exit_price_a: float,
    exit_price_b: float,
) -> tuple[float, float]:
    """Compute P&L for each leg of a stat arb trade.

    Returns (pnl_leg_a, pnl_leg_b).

    LONG_RATIO = long A, short B.
    SHORT_RATIO = short A, long B.
    """
    if direction == Direction.LONG_RATIO:
        pnl_a = notional * (exit_price_a - entry_price_a) / entry_price_a
        pnl_b = notional * (entry_price_b - exit_price_b) / entry_price_b
    else:
        pnl_a = notional * (entry_price_a - exit_price_a) / entry_price_a
        pnl_b = notional * (exit_price_b - entry_price_b) / entry_price_b
    return pnl_a, pnl_b


def compute_trade_pnl(
    direction: Direction,
    notional: float,
    cost_per_side_bps: float,
    entry_price_a: float,
    entry_price_b: float,
    exit_price_a: float,
    exit_price_b: float,
) -> tuple[float, float, float, float]:
    """Compute full trade P&L.

    Returns (pnl_leg_a, pnl_leg_b, gross_pnl, net_pnl).
    """
    pnl_a, pnl_b = compute_leg_pnl(
        direction, notional, entry_price_a, entry_price_b, exit_price_a, exit_price_b
    )
    gross = pnl_a + pnl_b
    round_trip_cost = notional * 2 * cost_per_side_bps / 10_000 * 2
    net = gross - round_trip_cost
    return pnl_a, pnl_b, gross, net


def compute_unrealized_pnl(
    position: OpenPosition,
    current_price_a: float,
    current_price_b: float,
    notional: float,
) -> float:
    """Unrealized P&L for an open position (before costs)."""
    pnl_a, pnl_b = compute_leg_pnl(
        position.direction,
        notional,
        position.entry_price_a,
        position.entry_price_b,
        current_price_a,
        current_price_b,
    )
    return pnl_a + pnl_b
