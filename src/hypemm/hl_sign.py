"""Hyperliquid L1 action signing.

Implements the signature scheme used by Hyperliquid's `/exchange` endpoint.
The wire format is a deterministic msgpack-encoded action plus an 8-byte big-
endian nonce plus an optional vault byte, hashed with keccak to produce a
"connection_id", which is then signed as an EIP-712 typed message under the
phantom-agent domain (chainId=1337, name="Exchange").

Mainnet uses source="a"; testnet uses source="b". Wrong source byte makes the
verifier compute a different connection_id than what we signed and the
exchange returns "User does not exist" or similar.

References:
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint/signing
- https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/main/hyperliquid/utils/signing.py
"""

from __future__ import annotations

from typing import Any

import msgpack  # type: ignore[import-untyped]
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils import keccak  # type: ignore[attr-defined]

PHANTOM_DOMAIN = {
    "name": "Exchange",
    "version": "1",
    "chainId": 1337,
    "verifyingContract": "0x0000000000000000000000000000000000000000",
}

PHANTOM_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Agent": [
        {"name": "source", "type": "string"},
        {"name": "connectionId", "type": "bytes32"},
    ],
}


def action_hash(action: dict[str, Any], nonce_ms: int, vault_address: str | None) -> bytes:
    """Compute the connection_id keccak hash for an action.

    msgpack encodes the action; the encoding must match Hyperliquid's
    canonical form. Python dicts preserve insertion order in 3.7+, so build
    actions with field ordering matching the SDK.
    """
    encoded = msgpack.packb(action)
    if encoded is None:
        raise ValueError("msgpack returned None")
    payload = encoded + nonce_ms.to_bytes(8, "big")
    if vault_address is None:
        payload += b"\x00"
    else:
        payload += b"\x01" + bytes.fromhex(vault_address.removeprefix("0x"))
    return keccak(payload)


def sign_l1_action(
    account: LocalAccount,
    action: dict[str, Any],
    nonce_ms: int,
    *,
    is_mainnet: bool,
    vault_address: str | None = None,
) -> dict[str, str | int]:
    """Sign an L1 action and return the {r, s, v} signature dict for /exchange."""
    connection_id = action_hash(action, nonce_ms, vault_address)
    source = "a" if is_mainnet else "b"
    msg = {
        "types": PHANTOM_TYPES,
        "primaryType": "Agent",
        "domain": PHANTOM_DOMAIN,
        "message": {"source": source, "connectionId": connection_id},
    }
    signed = Account.sign_typed_data(account.key, full_message=msg)
    return {
        "r": "0x" + signed.r.to_bytes(32, "big").hex(),
        "s": "0x" + signed.s.to_bytes(32, "big").hex(),
        "v": signed.v,
    }
