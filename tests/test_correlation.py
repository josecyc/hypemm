"""Tests for rolling correlation computation."""

from __future__ import annotations

import numpy as np
import pytest

from hypemm.math.correlation import compute_correlation_single, rolling_correlation


class TestRollingCorrelation:
    def test_perfectly_correlated(self) -> None:
        """Identical series should have correlation 1.0."""
        returns = np.array([0.01, -0.02, 0.03, -0.01, 0.02] * 4)
        result = rolling_correlation(returns, returns, window=5)
        # After window warmup, correlation should be 1.0
        valid = result[~np.isnan(result)]
        np.testing.assert_array_almost_equal(valid, np.ones(len(valid)))

    def test_perfectly_anticorrelated(self) -> None:
        """Opposite series should have correlation -1.0."""
        a = np.array([0.01, -0.02, 0.03, -0.01, 0.02] * 4)
        b = -a
        result = rolling_correlation(a, b, window=5)
        valid = result[~np.isnan(result)]
        np.testing.assert_array_almost_equal(valid, -np.ones(len(valid)))

    def test_nan_before_window(self) -> None:
        a = np.random.default_rng(1).normal(0, 0.01, 20)
        b = np.random.default_rng(2).normal(0, 0.01, 20)
        result = rolling_correlation(a, b, window=10)
        assert all(np.isnan(result[:10]))
        assert not np.isnan(result[10])

    def test_output_length(self) -> None:
        n = 30
        a = np.random.default_rng(1).normal(0, 0.01, n)
        b = np.random.default_rng(2).normal(0, 0.01, n)
        result = rolling_correlation(a, b, window=10)
        assert len(result) == n


class TestComputeCorrelationSingle:
    def test_insufficient_data(self) -> None:
        a = np.array([0.01, 0.02])
        b = np.array([0.01, 0.02])
        result = compute_correlation_single(a, b, window=10)
        assert result is None

    def test_perfect_correlation(self) -> None:
        a = np.array([0.01, -0.02, 0.03, -0.01, 0.02, 0.01, -0.005, 0.015, -0.01, 0.02])
        result = compute_correlation_single(a, a, window=10)
        assert result is not None
        assert result == pytest.approx(1.0)

    def test_uses_trailing_window(self) -> None:
        """Single correlation should use the last `window` elements."""
        rng = np.random.default_rng(42)
        a = rng.normal(0, 0.01, 50)
        b = a + rng.normal(0, 0.003, 50)
        window = 20

        single = compute_correlation_single(a, b, window)
        manual = float(np.corrcoef(a[-window:], b[-window:])[0, 1])

        assert single is not None
        assert single == pytest.approx(manual, abs=1e-10)
