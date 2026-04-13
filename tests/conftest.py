"""Shared test fixtures."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hypemm.config import StrategyConfig
from hypemm.models import PairConfig


@pytest.fixture
def default_config() -> StrategyConfig:
    return StrategyConfig(pairs=(PairConfig("LINK", "SOL"),))


@pytest.fixture
def link_sol() -> PairConfig:
    return PairConfig("LINK", "SOL")


@pytest.fixture
def sample_prices() -> pd.DataFrame:
    """200 bars of synthetic correlated prices. Seeded for reproducibility."""
    rng = np.random.default_rng(42)
    n = 200

    common = rng.normal(0, 0.01, n).cumsum()
    noise_a = rng.normal(0, 0.003, n).cumsum()
    noise_b = rng.normal(0, 0.003, n).cumsum()

    prices_a = 15.0 * np.exp(common + noise_a)
    prices_b = 150.0 * np.exp(common + noise_b)

    timestamps = pd.date_range("2025-09-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame({"LINK": prices_a, "SOL": prices_b}, index=timestamps)


@pytest.fixture
def diverging_prices() -> pd.DataFrame:
    """200 bars where prices diverge (correlation breakdown)."""
    rng = np.random.default_rng(99)
    n = 200

    prices_a = 15.0 * np.exp(rng.normal(0.001, 0.01, n).cumsum())
    prices_b = 150.0 * np.exp(rng.normal(-0.001, 0.01, n).cumsum())

    timestamps = pd.date_range("2025-09-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame({"LINK": prices_a, "SOL": prices_b}, index=timestamps)
