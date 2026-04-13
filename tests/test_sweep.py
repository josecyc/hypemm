"""Tests for parameter sweep."""

from __future__ import annotations

import pandas as pd

from hypemm.analysis.sweep import run_parameter_sweep
from hypemm.config import StrategyConfig
from hypemm.models import PairConfig


class TestRunParameterSweep:
    def test_grid_size(self, sample_prices: pd.DataFrame, link_sol: PairConfig) -> None:
        config = StrategyConfig(pairs=(link_sol,))
        results = run_parameter_sweep(
            prices=sample_prices, base_config=config, lookbacks=[24, 48], entry_zs=[1.5, 2.0]
        )
        assert len(results) == 4

    def test_result_keys(self, sample_prices: pd.DataFrame, link_sol: PairConfig) -> None:
        config = StrategyConfig(pairs=(link_sol,))
        results = run_parameter_sweep(
            prices=sample_prices, base_config=config, lookbacks=[48], entry_zs=[2.0]
        )
        assert len(results) == 1
        r = results[0]
        assert r["lookback"] == 48
        assert r["entry_z"] == 2.0
        assert "trades" in r
        assert "win_rate" in r
        assert "sharpe" in r
        assert "max_dd" in r
