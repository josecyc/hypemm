"""Hyperliquid WebSocket + REST client for L2 orderbooks and trades.

Handles both standard perps (e.g. "BTC") and HIP-3 spot pairs (e.g. "@182").
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass

import httpx
import websockets

REST_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"


class OrderBook:
    """Maintains a local L2 orderbook from snapshots and delta updates."""

    def __init__(self):
        self.bids: dict[float, float] = {}  # price -> size
        self.asks: dict[float, float] = {}  # price -> size
        self.timestamp: int = 0

    def set_snapshot(self, levels: list) -> None:
        """Replace entire book from a REST snapshot."""
        self.bids.clear()
        self.asks.clear()
        self._apply(levels)

    def apply_update(self, levels: list, timestamp: int | None = None) -> None:
        """Apply a delta update from WebSocket. sz=0 means remove level."""
        self._apply(levels)
        if timestamp:
            self.timestamp = timestamp

    def _apply(self, levels: list) -> None:
        if len(levels) < 2:
            return
        for entry in levels[0]:  # bids
            px, sz = float(entry["px"]), float(entry["sz"])
            if sz == 0:
                self.bids.pop(px, None)
            else:
                self.bids[px] = sz
        for entry in levels[1]:  # asks
            px, sz = float(entry["px"]), float(entry["sz"])
            if sz == 0:
                self.asks.pop(px, None)
            else:
                self.asks[px] = sz

    @property
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    @property
    def mid_price(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        return (bb + ba) / 2 if bb is not None and ba is not None else None

    def spread_bps(self) -> float | None:
        bb, ba, mid = self.best_bid, self.best_ask, self.mid_price
        if bb is not None and ba is not None and mid and mid > 0:
            return (ba - bb) / mid * 10_000
        return None

    def depth_at_bps(self, bps: float) -> float:
        """Sum of USD liquidity within `bps` basis points of mid on both sides."""
        mid = self.mid_price
        if not mid or mid <= 0:
            return 0.0
        threshold = mid * bps / 10_000
        total = 0.0
        for px, sz in self.bids.items():
            if mid - px <= threshold:
                total += px * sz
        for px, sz in self.asks.items():
            if px - mid <= threshold:
                total += px * sz
        return total


@dataclass
class Trade:
    coin: str
    side: str  # "B" = buy (taker bought), "A" = sell (taker sold)
    price: float
    size: float
    timestamp: int  # milliseconds

    @property
    def usd_value(self) -> float:
        return self.price * self.size

    @property
    def is_buy(self) -> bool:
        return self.side == "B"


class HyperliquidFeed:
    def __init__(self, coins: list[str]):
        self.coins = coins
        self.books: dict[str, OrderBook] = {c: OrderBook() for c in coins}
        self.trades: dict[str, list[Trade]] = defaultdict(list)
        self.asset_ctxs: dict[str, dict] = {}  # coin -> context data
        self.spot_tokens: dict[int, dict] = {}  # token_index -> token_info
        self._running = False
        self._max_trade_age_ms = 3_600_000  # 1 hour

    # ── REST ──────────────────────────────────────────────────────────

    async def _post(self, payload: dict) -> dict | list:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(REST_URL, json=payload)
            r.raise_for_status()
            return r.json()

    async def fetch_meta(self) -> None:
        """Fetch metadata for both perps and spot markets."""
        # Perps metadata (for BTC control and any perp coins)
        try:
            data = await self._post({"type": "metaAndAssetCtxs"})
            universe = data[0]["universe"]
            ctxs = data[1]
            for i, asset in enumerate(universe):
                if i < len(ctxs):
                    self.asset_ctxs[asset["name"]] = {
                        "source": "perp", **asset, **ctxs[i],
                    }
        except Exception as e:
            print(f"  Warning: perps metadata failed: {e}")

        # Spot metadata (for HIP-3 RWA pairs)
        try:
            data = await self._post({"type": "spotMetaAndAssetCtxs"})
            spot_meta = data[0]
            spot_ctxs = data[1]
            tokens = spot_meta.get("tokens", [])
            universe = spot_meta.get("universe", [])

            # Build token index -> info map
            for t in tokens:
                self.spot_tokens[t["index"]] = t

            # Map universe pairs to their contexts
            for i, pair in enumerate(universe):
                pair_name = pair.get("name", "")
                ctx = spot_ctxs[i] if i < len(spot_ctxs) else {}
                # Resolve token names for display
                pair_tokens = pair.get("tokens", [])
                token_names = []
                for tidx in pair_tokens:
                    tinfo = self.spot_tokens.get(tidx, {})
                    token_names.append(tinfo.get("name", str(tidx)))

                self.asset_ctxs[pair_name] = {
                    "source": "spot",
                    "pair_name": pair_name,
                    "token_names": token_names,
                    "token_indices": pair_tokens,
                    **{k: v for k, v in pair.items() if k != "tokens"},
                    **ctx,
                }
        except Exception as e:
            print(f"  Warning: spot metadata failed: {e}")

    async def fetch_initial_books(self) -> None:
        """Fetch initial L2 book snapshots for all monitored coins."""
        for coin in self.coins:
            try:
                data = await self._post({"type": "l2Book", "coin": coin})
                if isinstance(data, dict) and data.get("levels"):
                    self.books[coin].set_snapshot(data["levels"])
            except Exception as e:
                print(f"  Warning: book snapshot failed for {coin}: {e}")

    async def fetch_recent_trades(self) -> None:
        """Seed trade history from REST endpoint."""
        for coin in self.coins:
            try:
                data = await self._post({"type": "recentTrades", "coin": coin})
                if isinstance(data, list):
                    for t in data:
                        self.trades[coin].append(Trade(
                            coin=t.get("coin", coin),
                            side=t.get("side", ""),
                            price=float(t["px"]),
                            size=float(t["sz"]),
                            timestamp=t["time"],
                        ))
            except Exception as e:
                print(f"  Warning: recent trades failed for {coin}: {e}")

    # ── WebSocket ─────────────────────────────────────────────────────

    async def connect_ws(self) -> None:
        """Connect to Hyperliquid WS with auto-reconnect (exponential backoff)."""
        self._running = True
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(
                    WS_URL, ping_interval=20, ping_timeout=10,
                ) as ws:
                    backoff = 1
                    for coin in self.coins:
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "l2Book", "coin": coin},
                        }))
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "trades", "coin": coin},
                        }))
                    async for raw in ws:
                        self._handle_msg(raw)
            except asyncio.CancelledError:
                return
            except Exception:
                if self._running:
                    await asyncio.sleep(min(backoff, 30))
                    backoff = min(backoff * 2, 30)

    def _handle_msg(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        ch = msg.get("channel")
        data = msg.get("data")
        if not data:
            return

        if ch == "l2Book":
            coin = data.get("coin")
            if coin in self.books and "levels" in data:
                # WS sends full visible book each time — replace entirely
                self.books[coin].set_snapshot(data["levels"])
                if data.get("time"):
                    self.books[coin].timestamp = data["time"]

        elif ch == "trades":
            if isinstance(data, list):
                for t in data:
                    coin = t.get("coin", "")
                    if coin in self.books:
                        self.trades[coin].append(Trade(
                            coin=coin,
                            side=t.get("side", ""),
                            price=float(t["px"]),
                            size=float(t["sz"]),
                            timestamp=t["time"],
                        ))

    # ── Maintenance ───────────────────────────────────────────────────

    def prune_trades(self) -> None:
        """Remove trades older than 1 hour to bound memory."""
        cutoff = int(time.time() * 1000) - self._max_trade_age_ms
        for coin in self.trades:
            self.trades[coin] = [
                t for t in self.trades[coin] if t.timestamp > cutoff
            ]

    async def refresh_asset_ctxs_loop(self) -> None:
        """Periodically refresh metadata (every 60s)."""
        while self._running:
            await asyncio.sleep(60)
            try:
                await self.fetch_meta()
            except asyncio.CancelledError:
                return
            except Exception:
                pass

    def stop(self) -> None:
        self._running = False
