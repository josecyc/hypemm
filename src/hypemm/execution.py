"""Execution adapters: protocol and paper / live trading implementations."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Protocol

import httpx

from hypemm.hl_meta import AssetMeta, fetch_asset_meta, format_price, format_size, round_price
from hypemm.hl_sign import sign_l1_action
from hypemm.models import ConfigurationError, DataFetchError, Direction, HypeMMError, PairConfig
from hypemm.orderbook import book_vwap

logger = logging.getLogger(__name__)


def _load_key_from_keystore(spec: str, password: str) -> str:
    """Decrypt a Foundry-style keystore and return the hex private key.

    spec may be a short name like "rpo-81" (expanded to
    ~/.foundry/keystores/rpo-81) or an absolute path. Matches the convention
    used elsewhere in this codebase's broader stack.
    """
    from eth_account import Account

    path = Path(spec).expanduser()
    if not path.is_absolute() and not path.exists():
        path = Path("~/.foundry/keystores").expanduser() / spec
    if not path.exists():
        raise ConfigurationError(f"keystore not found: {path}")
    with open(path) as f:
        priv_bytes = Account.decrypt(json.load(f), password)
    return priv_bytes.hex() if isinstance(priv_bytes, bytes) else str(priv_bytes)


def _resolve_private_key(explicit: str | None) -> str:
    """Resolve the signing key from explicit arg, env, or Foundry keystore.

    Priority:
      1. explicit `private_key` argument
      2. HYPERLIQUID_PRIVATE_KEY env (raw hex)
      3. HYPERLIQUID_KEYSTORE env (path or rpo-name) +
         HYPERLIQUID_KEYSTORE_PWD (or RPO_KEYSTORE_PWD as fallback)
    """
    if explicit:
        return explicit
    raw = os.environ.get("HYPERLIQUID_PRIVATE_KEY")
    if raw:
        return raw
    keystore = os.environ.get("HYPERLIQUID_KEYSTORE")
    if not keystore:
        raise ConfigurationError(
            "No signing key found. Set HYPERLIQUID_PRIVATE_KEY (hex), or "
            "HYPERLIQUID_KEYSTORE (path or rpo-name) + HYPERLIQUID_KEYSTORE_PWD."
        )
    pwd = os.environ.get("HYPERLIQUID_KEYSTORE_PWD") or os.environ.get("RPO_KEYSTORE_PWD")
    if not pwd:
        raise ConfigurationError(
            "HYPERLIQUID_KEYSTORE is set but no password found. Set "
            "HYPERLIQUID_KEYSTORE_PWD or RPO_KEYSTORE_PWD."
        )
    return _load_key_from_keystore(keystore, pwd)


class ExecutionError(HypeMMError):
    """Order placement, fill, or reconciliation failure."""


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
    """Execute paper trades by walking the live HL L2 book.

    Fills at the realized VWAP for the configured notional, NOT mid. This
    means paper P&L includes the spread-crossing cost the live runner would
    pay — same model, no separate slippage calibration needed.
    """

    def __init__(self, rest_url: str) -> None:
        self.rest_url = rest_url
        self.client = httpx.Client(timeout=10)

    def get_fill_prices(
        self,
        pair: PairConfig,
        direction: Direction,
        notional_per_leg: float,
    ) -> tuple[float, float]:
        """Walk the L2 book on each leg and return the realized VWAP."""
        is_buy_a = direction == Direction.LONG_RATIO
        is_buy_b = not is_buy_a
        fa = book_vwap(self.client, self.rest_url, pair.coin_a, is_buy_a, notional_per_leg)
        fb = book_vwap(self.client, self.rest_url, pair.coin_b, is_buy_b, notional_per_leg)
        logger.info(
            "Paper fill %s %s: %s slip=%.2fbps, %s slip=%.2fbps",
            pair.label,
            direction.label,
            pair.coin_a,
            fa.slippage_bps,
            pair.coin_b,
            fb.slippage_bps,
        )
        return fa.vwap, fb.vwap

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

    Builds order actions, signs them via the EIP-712 phantom-agent scheme, posts
    to /exchange, polls /info userFills for fills, and returns the realized
    VWAP per leg. Refuses to fill if the realized VWAP differs from the signal
    mid by more than max_slippage_bps.

    Required environment (one of these signing-key sources):
        HYPERLIQUID_PRIVATE_KEY  — raw hex private key (0x-prefixed), OR
        HYPERLIQUID_KEYSTORE     — Foundry keystore path or short name
                                   (e.g. "rpo-81" → ~/.foundry/keystores/rpo-81)
        HYPERLIQUID_KEYSTORE_PWD — password for the keystore (or RPO_KEYSTORE_PWD)

    Optional:
        HYPERLIQUID_ACCOUNT      — main account address (0x-prefixed). If unset,
                                   defaults to the signer's address (i.e. the
                                   keystore is the trading account itself rather
                                   than an API wallet for a separate vault).
        HYPERLIQUID_API_URL      — base URL, default mainnet; testnet =
                                   https://api.hyperliquid-testnet.xyz
    """

    MAINNET_URL = "https://api.hyperliquid.xyz"
    TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
    INFO_PATH = "/info"
    EXCHANGE_PATH = "/exchange"

    def __init__(
        self,
        rest_url: str | None = None,
        *,
        private_key: str | None = None,
        account_address: str | None = None,
        leverage: int = 5,
        is_cross_margin: bool = True,
        max_slippage_bps: float = 5.0,
        ioc_aggression_bps: float = 10.0,
        fill_poll_seconds: float = 0.5,
        fill_timeout_seconds: float = 30.0,
    ) -> None:
        from eth_account import Account

        url = rest_url or os.environ.get("HYPERLIQUID_API_URL") or self.MAINNET_URL
        self.rest_url = url.rstrip("/")
        self.is_mainnet = self.MAINNET_URL in self.rest_url
        self._private_key = _resolve_private_key(private_key)
        self._signer = Account.from_key(self._private_key)
        # If HYPERLIQUID_ACCOUNT is unset, assume the keystore is the trading
        # account itself (no separate API wallet / vault). This matches the
        # rpo-{nb} convention where the keystore IS the account.
        self._account_address = (
            account_address
            or os.environ.get("HYPERLIQUID_ACCOUNT")
            or self._signer.address
        )
        self.leverage = leverage
        self.is_cross_margin = is_cross_margin
        self.max_slippage_bps = max_slippage_bps
        self.ioc_aggression_bps = ioc_aggression_bps
        self.fill_poll_seconds = fill_poll_seconds
        self.fill_timeout_seconds = fill_timeout_seconds
        self.client = httpx.Client(timeout=10)

        self._meta: dict[str, AssetMeta] | None = None
        self._leverage_set: set[str] = set()
        logger.warning(
            "LiveExecutionAdapter initialized: account=%s url=%s mainnet=%s lev=%dx",
            self._account_address,
            self.rest_url,
            self.is_mainnet,
            self.leverage,
        )

    # -- public API ----------------------------------------------------------

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
        """Place IoC orders for both legs and return realized VWAP fill prices.

        Direction LONG_RATIO = long A, short B. SHORT_RATIO = short A, long B.
        Aborts (raises ExecutionError) if any leg fails to fill within
        fill_timeout_seconds, or if the realized fill exceeds max_slippage_bps.
        """
        meta = self._ensure_meta()
        for coin in (pair.coin_a, pair.coin_b):
            if coin not in meta:
                raise ExecutionError(f"{coin} not in HL universe")

        # Set leverage on first use of each coin.
        for coin in (pair.coin_a, pair.coin_b):
            if coin not in self._leverage_set:
                self._set_leverage(meta[coin])
                self._leverage_set.add(coin)

        is_buy_a = direction == Direction.LONG_RATIO
        is_buy_b = not is_buy_a

        mid_a = self.fetch_mid(pair.coin_a)
        mid_b = self.fetch_mid(pair.coin_b)
        size_a = notional_per_leg / mid_a
        size_b = notional_per_leg / mid_b

        # Pre-flight: HL rejects orders below $10 notional. We round size to
        # szDecimals before placing, so check the post-rounding order value.
        # Better to reject both legs cleanly than fill leg A and orphan it.
        from hypemm.hl_meta import round_size

        rounded_a = round_size(size_a, meta[pair.coin_a].sz_decimals)
        rounded_b = round_size(size_b, meta[pair.coin_b].sz_decimals)
        for coin, rounded, mid in (
            (pair.coin_a, rounded_a, mid_a),
            (pair.coin_b, rounded_b, mid_b),
        ):
            if rounded * mid < 10.0:
                raise ExecutionError(
                    f"{coin} order value {rounded * mid:.2f} below HL $10 minimum "
                    f"(notional {notional_per_leg}, size {rounded}, mid {mid:.6f})"
                )

        order_id_a = self._place_ioc(meta[pair.coin_a], is_buy_a, size_a, mid_a)
        # If leg B fails AFTER leg A filled, we MUST flatten leg A to avoid an
        # unhedged position. Try-block scopes that recovery; failures inside
        # are logged but the original error is what propagates.
        try:
            order_id_b = self._place_ioc(meta[pair.coin_b], is_buy_b, size_b, mid_b)
            fill_a = self._await_fill(pair.coin_a, order_id_a)
            fill_b = self._await_fill(pair.coin_b, order_id_b)
        except ExecutionError as e:
            logger.error("Leg B failed (%s) — attempting to flatten leg A", e)
            self._flatten_position(meta[pair.coin_a], is_buy_a, size_a, mid_a)
            raise

        self._check_slippage(pair.coin_a, fill_a, mid_a)
        self._check_slippage(pair.coin_b, fill_b, mid_b)

        return fill_a, fill_b

    def _flatten_position(
        self, asset: AssetMeta, was_buy: bool, size: float, mid_price: float
    ) -> None:
        """Emergency flatten: place a reduceOnly IoC opposite to the original."""
        sign = -1 if was_buy else 1  # opposite direction to close
        crossing_price = mid_price * (1 + sign * (-self.ioc_aggression_bps) / 10_000)
        crossing_price = round_price(crossing_price, asset.sz_decimals)
        order = {
            "a": asset.asset_id,
            "b": not was_buy,
            "p": format_price(crossing_price, asset.sz_decimals),
            "s": format_size(size, asset.sz_decimals),
            "r": True,  # reduceOnly
            "t": {"limit": {"tif": "Ioc"}},
        }
        action = {"type": "order", "orders": [order], "grouping": "na"}
        try:
            resp = self._post_signed(action)
            logger.warning(
                "FLATTEN %s: response=%s", asset.coin, resp
            )
        except Exception as e:
            logger.critical(
                "FLATTEN FAILED for %s — manual intervention required: %s",
                asset.coin, e,
            )

    def fetch_user_state(self) -> dict[str, Any]:
        """Fetch /info clearinghouseState — used for startup reconciliation."""
        r = self.client.post(
            self.rest_url + self.INFO_PATH,
            json={"type": "clearinghouseState", "user": self._account_address},
        )
        r.raise_for_status()
        return dict(r.json())

    def close(self) -> None:
        self.client.close()

    # -- internals -----------------------------------------------------------

    def _ensure_meta(self) -> dict[str, AssetMeta]:
        if self._meta is None:
            self._meta = fetch_asset_meta(self.client, self.rest_url + self.INFO_PATH)
            logger.info("Fetched HL meta: %d assets", len(self._meta))
        return self._meta

    def _set_leverage(self, asset: AssetMeta) -> None:
        action = {
            "type": "updateLeverage",
            "asset": asset.asset_id,
            "isCross": self.is_cross_margin,
            "leverage": self.leverage,
        }
        resp = self._post_signed(action)
        if resp.get("status") != "ok":
            raise ExecutionError(
                f"updateLeverage failed for {asset.coin}: {resp!r}"
            )
        logger.info(
            "Set %s leverage to %dx (%s)",
            asset.coin,
            self.leverage,
            "cross" if self.is_cross_margin else "isolated",
        )

    def _place_ioc(
        self,
        asset: AssetMeta,
        is_buy: bool,
        size: float,
        mid_price: float,
    ) -> int:
        """Place an IoC limit order with a price aggressive enough to cross."""
        sign = 1 if is_buy else -1
        crossing_price = mid_price * (1 + sign * self.ioc_aggression_bps / 10_000)
        crossing_price = round_price(crossing_price, asset.sz_decimals)

        order = {
            "a": asset.asset_id,
            "b": is_buy,
            "p": format_price(crossing_price, asset.sz_decimals),
            "s": format_size(size, asset.sz_decimals),
            "r": False,
            "t": {"limit": {"tif": "Ioc"}},
        }
        action = {"type": "order", "orders": [order], "grouping": "na"}
        resp = self._post_signed(action)

        if resp.get("status") != "ok":
            raise ExecutionError(f"order rejected: {resp!r}")
        try:
            statuses = resp["response"]["data"]["statuses"]
            status = statuses[0]
            if "filled" in status:
                return int(status["filled"]["oid"])
            if "resting" in status:
                return int(status["resting"]["oid"])
            if "error" in status:
                raise ExecutionError(f"order error for {asset.coin}: {status['error']}")
            raise ExecutionError(f"unexpected order status for {asset.coin}: {status!r}")
        except (KeyError, IndexError, ValueError) as e:
            raise ExecutionError(f"malformed order response: {resp!r} ({e})")

    def _await_fill(self, coin: str, oid: int) -> float:
        """Poll /info userFills for the given order until fully filled or timeout.

        Returns the volume-weighted average fill price.
        """
        deadline = time.monotonic() + self.fill_timeout_seconds
        target_oid = oid
        while time.monotonic() < deadline:
            r = self.client.post(
                self.rest_url + self.INFO_PATH,
                json={"type": "userFills", "user": self._account_address},
            )
            r.raise_for_status()
            fills = r.json()
            if isinstance(fills, list):
                matched = [f for f in fills if int(f.get("oid", -1)) == target_oid]
                if matched:
                    total_sz = sum(float(f["sz"]) for f in matched)
                    if total_sz > 0:
                        vwap = sum(float(f["px"]) * float(f["sz"]) for f in matched) / total_sz
                        return vwap
            time.sleep(self.fill_poll_seconds)

        raise ExecutionError(
            f"fill timeout for {coin} order {oid} after {self.fill_timeout_seconds}s"
        )

    def _check_slippage(self, coin: str, fill_price: float, mid_price: float) -> None:
        slip_bps = abs(fill_price - mid_price) / mid_price * 10_000
        if slip_bps > self.max_slippage_bps:
            raise ExecutionError(
                f"{coin} fill {fill_price:.6f} vs mid {mid_price:.6f} = "
                f"{slip_bps:.1f} bps slippage > {self.max_slippage_bps} bps cap"
            )

    def _post_signed(self, action: dict[str, Any]) -> dict[str, Any]:
        nonce_ms = int(time.time() * 1000)
        signature = sign_l1_action(
            self._signer, action, nonce_ms, is_mainnet=self.is_mainnet
        )
        body = {"action": action, "nonce": nonce_ms, "signature": signature}
        r = self.client.post(self.rest_url + self.EXCHANGE_PATH, json=body, timeout=15.0)
        r.raise_for_status()
        return dict(r.json())


def build_adapter(
    rest_url: str,
    *,
    live: bool,
    leverage: int = 5,
    is_cross_margin: bool = True,
    max_slippage_bps: float = 5.0,
    ioc_aggression_bps: float = 10.0,
    fill_poll_seconds: float = 0.5,
    fill_timeout_seconds: float = 30.0,
) -> ExecutionAdapter:
    """Construct an execution adapter from app config.

    Centralized so the kill-switch confirmation lives in one place.
    """
    if live:
        # If config gave us /info, the LiveExecutionAdapter wants the base URL
        rest_root = rest_url.rsplit("/info", 1)[0] if rest_url.endswith("/info") else rest_url
        return LiveExecutionAdapter(
            rest_root,
            leverage=leverage,
            is_cross_margin=is_cross_margin,
            max_slippage_bps=max_slippage_bps,
            ioc_aggression_bps=ioc_aggression_bps,
            fill_poll_seconds=fill_poll_seconds,
            fill_timeout_seconds=fill_timeout_seconds,
        )
    return PaperExecutionAdapter(rest_url)
