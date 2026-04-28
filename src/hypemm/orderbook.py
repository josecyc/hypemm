"""Hyperliquid L2 orderbook helpers.

`book_vwap` walks the L2 book to compute the realized VWAP for a market order
of a given notional size. Used by:
  - PaperExecutionAdapter to simulate realistic fills (instead of mid)
  - hypemm snapshot-slippage CLI to estimate per-pair slippage for backtest
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from hypemm.models import DataFetchError, HypeMMError


class InsufficientDepthError(HypeMMError):
    """L2 book lacks enough size to fill the requested notional."""


@dataclass(frozen=True)
class BookFill:
    """Result of walking the book for a market order."""

    vwap: float
    mid: float
    filled_notional: float
    slippage_bps: float
    levels_consumed: int


def fetch_l2_book(client: httpx.Client, info_url: str, coin: str) -> dict[str, object]:
    """Fetch the raw L2 book for a coin."""
    try:
        r = client.post(info_url, json={"type": "l2Book", "coin": coin}, timeout=10.0)
        r.raise_for_status()
        return dict(r.json())
    except (httpx.HTTPError, httpx.TimeoutException, ValueError) as e:
        raise DataFetchError(f"Failed to fetch L2 book for {coin}: {e}")


def walk_book(book: dict[str, object], is_buy: bool, notional: float) -> BookFill:
    """Walk the L2 book on the appropriate side and return the realized VWAP.

    is_buy=True consumes the ask side (we cross up); False consumes the bid
    side (we cross down). Slippage is signed in bps relative to mid: positive
    means we paid worse than mid (always, by construction, for a market order).
    """
    levels = book.get("levels")
    if not isinstance(levels, list) or len(levels) < 2:
        raise DataFetchError(f"Malformed L2 book payload: {book!r}")

    bids, asks = levels[0], levels[1]
    if not bids or not asks:
        raise InsufficientDepthError("Empty bid or ask side")

    mid = (float(bids[0]["px"]) + float(asks[0]["px"])) / 2
    side = asks if is_buy else bids

    remaining = notional
    total_size = 0.0
    total_cost = 0.0
    levels_used = 0
    for lvl in side:
        px = float(lvl["px"])
        sz = float(lvl["sz"])
        level_notional = px * sz
        levels_used += 1
        if level_notional >= remaining:
            filled_size = remaining / px
            total_size += filled_size
            total_cost += filled_size * px
            remaining = 0.0
            break
        total_size += sz
        total_cost += sz * px
        remaining -= level_notional

    if remaining > 0 or total_size == 0:
        raise InsufficientDepthError(
            f"Book exhausted with ${remaining:.0f} of ${notional:.0f} unfilled "
            f"({levels_used} levels consumed)"
        )

    vwap = total_cost / total_size
    slip_bps = (vwap - mid) / mid * 10_000 * (1 if is_buy else -1)
    return BookFill(
        vwap=vwap,
        mid=mid,
        filled_notional=total_cost,
        slippage_bps=slip_bps,
        levels_consumed=levels_used,
    )


def book_vwap(
    client: httpx.Client, info_url: str, coin: str, is_buy: bool, notional: float
) -> BookFill:
    """Fetch L2 and walk it; convenience wrapper."""
    return walk_book(fetch_l2_book(client, info_url, coin), is_buy, notional)
