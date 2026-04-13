"""Tests for correlation stability analysis."""

from __future__ import annotations

import numpy as np
import pandas as pd

from hypemm.validate import (
    compute_returns,
    correlation_regimes,
    find_breakdowns,
)


class TestComputeReturns:
    def test_log_returns_shape(self) -> None:
        prices = pd.DataFrame({"A": [100.0, 110.0, 121.0], "B": [50.0, 55.0, 60.5]})
        returns = compute_returns(prices)
        assert len(returns) == 2

    def test_log_returns_values(self) -> None:
        prices = pd.DataFrame({"A": [100.0, 200.0]})
        returns = compute_returns(prices)
        assert abs(returns["A"].iloc[0] - np.log(2)) < 1e-10


class TestCorrelationRegimes:
    def test_all_high(self) -> None:
        corr = np.array([0.9, 0.85, 0.95, 0.88])
        result = correlation_regimes(corr, high_threshold=0.7)
        assert result["high_pct"] == 100.0
        assert result["low_pct"] == 0.0

    def test_all_low(self) -> None:
        corr = np.array([0.1, 0.2, 0.3, 0.4])
        result = correlation_regimes(corr, high_threshold=0.7)
        assert result["low_pct"] == 100.0
        assert result["high_pct"] == 0.0

    def test_mixed_regimes(self) -> None:
        corr = np.array([0.9, 0.6, 0.3, 0.8])
        result = correlation_regimes(corr, high_threshold=0.7)
        assert result["high_pct"] == 50.0
        assert result["low_pct"] == 25.0
        assert result["med_pct"] == 25.0

    def test_empty_array(self) -> None:
        result = correlation_regimes(np.array([]), high_threshold=0.7)
        assert result["high_pct"] == 0

    def test_nan_values_excluded(self) -> None:
        corr = np.array([0.9, np.nan, 0.8, np.nan])
        result = correlation_regimes(corr, high_threshold=0.7)
        assert result["high_pct"] == 100.0


class TestFindBreakdowns:
    def test_detects_low_period(self) -> None:
        corr = np.array([0.8, 0.8, 0.3, 0.2, 0.4, 0.8, 0.9])
        timestamps = pd.date_range("2025-01-01", periods=7, freq="h", tz="UTC")
        bds = find_breakdowns(corr, timestamps, threshold=0.5)
        assert len(bds) == 1
        assert bds[0]["duration_hours"] == 3

    def test_no_breakdowns(self) -> None:
        corr = np.array([0.8, 0.9, 0.85, 0.75])
        timestamps = pd.date_range("2025-01-01", periods=4, freq="h", tz="UTC")
        bds = find_breakdowns(corr, timestamps, threshold=0.5)
        assert len(bds) == 0

    def test_breakdown_at_end(self) -> None:
        corr = np.array([0.8, 0.8, 0.3, 0.2])
        timestamps = pd.date_range("2025-01-01", periods=4, freq="h", tz="UTC")
        bds = find_breakdowns(corr, timestamps, threshold=0.5)
        assert len(bds) == 1
        assert bds[0]["duration_hours"] == 2

    def test_multiple_breakdowns(self) -> None:
        corr = np.array([0.8, 0.3, 0.8, 0.2, 0.8])
        timestamps = pd.date_range("2025-01-01", periods=5, freq="h", tz="UTC")
        bds = find_breakdowns(corr, timestamps, threshold=0.5)
        assert len(bds) == 2
