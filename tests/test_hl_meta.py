"""Tests for HL asset metadata loader and price/size rounding."""

from __future__ import annotations

import pytest

from hypemm.hl_meta import (
    AssetMeta,
    fetch_asset_meta,
    format_price,
    format_size,
    round_price,
    round_size,
)
from hypemm.models import DataFetchError

# -- rounding --------------------------------------------------------------


def test_round_size_to_decimals():
    assert round_size(123.456789, 2) == 123.46
    assert round_size(0.0001, 4) == 0.0001
    assert round_size(0.00009, 4) == 0.0001


def test_round_size_negative_decimals_rejected():
    with pytest.raises(ValueError):
        round_size(1.0, -1)


def test_round_price_respects_px_decimals():
    # sz_decimals=2 → px_decimals = 6 - 2 = 4
    assert round_price(1.234567, 2) == 1.2346


def test_round_price_respects_sigfigs():
    # 5 sig figs cap. 12345.678 → 12346 (already 5 sig figs after px_decimals)
    assert round_price(12345.678, 0) == 12346.0
    # 123.45678 with sz_decimals=0 → px_decimals=6, but limited to 5 sig figs → 123.46
    assert round_price(123.45678, 0) == 123.46
    # 0.123456 with sz_decimals=2 → px_decimals=4, sigfigs cap → 0.1235
    assert round_price(0.123456, 2) == 0.1235


def test_round_price_subdollar_sigfigs_enforced():
    """Regression: pre-fix code skipped the 5-sigfig check for sub-1 prices.

    DOGE szDecimals=0; once mid crossed $0.10 the IoC limit price computed
    as e.g. 0.100065, which round(_, 6) leaves at 0.100065 — 6 sig figs.
    HL rejects with "Price must be divisible by tick size".
    """
    # 0.100065 → must collapse to 5 sig figs at 5 decimals: 0.10006 or 0.10007
    out = round_price(0.100065, 0)
    assert len(f"{out:.6f}".rstrip("0").rstrip(".").lstrip("0").lstrip(".")) <= 5
    assert out in (0.10006, 0.10007)
    # 0.247247 (ADA) → 5 sig figs: 0.24725
    assert round_price(0.247247, 0) == 0.24725
    # 0.099064 (DOGE pre-cross) → already 5 sig figs at 6 decimals, unchanged
    assert round_price(0.099064, 0) == 0.099064


def test_round_price_zero_rejected():
    with pytest.raises(ValueError):
        round_price(0, 2)
    with pytest.raises(ValueError):
        round_price(-1.0, 2)


# -- formatting ------------------------------------------------------------


def test_format_size_strips_trailing_zeros():
    assert format_size(1.50, 2) == "1.5"
    assert format_size(1.0, 2) == "1"
    assert format_size(1.0, 0) == "1"


def test_format_price_strips_trailing_zeros():
    assert format_price(100.0, 2) == "100"
    assert format_price(123.45, 2) == "123.45"


# -- fetch_asset_meta ------------------------------------------------------


class _FakeResp:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> object:
        return self.payload


class _FakeClient:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.requests: list[dict] = []

    def post(self, url: str, json: dict, timeout: float = 10.0):
        self.requests.append({"url": url, "json": json})
        return _FakeResp(self.payload)


def test_fetch_asset_meta_assigns_indices_as_asset_ids():
    payload = {
        "universe": [
            {"name": "BTC", "szDecimals": 4},
            {"name": "ETH", "szDecimals": 3},
            {"name": "SOL", "szDecimals": 1},
        ]
    }
    client = _FakeClient(payload)
    meta = fetch_asset_meta(client, "https://api.hyperliquid.xyz/info")  # type: ignore[arg-type]

    assert meta["BTC"] == AssetMeta(coin="BTC", asset_id=0, sz_decimals=4)
    assert meta["ETH"] == AssetMeta(coin="ETH", asset_id=1, sz_decimals=3)
    assert meta["SOL"] == AssetMeta(coin="SOL", asset_id=2, sz_decimals=1)


def test_fetch_asset_meta_raises_on_malformed_payload():
    client = _FakeClient({"not_universe": []})
    with pytest.raises(DataFetchError, match="missing 'universe'"):
        fetch_asset_meta(client, "https://api.hyperliquid.xyz/info")  # type: ignore[arg-type]


def test_asset_meta_px_decimals_property():
    meta = AssetMeta(coin="X", asset_id=0, sz_decimals=2)
    assert meta.px_decimals == 4
