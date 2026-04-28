"""Hyperliquid asset metadata: asset_id, szDecimals, price/size rounding.

The /info `meta` endpoint returns an ordered universe of perp assets. The
*index* in the universe array IS the asset_id used in order actions. Each
asset has an `szDecimals` field that controls valid order size precision.

Price tick rules (from HL docs): perps use up to 5 significant figures, with
decimal places ≤ MAX_DECIMALS - szDecimals where MAX_DECIMALS = 6.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from hypemm.models import DataFetchError

PERP_MAX_DECIMALS = 6
PERP_MAX_SIGFIGS = 5


@dataclass(frozen=True)
class AssetMeta:
    """One asset's exchange metadata."""

    coin: str
    asset_id: int
    sz_decimals: int

    @property
    def px_decimals(self) -> int:
        return PERP_MAX_DECIMALS - self.sz_decimals


def fetch_asset_meta(client: httpx.Client, info_url: str) -> dict[str, AssetMeta]:
    """Fetch meta from /info and return {coin: AssetMeta}.

    info_url should point to the /info endpoint
    (e.g. https://api.hyperliquid.xyz/info).
    """
    try:
        r = client.post(info_url, json={"type": "meta"}, timeout=10.0)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise DataFetchError(f"Failed to fetch HL asset meta: {e}")

    universe = data.get("universe")
    if not isinstance(universe, list):
        raise DataFetchError(f"Malformed meta response: missing 'universe' (got {data!r})")

    out: dict[str, AssetMeta] = {}
    for idx, entry in enumerate(universe):
        coin = entry.get("name")
        sz = entry.get("szDecimals")
        if coin is None or sz is None:
            raise DataFetchError(f"Malformed universe entry at index {idx}: {entry!r}")
        out[str(coin)] = AssetMeta(coin=str(coin), asset_id=idx, sz_decimals=int(sz))
    return out


def round_size(size: float, sz_decimals: int) -> float:
    """Round a size to the asset's allowed precision."""
    if sz_decimals < 0:
        raise ValueError(f"sz_decimals must be >= 0, got {sz_decimals}")
    return round(size, sz_decimals)


def round_price(price: float, sz_decimals: int) -> float:
    """Round a price to comply with HL's tick rules.

    Two constraints, both must hold:
      1. At most PERP_MAX_DECIMALS - sz_decimals decimal places
      2. At most PERP_MAX_SIGFIGS significant figures
    """
    from math import floor, log10

    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")

    px_decimals = PERP_MAX_DECIMALS - sz_decimals
    magnitude = floor(log10(price))
    max_decimals_for_sigfigs = max(0, PERP_MAX_SIGFIGS - 1 - magnitude)
    decimals = min(px_decimals, max_decimals_for_sigfigs)
    return round(price, decimals)


def format_size(size: float, sz_decimals: int) -> str:
    """Format size as a string suitable for the order action."""
    s = round_size(size, sz_decimals)
    return f"{s:.{sz_decimals}f}".rstrip("0").rstrip(".") if sz_decimals > 0 else f"{int(s)}"


def format_price(price: float, sz_decimals: int) -> str:
    """Format price as a string suitable for the order action."""
    p = round_price(price, sz_decimals)
    px_decimals = PERP_MAX_DECIMALS - sz_decimals
    s = f"{p:.{px_decimals}f}".rstrip("0").rstrip(".") if px_decimals > 0 else f"{int(p)}"
    return s if s else "0"
