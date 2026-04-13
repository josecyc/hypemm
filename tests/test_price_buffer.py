"""Tests for the hourly price buffer."""

from __future__ import annotations

import numpy as np

from hypemm.data.price_buffer import HourlyPriceBuffer


class TestHourlyPriceBuffer:
    def test_seed_populates_buffer(self) -> None:
        buf = HourlyPriceBuffer(coins=["BTC"], max_hours=100)
        closes = [100.0, 101.0, 102.0]
        buf.seed("BTC", closes, last_candle_epoch_hour=500_000)
        assert list(buf.prices["BTC"]) == closes
        assert buf.last_epoch_hour == 500_000

    def test_update_live_same_hour_overwrites(self) -> None:
        buf = HourlyPriceBuffer(coins=["BTC"], max_hours=100)
        buf.seed("BTC", [100.0, 101.0], last_candle_epoch_hour=500_000)

        buf.update_live("BTC", 102.0, epoch_hour=500_000)
        assert list(buf.prices["BTC"]) == [100.0, 102.0]

    def test_update_live_new_hour_appends(self) -> None:
        buf = HourlyPriceBuffer(coins=["BTC"], max_hours=100)
        buf.seed("BTC", [100.0, 101.0], last_candle_epoch_hour=500_000)

        buf.update_live("BTC", 102.0, epoch_hour=500_001)
        assert list(buf.prices["BTC"]) == [100.0, 101.0, 102.0]

    def test_advance_hour_detects_change(self) -> None:
        buf = HourlyPriceBuffer(coins=["BTC"], max_hours=100)
        buf.last_epoch_hour = 500_000

        assert buf.advance_hour(500_000) is False
        assert buf.advance_hour(500_001) is True
        assert buf.last_epoch_hour == 500_001

    def test_handles_24h_gap(self) -> None:
        """After >24h downtime, epoch_hour correctly identifies new bars."""
        buf = HourlyPriceBuffer(coins=["BTC"], max_hours=100)
        buf.seed("BTC", [100.0], last_candle_epoch_hour=500_000)

        # 25 hours later
        buf.update_live("BTC", 105.0, epoch_hour=500_025)
        assert len(buf.prices["BTC"]) == 2
        assert buf.prices["BTC"][-1] == 105.0

    def test_get_prices_returns_array(self) -> None:
        buf = HourlyPriceBuffer(coins=["BTC"], max_hours=100)
        buf.seed("BTC", [100.0, 101.0, 102.0], last_candle_epoch_hour=0)
        arr = buf.get_prices("BTC")
        assert isinstance(arr, np.ndarray)
        np.testing.assert_array_equal(arr, [100.0, 101.0, 102.0])

    def test_bar_count(self) -> None:
        buf = HourlyPriceBuffer(coins=["BTC", "ETH"], max_hours=100)
        buf.seed("BTC", [1.0, 2.0, 3.0], last_candle_epoch_hour=0)
        buf.seed("ETH", [10.0, 20.0, 30.0], last_candle_epoch_hour=0)
        assert buf.bar_count == 3

    def test_empty_buffer_update(self) -> None:
        buf = HourlyPriceBuffer(coins=["BTC"], max_hours=100)
        buf.update_live("BTC", 100.0, epoch_hour=500_000)
        assert list(buf.prices["BTC"]) == [100.0]

    def test_max_hours_limit(self) -> None:
        buf = HourlyPriceBuffer(coins=["BTC"], max_hours=5)
        buf.seed("BTC", [1.0, 2.0, 3.0, 4.0, 5.0], last_candle_epoch_hour=0)
        buf.update_live("BTC", 6.0, epoch_hour=1)
        buf.advance_hour(1)
        assert len(buf.prices["BTC"]) == 5
        assert list(buf.prices["BTC"]) == [2.0, 3.0, 4.0, 5.0, 6.0]
