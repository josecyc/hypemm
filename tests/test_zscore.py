"""Tests for z-score computation."""

from __future__ import annotations

import numpy as np
import pytest

from hypemm.math import compute_log_ratios, compute_z_score_single, compute_z_scores


class TestComputeLogRatios:
    def test_equal_prices(self) -> None:
        a = np.array([100.0, 100.0])
        b = np.array([100.0, 100.0])
        result = compute_log_ratios(a, b)
        np.testing.assert_array_almost_equal(result, [0.0, 0.0])

    def test_known_ratio(self) -> None:
        a = np.array([100.0])
        b = np.array([50.0])
        result = compute_log_ratios(a, b)
        assert result[0] == pytest.approx(np.log(2.0))


class TestComputeZScores:
    def test_nan_before_lookback(self) -> None:
        ratios = np.zeros(20)
        z = compute_z_scores(ratios, lookback=10)
        assert all(np.isnan(z[:10]))

    def test_constant_ratios_are_nan(self) -> None:
        """Constant prices -> std=0 -> z-score undefined (NaN)."""
        ratios = np.ones(20)
        z = compute_z_scores(ratios, lookback=10)
        # After lookback, std=0 so z should remain NaN
        assert all(np.isnan(z[10:]))

    def test_known_z_score(self) -> None:
        """Build a series where the last value is exactly 2 std devs above mean."""
        rng = np.random.default_rng(42)
        lookback = 10
        window = rng.normal(0, 1, lookback)
        mean = np.mean(window)
        std = np.std(window, ddof=1)

        # Append a value that is exactly +2 std devs from the window mean
        target = mean + 2.0 * std
        ratios = np.concatenate([window, [target]])

        z = compute_z_scores(ratios, lookback=lookback)
        assert z[lookback] == pytest.approx(2.0)

    def test_output_length_matches_input(self) -> None:
        ratios = np.random.default_rng(1).normal(0, 1, 50)
        z = compute_z_scores(ratios, lookback=10)
        assert len(z) == 50


class TestComputeZScoreSingle:
    def test_insufficient_data(self) -> None:
        ratios = np.array([1.0, 2.0, 3.0])
        result = compute_z_score_single(ratios, lookback=10)
        assert result is None

    def test_matches_batch_computation(self) -> None:
        """Single z-score should match the last value from batch computation."""
        rng = np.random.default_rng(7)
        ratios = rng.normal(0, 0.01, 60)
        lookback = 48

        batch = compute_z_scores(ratios, lookback)
        single = compute_z_score_single(ratios, lookback)

        assert single is not None
        assert single == pytest.approx(batch[-1], abs=1e-10)

    def test_constant_returns_none(self) -> None:
        ratios = np.ones(20)
        result = compute_z_score_single(ratios, lookback=10)
        assert result is None
