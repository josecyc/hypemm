"""Tests for L2 book walking + per-pair slippage in backtest."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hypemm.backtest import load_slippage_profile, run_backtest_all_pairs
from hypemm.config import StrategyConfig
from hypemm.models import PairConfig
from hypemm.orderbook import InsufficientDepthError, walk_book


def _book(bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> dict:
    return {
        "levels": [
            [{"px": p, "sz": s} for p, s in bids],
            [{"px": p, "sz": s} for p, s in asks],
        ]
    }


def test_walk_book_single_level_fill():
    book = _book(bids=[("100.00", "10")], asks=[("100.10", "10")])
    fill = walk_book(book, is_buy=True, notional=500.0)
    assert fill.vwap == pytest.approx(100.10)
    assert fill.mid == pytest.approx(100.05)
    assert fill.slippage_bps == pytest.approx((100.10 - 100.05) / 100.05 * 10_000)
    assert fill.levels_consumed == 1


def test_walk_book_sweeps_multiple_levels():
    book = _book(
        bids=[("100.00", "10")],
        asks=[("100.10", "1"), ("100.20", "5"), ("100.30", "10")],
    )
    fill = walk_book(book, is_buy=True, notional=500.0)
    assert fill.levels_consumed == 2
    assert 100.10 < fill.vwap < 100.20
    assert fill.slippage_bps > 0


def test_walk_book_buy_uses_asks():
    book = _book(bids=[("99.00", "100")], asks=[("101.00", "100")])
    fill = walk_book(book, is_buy=True, notional=500.0)
    assert fill.vwap == pytest.approx(101.0)


def test_walk_book_sell_uses_bids():
    book = _book(bids=[("99.00", "100")], asks=[("101.00", "100")])
    fill = walk_book(book, is_buy=False, notional=500.0)
    assert fill.vwap == pytest.approx(99.0)


def test_walk_book_sell_slippage_is_positive():
    """Slippage convention: positive = worse than mid, regardless of direction."""
    book = _book(bids=[("99.95", "100")], asks=[("100.05", "100")])
    fill = walk_book(book, is_buy=False, notional=500.0)
    assert fill.slippage_bps == pytest.approx(5.0, abs=0.01)


def test_walk_book_raises_on_insufficient_depth():
    book = _book(bids=[("100.00", "1")], asks=[("100.10", "1")])
    with pytest.raises(InsufficientDepthError):
        walk_book(book, is_buy=True, notional=10_000.0)


def test_walk_book_raises_on_empty_side():
    book = _book(bids=[], asks=[("100.0", "10")])
    with pytest.raises(InsufficientDepthError):
        walk_book(book, is_buy=False, notional=100.0)


# -- slippage profile loading + backtest integration -----------------------


def test_load_slippage_profile_returns_none_when_missing(tmp_path: Path):
    assert load_slippage_profile(tmp_path / "missing.json") is None


def test_load_slippage_profile_picks_percentile(tmp_path: Path):
    p = tmp_path / "slip.json"
    p.write_text(
        json.dumps({
            "pairs": {
                "BTC": {"median_bps": 0.5, "p90_bps": 1.5, "max_bps": 3.0},
                "AVAX": {"median_bps": 3.0, "p90_bps": 5.0, "max_bps": 12.0},
            }
        })
    )
    median = load_slippage_profile(p, percentile="median_bps")
    assert median == {"BTC": 0.5, "AVAX": 3.0}
    p90 = load_slippage_profile(p, percentile="p90_bps")
    assert p90 == {"BTC": 1.5, "AVAX": 5.0}


def test_backtest_applies_per_pair_slippage(tmp_path: Path):
    """Backtest with a slippage profile deducts more cost than without."""
    rng = np.random.default_rng(7)
    n = 500
    common = rng.normal(0, 0.005, n).cumsum()
    a = 10.0 * np.exp(common + rng.normal(0, 0.002, n).cumsum())
    b = 100.0 * np.exp(common + rng.normal(0, 0.002, n).cumsum())
    timestamps = pd.date_range("2025-09-01", periods=n, freq="h", tz="UTC")
    prices = pd.DataFrame({"X": a, "Y": b}, index=timestamps)
    cfg = StrategyConfig(
        pairs=(PairConfig("X", "Y"),),
        entry_z=1.5,
        notional_per_leg=50_000,
        cost_per_side_bps=2.0,
        slippage_per_side_bps=0.0,
    )

    no_slip = run_backtest_all_pairs(prices, cfg)
    with_slip = run_backtest_all_pairs(
        prices, cfg, slippage_profile={"X": 5.0, "Y": 5.0}
    )

    assert len(no_slip) == len(with_slip)
    # Per-trade slippage cost = 2 * 50_000 * (5+5) / 10000 = $100
    expected_delta_per_trade = 2 * 50_000 * (5.0 + 5.0) / 10_000
    no_total = sum(t.net_pnl for t in no_slip)
    with_total = sum(t.net_pnl for t in with_slip)
    assert no_total - with_total == pytest.approx(
        expected_delta_per_trade * len(no_slip)
    )
