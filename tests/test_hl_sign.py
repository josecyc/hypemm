"""Tests for Hyperliquid L1 signing.

The signing path is deterministic by construction: the same key + action +
nonce + mainnet flag must always produce the same signature, otherwise we
can't replay or reason about it. We assert the canonical bytes here so any
future change is loud.
"""

from __future__ import annotations

import pytest
from eth_account import Account

from hypemm.hl_sign import action_hash, sign_l1_action

KEY = "0x" + "11" * 32
ACTION = {"type": "updateLeverage", "asset": 0, "isCross": True, "leverage": 5}
NONCE = 1_700_000_000_000


def test_action_hash_is_deterministic():
    h1 = action_hash(ACTION, NONCE, None)
    h2 = action_hash(ACTION, NONCE, None)
    assert h1 == h2
    assert len(h1) == 32  # keccak256


def test_action_hash_changes_with_nonce():
    h1 = action_hash(ACTION, NONCE, None)
    h2 = action_hash(ACTION, NONCE + 1, None)
    assert h1 != h2


def test_action_hash_changes_with_vault():
    h1 = action_hash(ACTION, NONCE, None)
    h2 = action_hash(ACTION, NONCE, "0x" + "ab" * 20)
    assert h1 != h2


def test_signature_is_deterministic():
    acct = Account.from_key(KEY)
    s1 = sign_l1_action(acct, ACTION, NONCE, is_mainnet=True)
    s2 = sign_l1_action(acct, ACTION, NONCE, is_mainnet=True)
    assert s1 == s2


def test_mainnet_vs_testnet_produce_different_sigs():
    acct = Account.from_key(KEY)
    s_main = sign_l1_action(acct, ACTION, NONCE, is_mainnet=True)
    s_test = sign_l1_action(acct, ACTION, NONCE, is_mainnet=False)
    assert s_main != s_test


def test_signature_format():
    acct = Account.from_key(KEY)
    s = sign_l1_action(acct, ACTION, NONCE, is_mainnet=True)
    assert isinstance(s["r"], str) and s["r"].startswith("0x") and len(s["r"]) == 66
    assert isinstance(s["s"], str) and s["s"].startswith("0x") and len(s["s"]) == 66
    assert s["v"] in (27, 28)


def test_action_field_order_matters():
    """msgpack is order-sensitive; the docs require fields in a specific order.

    If we swap field order the hash MUST change, otherwise we'd silently produce
    sigs that the verifier rejects.
    """
    a1 = {"type": "updateLeverage", "asset": 0, "isCross": True, "leverage": 5}
    a2 = {"asset": 0, "type": "updateLeverage", "isCross": True, "leverage": 5}
    assert action_hash(a1, NONCE, None) != action_hash(a2, NONCE, None)


def test_known_signature_regression():
    """Lock in the exact signature for a fixed input. If this changes silently,
    we've broken signing for the live exchange."""
    acct = Account.from_key(KEY)
    s = sign_l1_action(acct, ACTION, NONCE, is_mainnet=True)
    assert s == {
        "r": "0x55e97652574531f3ab0e059fc4dcae66dcb93121b6652f44b0e6c0674ae54a29",
        "s": "0x0bebff59a60ef5683433b3231de2adcea2eacbf17d2f06141b3008413e1ae969",
        "v": 27,
    }


def test_invalid_vault_raises():
    with pytest.raises((ValueError, IndexError)):
        action_hash(ACTION, NONCE, "not-a-hex-string")
