"""Tests for the backtest runner."""

from __future__ import annotations

import numpy as np
import pandas as pd

from hypemm.backtest import check_backtest_gate, run_backtest
from hypemm.config import GateConfig, StrategyConfig
from hypemm.models import BacktestResult, PairConfig


def test_backtest_produces_trades_on_divergent_data() -> None:
    """Synthetic data with clear divergence should produce at least one trade."""
    rng = np.random.default_rng(42)
    n = 300
    pair = PairConfig("A", "B")

    # Create prices that diverge and revert
    common = rng.normal(0, 0.005, n).cumsum()
    # Add a big divergence around bar 100, then reversion
    divergence = np.zeros(n)
    divergence[100:120] = np.linspace(0, 0.15, 20)
    divergence[120:150] = np.linspace(0.15, 0, 30)

    prices_a = 15.0 * np.exp(common + rng.normal(0, 0.002, n).cumsum() + divergence)
    prices_b = 150.0 * np.exp(common + rng.normal(0, 0.002, n).cumsum())

    timestamps = pd.date_range("2025-09-01", periods=n, freq="h", tz="UTC")
    df = pd.DataFrame({"A": prices_a, "B": prices_b}, index=timestamps)

    config = StrategyConfig(
        pairs=(pair,),
        lookback_hours=48,
        entry_z=2.0,
        corr_threshold=0.0,  # disable corr gate for synthetic data
    )
    trades = run_backtest(df, pair, config)
    assert len(trades) > 0


def test_backtest_returns_empty_for_short_data() -> None:
    pair = PairConfig("A", "B")
    timestamps = pd.date_range("2025-09-01", periods=10, freq="h", tz="UTC")
    df = pd.DataFrame({"A": np.ones(10), "B": np.ones(10)}, index=timestamps)

    config = StrategyConfig(pairs=(pair,), lookback_hours=48)
    trades = run_backtest(df, pair, config)
    assert len(trades) == 0


def test_backtest_trade_pnl_has_costs() -> None:
    """All trades should have costs deducted."""
    rng = np.random.default_rng(7)
    n = 300
    pair = PairConfig("A", "B")
    common = rng.normal(0, 0.005, n).cumsum()
    divergence = np.zeros(n)
    divergence[100:120] = np.linspace(0, 0.15, 20)
    divergence[120:150] = np.linspace(0.15, 0, 30)

    prices_a = 15.0 * np.exp(common + rng.normal(0, 0.002, n).cumsum() + divergence)
    prices_b = 150.0 * np.exp(common + rng.normal(0, 0.002, n).cumsum())

    timestamps = pd.date_range("2025-09-01", periods=n, freq="h", tz="UTC")
    df = pd.DataFrame({"A": prices_a, "B": prices_b}, index=timestamps)

    config = StrategyConfig(
        pairs=(pair,),
        lookback_hours=48,
        corr_threshold=0.0,
    )
    trades = run_backtest(df, pair, config)
    for trade in trades:
        assert trade.cost > 0
        assert trade.net_pnl < trade.gross_pnl


def test_check_backtest_gate_pass() -> None:
    result = BacktestResult(
        trades=[], total_net=0, win_rate=0, sharpe=1.5, max_drawdown=0, monthly=[]
    )
    gate = check_backtest_gate(result, GateConfig(min_sharpe=1.0))
    assert gate.passed is True
    assert gate.gate == "backtest"


def test_check_backtest_gate_fail() -> None:
    result = BacktestResult(
        trades=[], total_net=0, win_rate=0, sharpe=0.5, max_drawdown=0, monthly=[]
    )
    gate = check_backtest_gate(result, GateConfig(min_sharpe=1.0))
    assert gate.passed is False
