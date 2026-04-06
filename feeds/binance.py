"""Binance Futures WebSocket client for L2 depth snapshots."""

from __future__ import annotations

import asyncio
import json

import websockets


class BinanceBook:
    """Orderbook from Binance partial depth snapshots (depth20@100ms)."""

    def __init__(self):
        self.bids: list[tuple[float, float]] = []  # sorted desc by price
        self.asks: list[tuple[float, float]] = []  # sorted asc by price
        self.timestamp: int = 0

    def update(
        self,
        bids: list,
        asks: list,
        timestamp: int | None = None,
    ) -> None:
        self.bids = sorted(
            [(float(b[0]), float(b[1])) for b in bids],
            key=lambda x: -x[0],
        )
        self.asks = sorted(
            [(float(a[0]), float(a[1])) for a in asks],
            key=lambda x: x[0],
        )
        if timestamp:
            self.timestamp = timestamp

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

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
        mid = self.mid_price
        if not mid or mid <= 0:
            return 0.0
        threshold = mid * bps / 10_000
        total = 0.0
        for px, sz in self.bids:
            if mid - px <= threshold:
                total += px * sz
            else:
                break
        for px, sz in self.asks:
            if px - mid <= threshold:
                total += px * sz
            else:
                break
        return total


class BinanceFeed:
    def __init__(self, symbols: list[str]):
        self.symbols = [s.lower() for s in symbols]
        self.books: dict[str, BinanceBook] = {s: BinanceBook() for s in self.symbols}
        self._running = False
        self._active_symbols: list[str] = list(self.symbols)

    def _make_url(self, symbols: list[str]) -> str:
        streams = "/".join(f"{s}@depth20@100ms" for s in symbols)
        return f"wss://fstream.binance.com/stream?streams={streams}"

    async def connect_ws(self) -> None:
        """Connect to Binance combined stream with auto-reconnect."""
        self._running = True
        backoff = 1
        fail_count = 0

        while self._running:
            if not self._active_symbols:
                await asyncio.sleep(30)
                self._active_symbols = list(self.symbols)
                continue

            try:
                url = self._make_url(self._active_symbols)
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=10,
                ) as ws:
                    backoff = 1
                    fail_count = 0
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            stream = msg.get("stream", "")
                            data = msg.get("data", {})
                            symbol = stream.split("@")[0]
                            if symbol in self.books:
                                self.books[symbol].update(
                                    bids=data.get("bids", data.get("b", [])),
                                    asks=data.get("asks", data.get("a", [])),
                                    timestamp=data.get("E"),
                                )
                        except (KeyError, ValueError, TypeError):
                            pass
            except asyncio.CancelledError:
                return
            except Exception:
                fail_count += 1
                # After 3 consecutive failures, drop last symbol (might be invalid)
                if fail_count >= 3 and len(self._active_symbols) > 1:
                    dropped = self._active_symbols.pop()
                    print(f"  Warning: dropped Binance stream {dropped} after repeated failures")
                    fail_count = 0
                if self._running:
                    await asyncio.sleep(min(backoff, 30))
                    backoff = min(backoff * 2, 30)

    def stop(self) -> None:
        self._running = False
