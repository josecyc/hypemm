"""Pure accounting primitives shared by backtest and live runner.

Single source of truth for fee and funding math. Both code paths feed identical
inputs (fill prices, sizes, rates, marks) to the same functions, which is what
gives the backtest≡live parity guarantee.
"""

from __future__ import annotations

from hypemm.models import Direction


def fee_for_fill(price: float, size: float, taker_fee_bps: float) -> float:
    """USD fee for a single fill at given price/size, given the taker rate.

    `size` is unsigned (long or short pays the same fee).
    """
    return abs(price * size) * taker_fee_bps / 10_000.0


def funding_for_leg_hour(signed_size: float, mark_price: float, hourly_rate: float) -> float:
    """One-hour funding charge for one leg. Positive = cost to us.

    Formula: signed_size * mark * rate. Long pays positive funding; short
    receives it. By linearity, summing this across all legs that hold a coin
    equals HL's per-coin charge in `userFunding.delta.usdc` exactly — even
    when cross-pair coin overlap cancels at the netted level.
    """
    return signed_size * mark_price * hourly_rate


def signed_leg_sizes(direction: Direction, size_a: float, size_b: float) -> tuple[float, float]:
    """Return signed leg sizes given the pair direction.

    LONG_RATIO  = long A, short B → (+size_a, -size_b)
    SHORT_RATIO = short A, long B → (-size_a, +size_b)
    """
    if direction == Direction.LONG_RATIO:
        return (+size_a, -size_b)
    return (-size_a, +size_b)
