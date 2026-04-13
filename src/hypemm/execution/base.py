"""Execution adapter protocol for paper and live trading."""

from __future__ import annotations

from typing import Protocol

from hypemm.models import Direction, PairConfig


class ExecutionAdapter(Protocol):
    """Interface for executing trades. Swap implementations for paper vs live."""

    def get_fill_prices(
        self,
        pair: PairConfig,
        direction: Direction,
        notional_per_leg: float,
    ) -> tuple[float, float]:
        """Get fill prices for a trade.

        For paper: returns current mid prices from the API.
        For live: places orders and returns actual fills.

        Returns (fill_price_a, fill_price_b).
        """
        ...
