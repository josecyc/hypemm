"""Execution adapters: protocol and paper trading implementation."""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from hypemm.models import DataFetchError, Direction, PairConfig

logger = logging.getLogger(__name__)


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


class PaperExecutionAdapter:
    """Execute paper trades by fetching current mid prices from Hyperliquid."""

    def __init__(self, rest_url: str) -> None:
        self.rest_url = rest_url
        self.client = httpx.Client(timeout=10)

    def get_fill_prices(
        self,
        pair: PairConfig,
        direction: Direction,
        notional_per_leg: float,
    ) -> tuple[float, float]:
        """Fetch current mid prices as paper fill prices."""
        price_a = self.fetch_mid(pair.coin_a)
        price_b = self.fetch_mid(pair.coin_b)
        return price_a, price_b

    def fetch_mid(self, coin: str) -> float:
        """Fetch the current mid price for a coin."""
        try:
            r = self.client.post(self.rest_url, json={"type": "l2Book", "coin": coin})
            r.raise_for_status()
            data = r.json()
            levels = data.get("levels", [])
            if len(levels) >= 2 and levels[0] and levels[1]:
                bid = float(levels[0][0]["px"])
                ask = float(levels[1][0]["px"])
                return (bid + ask) / 2
        except (httpx.HTTPError, httpx.TimeoutException, KeyError, IndexError, ValueError) as e:
            raise DataFetchError(f"Failed to fetch mid price for {coin}: {e}")

        raise DataFetchError(f"Empty orderbook for {coin}")

    def close(self) -> None:
        """Close the HTTP client."""
        self.client.close()
