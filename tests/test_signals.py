"""Tests for signal computation."""

from __future__ import annotations

import numpy as np

from hypemm.config import StrategyConfig
from hypemm.models import PairConfig
from hypemm.strategy.signals import compute_pair_signal


class TestComputePairSignal:
    def test_returns_none_for_insufficient_data(self) -> None:
        config = StrategyConfig(lookback_hours=48)
        pair = PairConfig("A", "B")
        prices_a = np.ones(10)
        prices_b = np.ones(10)
        result = compute_pair_signal(prices_a, prices_b, config, pair, timestamp_ms=0)
        assert result is None

    def test_returns_signal_with_enough_data(self) -> None:
        rng = np.random.default_rng(42)
        n = 250
        common = rng.normal(0, 0.01, n).cumsum()
        prices_a = 15.0 * np.exp(common + rng.normal(0, 0.003, n).cumsum())
        prices_b = 150.0 * np.exp(common + rng.normal(0, 0.003, n).cumsum())

        config = StrategyConfig(lookback_hours=48, corr_window_hours=168)
        pair = PairConfig("LINK", "SOL")

        signal = compute_pair_signal(prices_a, prices_b, config, pair, timestamp_ms=1000)
        assert signal is not None
        assert signal.pair == pair
        assert isinstance(signal.z_score, float)
        assert signal.correlation is not None
        assert signal.n_bars == n

    def test_correlation_none_when_insufficient_history(self) -> None:
        """Enough for z-score but not for correlation."""
        rng = np.random.default_rng(42)
        n = 60  # Enough for lookback=48 but not corr_window=168
        prices_a = 15.0 * np.exp(rng.normal(0, 0.01, n).cumsum())
        prices_b = 150.0 * np.exp(rng.normal(0, 0.01, n).cumsum())

        config = StrategyConfig(lookback_hours=48, corr_window_hours=168)
        pair = PairConfig("A", "B")

        signal = compute_pair_signal(prices_a, prices_b, config, pair, timestamp_ms=0)
        assert signal is not None
        assert signal.correlation is None
