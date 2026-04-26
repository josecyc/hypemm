"""Execution adapters: protocol and paper trading implementation."""

from __future__ import annotations

import logging
import os
from typing import Protocol

import httpx

from hypemm.models import ConfigurationError, DataFetchError, Direction, HypeMMError, PairConfig

logger = logging.getLogger(__name__)


class ExecutionAdapter(Protocol):
    """Interface for executing trades. Swap implementations for paper vs live."""

    client: httpx.Client
    rest_url: str

    def fetch_mid(self, coin: str) -> float:
        """Return the current mid price for a coin."""
        ...

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

    def close(self) -> None:
        """Release resources."""
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


class LiveExecutionAdapter:
    """Live trading adapter for Hyperliquid. Places signed L1 actions.

    This is a scaffold: it loads credentials and exposes the same interface as
    PaperExecutionAdapter, but `get_fill_prices` raises NotImplementedError until
    the EIP-712 signing path is wired up. Run paper trading until that ships.

    Required environment variables:
        HYPERLIQUID_PRIVATE_KEY    — wallet private key (hex, 0x-prefixed)
        HYPERLIQUID_ACCOUNT        — main account address (0x-prefixed)
        HYPERLIQUID_API_URL        — default https://api.hyperliquid.xyz
                                     (overridable for testnet)
    """

    INFO_PATH = "/info"
    EXCHANGE_PATH = "/exchange"

    def __init__(
        self,
        rest_url: str = "https://api.hyperliquid.xyz",
        *,
        private_key: str | None = None,
        account_address: str | None = None,
    ) -> None:
        self.rest_url = rest_url.rstrip("/")
        self._private_key = private_key or os.environ.get("HYPERLIQUID_PRIVATE_KEY")
        self._account = account_address or os.environ.get("HYPERLIQUID_ACCOUNT")
        if not self._private_key:
            raise ConfigurationError(
                "HYPERLIQUID_PRIVATE_KEY is required for live trading. "
                "Set it in the environment or pass private_key explicitly."
            )
        if not self._account:
            raise ConfigurationError(
                "HYPERLIQUID_ACCOUNT is required for live trading. "
                "Set it in the environment or pass account_address explicitly."
            )
        self.client = httpx.Client(timeout=10)
        logger.warning(
            "LiveExecutionAdapter initialized for account %s — orders will hit real markets",
            self._account,
        )

    def fetch_mid(self, coin: str) -> float:
        """Fetch the current mid price for a coin (same as paper)."""
        try:
            r = self.client.post(
                self.rest_url + self.INFO_PATH, json={"type": "l2Book", "coin": coin}
            )
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

    def get_fill_prices(
        self,
        pair: PairConfig,
        direction: Direction,
        notional_per_leg: float,
    ) -> tuple[float, float]:
        """Place market orders for both legs and return realized fill prices.

        Not yet implemented. Wiring requires:
          1. Build the L1 order action (`{"type": "order", "orders": [...]}`)
          2. EIP-712 sign with the private key (eth_account)
          3. POST to /exchange and parse the response
          4. Poll fills via /info userFills until both legs settle
          5. Return the volume-weighted average fill prices

        Until shipped, this raises so an accidental live run can't quietly fall
        through to the wrong code path.
        """
        raise NotImplementedError(
            "LiveExecutionAdapter.get_fill_prices is not implemented yet. "
            "Order signing + placement code pending. "
            "Run paper trading (without --live) until this lands."
        )

    def close(self) -> None:
        self.client.close()


def build_adapter(rest_url: str, *, live: bool) -> ExecutionAdapter:
    """Construct an execution adapter from CLI arguments.

    Centralized so the kill-switch confirmation lives in one place.
    """
    if live:
        rest_root = rest_url.rsplit("/info", 1)[0] if rest_url.endswith("/info") else rest_url
        try:
            return LiveExecutionAdapter(rest_root)
        except ConfigurationError:
            raise
        except HypeMMError:
            raise
    return PaperExecutionAdapter(rest_url)
