"""Tests for the LiveExecutionAdapter.

We mock the HTTP client at the boundary so signing, action construction,
fill polling, slippage detection, and reconciliation can be verified without
hitting the real exchange.
"""

from __future__ import annotations

from typing import Any

import pytest

from hypemm.execution import ExecutionError, LiveExecutionAdapter
from hypemm.models import Direction, PairConfig

VALID_KEY = "0x" + "11" * 32
VALID_ADDR = "0x" + "ab" * 20


class _Resp:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> Any:
        return self.payload


class _MockClient:
    """In-memory HL stub. Hands back canned responses by request type."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.responses: dict[str, list[Any]] = {}

    def queue(self, kind: str, payload: Any) -> None:
        self.responses.setdefault(kind, []).append(payload)

    def _next(self, kind: str) -> Any:
        if kind not in self.responses or not self.responses[kind]:
            raise AssertionError(f"no response queued for {kind}")
        return self.responses[kind].pop(0)

    def post(self, url: str, json: dict, timeout: float = 10.0):
        self.calls.append({"url": url, "json": json})
        if "/info" in url:
            t = json.get("type")
            if t == "meta":
                return _Resp(self._next("meta"))
            if t == "l2Book":
                return _Resp(self._next(f"l2Book:{json['coin']}"))
            if t == "userFills":
                return _Resp(self._next("userFills"))
            if t == "clearinghouseState":
                return _Resp(self._next("clearinghouseState"))
        if "/exchange" in url:
            action_type = json["action"]["type"]
            return _Resp(self._next(f"exchange:{action_type}"))
        raise AssertionError(f"unexpected request: {url} {json!r}")

    def close(self) -> None:
        pass


def _make_adapter(client: _MockClient, **kw: Any) -> LiveExecutionAdapter:
    """Build a LiveExecutionAdapter wired to a mock client."""
    a = LiveExecutionAdapter(
        rest_url="https://api.hyperliquid-testnet.xyz",
        private_key=VALID_KEY,
        account_address=VALID_ADDR,
        fill_poll_seconds=0.0,
        fill_timeout_seconds=1.0,
        **kw,
    )
    a.client = client  # type: ignore[assignment]
    return a


def _meta_payload() -> dict:
    return {
        "universe": [
            {"name": "LINK", "szDecimals": 1},
            {"name": "SOL", "szDecimals": 2},
            {"name": "DOGE", "szDecimals": 0},
            {"name": "AVAX", "szDecimals": 2},
            {"name": "ADA", "szDecimals": 0},
        ]
    }


# -- credential gating -----------------------------------------------------


def test_init_requires_private_key(monkeypatch):
    monkeypatch.delenv("HYPERLIQUID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("HYPERLIQUID_ACCOUNT", raising=False)
    with pytest.raises(Exception, match="HYPERLIQUID_PRIVATE_KEY"):
        LiveExecutionAdapter()


def test_init_detects_mainnet_vs_testnet():
    a = LiveExecutionAdapter(
        "https://api.hyperliquid.xyz", private_key=VALID_KEY, account_address=VALID_ADDR
    )
    assert a.is_mainnet is True
    a2 = LiveExecutionAdapter(
        "https://api.hyperliquid-testnet.xyz",
        private_key=VALID_KEY,
        account_address=VALID_ADDR,
    )
    assert a2.is_mainnet is False


# -- get_fill_prices happy path -------------------------------------------


def _ok_status(oid: int, kind: str = "resting") -> dict:
    return {"status": "ok", "response": {"data": {"statuses": [{kind: {"oid": oid}}]}}}


def test_get_fill_prices_long_ratio_places_correct_legs():
    client = _MockClient()
    client.queue("meta", _meta_payload())
    # Set leverage twice (LINK + SOL)
    client.queue("exchange:updateLeverage", {"status": "ok"})
    client.queue("exchange:updateLeverage", {"status": "ok"})
    # Mids
    client.queue("l2Book:LINK", {"levels": [[{"px": "10.0"}], [{"px": "10.02"}]]})
    client.queue("l2Book:SOL", {"levels": [[{"px": "100.0"}], [{"px": "100.04"}]]})
    client.queue("exchange:order", _ok_status(111))
    client.queue("exchange:order", _ok_status(222))
    client.queue("userFills", [{"oid": 111, "px": "10.005", "sz": "5000.0"}])
    client.queue("userFills", [{"oid": 222, "px": "100.01", "sz": "500.0"}])

    adapter = _make_adapter(client)
    fa, fb = adapter.get_fill_prices(PairConfig("LINK", "SOL"), Direction.LONG_RATIO, 50_000.0)
    assert fa == pytest.approx(10.005)
    assert fb == pytest.approx(100.01)

    # LINK leg should be a buy, SOL leg a sell (LONG_RATIO = long A, short B)
    order_calls = [
        c
        for c in client.calls
        if "/exchange" in c["url"] and c["json"]["action"]["type"] == "order"
    ]
    assert order_calls[0]["json"]["action"]["orders"][0]["b"] is True
    assert order_calls[1]["json"]["action"]["orders"][0]["b"] is False


def test_get_fill_prices_aborts_on_excess_slippage():
    client = _MockClient()
    client.queue("meta", _meta_payload())
    client.queue("exchange:updateLeverage", {"status": "ok"})
    client.queue("exchange:updateLeverage", {"status": "ok"})
    client.queue("l2Book:LINK", {"levels": [[{"px": "10.0"}], [{"px": "10.02"}]]})
    client.queue("l2Book:SOL", {"levels": [[{"px": "100.0"}], [{"px": "100.04"}]]})
    client.queue("exchange:order", _ok_status(111, "filled"))
    client.queue("exchange:order", _ok_status(222, "filled"))
    # First leg fills 50 bps off mid (10.0 → 10.05) — exceeds default 5 bps cap
    client.queue("userFills", [{"oid": 111, "px": "10.05", "sz": "5000.0"}])
    client.queue("userFills", [{"oid": 222, "px": "100.01", "sz": "500.0"}])

    adapter = _make_adapter(client)
    with pytest.raises(ExecutionError, match="slippage"):
        adapter.get_fill_prices(PairConfig("LINK", "SOL"), Direction.LONG_RATIO, 50_000.0)


def test_get_fill_prices_raises_on_unknown_coin():
    client = _MockClient()
    client.queue("meta", _meta_payload())
    adapter = _make_adapter(client)
    with pytest.raises(ExecutionError, match="not in HL universe"):
        adapter.get_fill_prices(PairConfig("XXX", "SOL"), Direction.LONG_RATIO, 50_000.0)


def test_get_fill_prices_propagates_order_error():
    client = _MockClient()
    client.queue("meta", _meta_payload())
    client.queue("exchange:updateLeverage", {"status": "ok"})
    client.queue("exchange:updateLeverage", {"status": "ok"})
    client.queue("l2Book:LINK", {"levels": [[{"px": "10.0"}], [{"px": "10.02"}]]})
    client.queue("l2Book:SOL", {"levels": [[{"px": "100.0"}], [{"px": "100.04"}]]})
    client.queue(
        "exchange:order",
        {"status": "ok", "response": {"data": {"statuses": [{"error": "Insufficient margin"}]}}},
    )
    adapter = _make_adapter(client)
    with pytest.raises(ExecutionError, match="Insufficient margin"):
        adapter.get_fill_prices(PairConfig("LINK", "SOL"), Direction.LONG_RATIO, 50_000.0)


# -- fetch_user_state ------------------------------------------------------


def test_fetch_user_state_returns_clearinghouse_payload():
    client = _MockClient()
    expected = {"assetPositions": [{"position": {"coin": "LINK", "szi": "5000"}}]}
    client.queue("clearinghouseState", expected)
    adapter = _make_adapter(client)
    assert adapter.fetch_user_state() == expected
