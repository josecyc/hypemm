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
    monkeypatch.delenv("HYPERLIQUID_KEYSTORE", raising=False)
    monkeypatch.delenv("HYPERLIQUID_ACCOUNT", raising=False)
    with pytest.raises(Exception, match="No signing key found"):
        LiveExecutionAdapter()


def test_init_account_defaults_to_signer_address_when_unset(monkeypatch):
    monkeypatch.delenv("HYPERLIQUID_ACCOUNT", raising=False)
    a = LiveExecutionAdapter(
        rest_url="https://api.hyperliquid-testnet.xyz",
        private_key=VALID_KEY,
    )
    assert a._account_address == a._signer.address


def test_init_loads_key_from_foundry_keystore(tmp_path, monkeypatch):
    """Loading from a Foundry-style keystore matches the rpo-{nb} convention."""
    from eth_account import Account

    # Create an encrypted keystore at a tmp path with a known password.
    pwd = "testpassword"
    encrypted = Account.encrypt(VALID_KEY, pwd)
    keystore_path = tmp_path / "rpo-test"
    keystore_path.write_text(__import__("json").dumps(encrypted))

    monkeypatch.delenv("HYPERLIQUID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("HYPERLIQUID_ACCOUNT", raising=False)
    monkeypatch.setenv("HYPERLIQUID_KEYSTORE", str(keystore_path))
    monkeypatch.setenv("HYPERLIQUID_KEYSTORE_PWD", pwd)

    a = LiveExecutionAdapter(rest_url="https://api.hyperliquid-testnet.xyz")
    expected = Account.from_key(VALID_KEY).address
    assert a._signer.address == expected
    assert a._account_address == expected


def test_init_keystore_falls_back_to_rpo_keystore_pwd(tmp_path, monkeypatch):
    """RPO_KEYSTORE_PWD is honored when HYPERLIQUID_KEYSTORE_PWD is unset."""
    from eth_account import Account

    pwd = "rpopwd"
    encrypted = Account.encrypt(VALID_KEY, pwd)
    keystore_path = tmp_path / "rpo-test"
    keystore_path.write_text(__import__("json").dumps(encrypted))

    monkeypatch.delenv("HYPERLIQUID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("HYPERLIQUID_KEYSTORE_PWD", raising=False)
    monkeypatch.setenv("HYPERLIQUID_KEYSTORE", str(keystore_path))
    monkeypatch.setenv("RPO_KEYSTORE_PWD", pwd)

    a = LiveExecutionAdapter(rest_url="https://api.hyperliquid-testnet.xyz")
    assert a._signer.address == Account.from_key(VALID_KEY).address


def test_init_keystore_missing_password_raises(tmp_path, monkeypatch):
    keystore_path = tmp_path / "rpo-test"
    keystore_path.write_text("{}")  # not actually decrypted, error short-circuits
    monkeypatch.delenv("HYPERLIQUID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("HYPERLIQUID_KEYSTORE_PWD", raising=False)
    monkeypatch.delenv("RPO_KEYSTORE_PWD", raising=False)
    monkeypatch.setenv("HYPERLIQUID_KEYSTORE", str(keystore_path))
    with pytest.raises(Exception, match="no password found"):
        LiveExecutionAdapter(rest_url="https://api.hyperliquid-testnet.xyz")


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
    fill = adapter.get_fill_prices(PairConfig("LINK", "SOL"), Direction.LONG_RATIO, 50_000.0)
    assert fill.price_a == pytest.approx(10.005)
    assert fill.price_b == pytest.approx(100.01)
    # Returned sizes are the rounded values actually sent to HL (the runner
    # persists these on the position so the close uses the same numbers).
    # Mid_LINK = (10.0+10.02)/2 = 10.01; size = 50000/10.01 = 4995.005 → 4995.0 (sz=1)
    # Mid_SOL  = (100.0+100.04)/2 = 100.02; size = 50000/100.02 = 499.90 → 499.90 (sz=2)
    assert fill.size_a == pytest.approx(4995.0)
    assert fill.size_b == pytest.approx(499.90)
    # Modeled fees use fee_for_fill(price, size, taker_fee_bps).
    # 4995 × 10.005 × 4.5/10000 = ~22.49; 499.90 × 100.01 × 4.5/10000 = ~22.50
    assert fill.fee_a == pytest.approx(4995.0 * 10.005 * 4.5 / 10_000.0)
    assert fill.fee_b == pytest.approx(499.90 * 100.01 * 4.5 / 10_000.0)
    # Default mock fills don't carry a "fee" field → actual fees are zero.
    assert fill.fee_a_actual == pytest.approx(0.0)
    assert fill.fee_b_actual == pytest.approx(0.0)
    assert fill.oid_a == 111
    assert fill.oid_b == 222

    # LINK leg should be a buy, SOL leg a sell (LONG_RATIO = long A, short B)
    order_calls = [
        c
        for c in client.calls
        if "/exchange" in c["url"] and c["json"]["action"]["type"] == "order"
    ]
    assert order_calls[0]["json"]["action"]["orders"][0]["b"] is True
    assert order_calls[1]["json"]["action"]["orders"][0]["b"] is False


def test_get_fill_prices_close_inverts_legs_without_reduce_only():
    """Closes flip both leg directions but do NOT set reduceOnly.

    Why no reduceOnly: in a multi-pair portfolio sharing coins, HL nets
    per-coin across all pairs. If pair B is open in the opposite direction
    on a coin pair A shares, HL's actual net on that coin is the algebraic
    sum, not pair A's leg size. A reduceOnly close of pair A would reject
    ("would increase position") whenever the net direction differs from
    pair A's leg direction. Sending without reduceOnly lets the close
    move HL's net by exactly the leg amount; the runner's reconcile loop
    is the safety net for any unexpected divergence.
    """
    client = _MockClient()
    client.queue("meta", _meta_payload())
    client.queue("exchange:updateLeverage", {"status": "ok"})
    client.queue("exchange:updateLeverage", {"status": "ok"})
    client.queue("l2Book:LINK", {"levels": [[{"px": "10.0"}], [{"px": "10.02"}]]})
    client.queue("l2Book:SOL", {"levels": [[{"px": "100.0"}], [{"px": "100.04"}]]})
    client.queue("exchange:order", _ok_status(111))
    client.queue("exchange:order", _ok_status(222))
    client.queue("userFills", [{"oid": 111, "px": "10.01", "sz": "5000.0"}])
    client.queue("userFills", [{"oid": 222, "px": "100.02", "sz": "500.0"}])

    adapter = _make_adapter(client)
    adapter.get_fill_prices(
        PairConfig("LINK", "SOL"),
        Direction.SHORT_RATIO,
        50_000.0,
        is_close=True,
        close_sizes=(5000.0, 500.0),
    )

    order_calls = [
        c
        for c in client.calls
        if "/exchange" in c["url"] and c["json"]["action"]["type"] == "order"
    ]
    # SHORT_RATIO entry would be: sell A, buy B. Closing it inverts: buy A, sell B.
    leg_a = order_calls[0]["json"]["action"]["orders"][0]
    leg_b = order_calls[1]["json"]["action"]["orders"][0]
    assert leg_a["b"] is True  # buy LINK to close prior short
    assert leg_b["b"] is False  # sell SOL to close prior long
    assert leg_a["r"] is False  # NOT reduceOnly — see docstring
    assert leg_b["r"] is False


def test_close_uses_explicit_close_sizes_verbatim():
    """Regression: closes must size from close_sizes, not from notional/mid.

    On a $25/leg AVAX leg with szDecimals=2, entry mid 9.10 rounds to 2.75
    AVAX, exit mid 9.20 rounds to 2.72. ReduceOnly clamps to 2.72, leaving
    0.03 AVAX (~$0.28) residual. Pinning that close_sizes wins guarantees
    the exact entry size is sent regardless of mid drift.
    """
    client = _MockClient()
    client.queue("meta", _meta_payload())
    client.queue("exchange:updateLeverage", {"status": "ok"})
    client.queue("exchange:updateLeverage", {"status": "ok"})
    # Mids drifted between entry and exit — close must NOT recompute from these.
    client.queue("l2Book:LINK", {"levels": [[{"px": "10.5"}], [{"px": "10.52"}]]})
    client.queue("l2Book:SOL", {"levels": [[{"px": "105.0"}], [{"px": "105.04"}]]})
    client.queue("exchange:order", _ok_status(111))
    client.queue("exchange:order", _ok_status(222))
    client.queue("userFills", [{"oid": 111, "px": "10.51", "sz": "5000.0"}])
    client.queue("userFills", [{"oid": 222, "px": "105.02", "sz": "500.0"}])

    adapter = _make_adapter(client)
    # Entry-time sizes (different from what 50000/current_mid would give).
    adapter.get_fill_prices(
        PairConfig("LINK", "SOL"),
        Direction.LONG_RATIO,
        50_000.0,
        is_close=True,
        close_sizes=(5000.0, 500.0),
    )

    order_calls = [
        c
        for c in client.calls
        if "/exchange" in c["url"] and c["json"]["action"]["type"] == "order"
    ]
    leg_a = order_calls[0]["json"]["action"]["orders"][0]
    leg_b = order_calls[1]["json"]["action"]["orders"][0]
    assert float(leg_a["s"]) == pytest.approx(5000.0)
    assert float(leg_b["s"]) == pytest.approx(500.0)


def test_close_without_close_sizes_raises():
    """Closes must always pass close_sizes — the runner reads them from the
    open position. Calling without is a programming error."""
    client = _MockClient()
    client.queue("meta", _meta_payload())
    adapter = _make_adapter(client)
    with pytest.raises(ExecutionError, match="close requires close_sizes"):
        adapter.get_fill_prices(
            PairConfig("LINK", "SOL"), Direction.LONG_RATIO, 50_000.0, is_close=True
        )


def test_actual_fees_summed_across_multi_level_fills():
    """A single IoC can match multiple resting orders → multiple userFills events.

    The adapter must sum `fee` across all events for the oid (and weight px
    by sz for the VWAP). Modeled fee uses the resulting VWAP × total size ×
    taker_fee_bps, which should agree with the actual sum within rounding.
    """
    client = _MockClient()
    client.queue("meta", _meta_payload())
    client.queue("exchange:updateLeverage", {"status": "ok"})
    client.queue("exchange:updateLeverage", {"status": "ok"})
    client.queue("l2Book:LINK", {"levels": [[{"px": "10.0"}], [{"px": "10.02"}]]})
    client.queue("l2Book:SOL", {"levels": [[{"px": "100.0"}], [{"px": "100.04"}]]})
    client.queue("exchange:order", _ok_status(111))
    client.queue("exchange:order", _ok_status(222))
    # LINK leg fills in two slices at slightly different prices. fee per slice
    # is what HL billed; total = 0.011 + 0.022 = 0.033.
    client.queue(
        "userFills",
        [
            {"oid": 111, "px": "10.005", "sz": "2000.0", "fee": "0.011"},
            {"oid": 111, "px": "10.005", "sz": "3000.0", "fee": "0.022"},
        ],
    )
    client.queue("userFills", [{"oid": 222, "px": "100.01", "sz": "500.0", "fee": "0.0225"}])

    adapter = _make_adapter(client)
    fill = adapter.get_fill_prices(PairConfig("LINK", "SOL"), Direction.LONG_RATIO, 50_000.0)
    assert fill.fee_a_actual == pytest.approx(0.033)
    assert fill.fee_b_actual == pytest.approx(0.0225)


def test_get_fill_prices_open_keeps_reduce_only_false():
    """Sanity: entries (is_close=False) keep reduce_only off."""
    client = _MockClient()
    client.queue("meta", _meta_payload())
    client.queue("exchange:updateLeverage", {"status": "ok"})
    client.queue("exchange:updateLeverage", {"status": "ok"})
    client.queue("l2Book:LINK", {"levels": [[{"px": "10.0"}], [{"px": "10.02"}]]})
    client.queue("l2Book:SOL", {"levels": [[{"px": "100.0"}], [{"px": "100.04"}]]})
    client.queue("exchange:order", _ok_status(111))
    client.queue("exchange:order", _ok_status(222))
    client.queue("userFills", [{"oid": 111, "px": "10.005", "sz": "5000.0"}])
    client.queue("userFills", [{"oid": 222, "px": "100.01", "sz": "500.0"}])

    adapter = _make_adapter(client)
    adapter.get_fill_prices(PairConfig("LINK", "SOL"), Direction.LONG_RATIO, 50_000.0)

    order_calls = [
        c
        for c in client.calls
        if "/exchange" in c["url"] and c["json"]["action"]["type"] == "order"
    ]
    assert order_calls[0]["json"]["action"]["orders"][0]["r"] is False
    assert order_calls[1]["json"]["action"]["orders"][0]["r"] is False


def test_get_fill_prices_warns_on_excess_slippage_but_returns(caplog):
    # max_slippage_bps is telemetry, not a control: by the time we observe
    # realized fills both legs are already on HL. Refusing to return would
    # orphan the position (exchange filled, engine state never updated).
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
    import logging

    with caplog.at_level(logging.WARNING):
        fill = adapter.get_fill_prices(PairConfig("LINK", "SOL"), Direction.LONG_RATIO, 50_000.0)

    assert fill.price_a == pytest.approx(10.05)
    assert fill.price_b == pytest.approx(100.01)
    assert any("slippage" in r.message and "LINK" in r.message for r in caplog.records)


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


def test_get_fill_prices_rejects_below_min_order_value():
    """HL rejects orders < $10 notional; better to abort before placing leg A."""
    client = _MockClient()
    client.queue("meta", _meta_payload())
    client.queue("exchange:updateLeverage", {"status": "ok"})
    client.queue("exchange:updateLeverage", {"status": "ok"})
    client.queue("l2Book:LINK", {"levels": [[{"px": "10.0"}], [{"px": "10.02"}]]})
    client.queue("l2Book:SOL", {"levels": [[{"px": "100.0"}], [{"px": "100.04"}]]})
    adapter = _make_adapter(client)
    # $5 per leg → SOL leg rounds to 0.05 (sz_decimals=2) * 100 = $5 < $10
    with pytest.raises(ExecutionError, match="below HL .10 minimum"):
        adapter.get_fill_prices(PairConfig("LINK", "SOL"), Direction.LONG_RATIO, 5.0)


def test_leg_b_failure_triggers_leg_a_flatten():
    """If leg B fails after leg A fills we MUST close leg A immediately.

    Flatten is sent WITHOUT reduceOnly: a reduceOnly flatten can reject if
    HL's per-coin net (across all pairs sharing this coin) doesn't agree
    with leg A's direction. Sending non-reduceOnly moves the HL net by
    exactly the leg amount, which is what we want to restore the
    pre-attempt state.
    """
    client = _MockClient()
    client.queue("meta", _meta_payload())
    client.queue("exchange:updateLeverage", {"status": "ok"})
    client.queue("exchange:updateLeverage", {"status": "ok"})
    client.queue("l2Book:LINK", {"levels": [[{"px": "10.0"}], [{"px": "10.02"}]]})
    client.queue("l2Book:SOL", {"levels": [[{"px": "100.0"}], [{"px": "100.04"}]]})
    # Leg A places fine
    client.queue("exchange:order", _ok_status(111, "filled"))
    # Leg B errors
    client.queue(
        "exchange:order",
        {"status": "ok", "response": {"data": {"statuses": [{"error": "Min size"}]}}},
    )
    # Flatten order for leg A succeeds
    client.queue("exchange:order", _ok_status(222, "filled"))
    adapter = _make_adapter(client)

    with pytest.raises(ExecutionError, match="Min size"):
        adapter.get_fill_prices(PairConfig("LINK", "SOL"), Direction.LONG_RATIO, 50_000.0)

    # 3 orders total: leg A, leg B (errored), flatten leg A.
    order_calls = [
        c
        for c in client.calls
        if "/exchange" in c["url"] and c["json"]["action"]["type"] == "order"
    ]
    assert len(order_calls) == 3
    flatten = order_calls[2]["json"]["action"]["orders"][0]
    assert flatten["r"] is False  # NOT reduceOnly — see docstring
    assert flatten["b"] is False  # opposite of original LONG_RATIO leg A buy
    # Closing a long → SELL → IoC limit must sit at or below the bid (10.0)
    # to cross immediately. A prior bug inverted the price side, putting the
    # limit above mid; HL refused with "could not immediately match against
    # any resting orders."
    assert float(flatten["p"]) <= 10.0


def test_close_leg_b_failure_triggers_leg_a_flatten_no_reduce_only():
    """Close-path: leg A successfully closes (sells), leg B rejects.

    The flatten must put leg A BACK into the position (a buy) so engine
    state stays consistent with HL. Non-reduceOnly because reduceOnly buy
    against an already-reduced long would reject. The runner retries on
    the next bar.
    """
    client = _MockClient()
    client.queue("meta", _meta_payload())
    client.queue("exchange:updateLeverage", {"status": "ok"})
    client.queue("exchange:updateLeverage", {"status": "ok"})
    client.queue("l2Book:LINK", {"levels": [[{"px": "10.0"}], [{"px": "10.02"}]]})
    client.queue("l2Book:SOL", {"levels": [[{"px": "100.0"}], [{"px": "100.04"}]]})
    # Closing a LONG_RATIO entry inverts legs: SELL LINK, BUY SOL.
    # Leg A (sell LINK) places ok.
    client.queue("exchange:order", _ok_status(111, "filled"))
    # Leg B (buy SOL) rejects.
    client.queue(
        "exchange:order",
        {"status": "ok", "response": {"data": {"statuses": [{"error": "Insufficient margin"}]}}},
    )
    # Flatten leg A (buy LINK back) succeeds.
    client.queue("exchange:order", _ok_status(333, "filled"))
    adapter = _make_adapter(client)

    with pytest.raises(ExecutionError, match="Insufficient margin"):
        adapter.get_fill_prices(
            PairConfig("LINK", "SOL"),
            Direction.LONG_RATIO,
            50_000.0,
            is_close=True,
            close_sizes=(5000.0, 500.0),
        )

    order_calls = [
        c
        for c in client.calls
        if "/exchange" in c["url"] and c["json"]["action"]["type"] == "order"
    ]
    assert len(order_calls) == 3
    leg_a = order_calls[0]["json"]["action"]["orders"][0]
    flatten = order_calls[2]["json"]["action"]["orders"][0]
    # Leg A close of a LONG_RATIO long = SELL LINK.
    assert leg_a["b"] is False
    assert leg_a["r"] is False  # close not reduceOnly
    # Flatten = BUY LINK (restore), non-reduceOnly.
    assert flatten["b"] is True
    assert flatten["r"] is False
    # Same size as leg A (restoring exactly what was sold).
    assert float(flatten["s"]) == pytest.approx(float(leg_a["s"]))


# -- fetch_user_state ------------------------------------------------------


def test_fetch_user_state_returns_clearinghouse_payload():
    client = _MockClient()
    expected = {"assetPositions": [{"position": {"coin": "LINK", "szi": "5000"}}]}
    client.queue("clearinghouseState", expected)
    adapter = _make_adapter(client)
    assert adapter.fetch_user_state() == expected
