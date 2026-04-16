"""Hourly price buffer for live signal computation.

Replicates the backtest timescale: one data point per hour.
The seed phase loads historical hourly candles, and the live phase
updates only the current (latest) bar in-place on each poll.
A new bar is appended only when the hour rolls over.

Uses epoch hours (timestamp_ms // 3_600_000) instead of UTC hour (0-23)
to correctly handle gaps longer than 24 hours.
"""

from __future__ import annotations

from collections import deque

import numpy as np


class HourlyPriceBuffer:
    """Buffer of hourly close prices for multiple coins."""

    def __init__(self, coins: list[str], max_hours: int = 300) -> None:
        self.prices: dict[str, deque[float]] = {c: deque(maxlen=max_hours) for c in coins}
        self.last_epoch_hour: int = -1

    def seed(self, coin: str, hourly_closes: list[float], last_candle_epoch_hour: int) -> None:
        """Bulk-load historical hourly closes.

        last_candle_epoch_hour is the epoch hour of the final candle's open time.
        """
        for px in hourly_closes:
            self.prices[coin].append(px)
        self.last_epoch_hour = last_candle_epoch_hour

    def update_live(self, coin: str, price: float, epoch_hour: int) -> None:
        """Update with a live mid-price.

        Same epoch_hour as last bar: overwrite (intra-hour update).
        Different epoch_hour: append a new bar.
        """
        buf = self.prices[coin]
        if not buf:
            buf.append(price)
            return

        if epoch_hour != self.last_epoch_hour:
            buf.append(price)
        else:
            buf[-1] = price

    def advance_hour(self, epoch_hour: int) -> bool:
        """Record the current epoch hour. Returns True if the hour changed."""
        changed = epoch_hour != self.last_epoch_hour
        self.last_epoch_hour = epoch_hour
        return changed

    def get_prices(self, coin: str) -> np.ndarray:
        """Get price array for a coin."""
        return np.array(self.prices[coin], dtype=np.float64)

    @property
    def bar_count(self) -> int:
        """Number of bars in the buffer (from the first coin)."""
        for buf in self.prices.values():
            return len(buf)
        return 0
