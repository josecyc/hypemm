"""Tests for trade CSV logging."""

from __future__ import annotations

from pathlib import Path

from hypemm.models import CompletedTrade, Direction, ExitReason
from hypemm.persistence.trade_log import load_trades, log_trade


def _make_trade(pair_label: str = "LINK/SOL", net_pnl: float = 500.0) -> CompletedTrade:
    return CompletedTrade(
        pair_label=pair_label,
        direction=Direction.LONG_RATIO,
        entry_ts=1000,
        exit_ts=2000,
        entry_z=-2.5,
        exit_z=-0.3,
        hours_held=10,
        entry_price_a=15.0,
        entry_price_b=150.0,
        exit_price_a=16.0,
        exit_price_b=149.0,
        pnl_leg_a=300.0,
        pnl_leg_b=200.0,
        gross_pnl=540.0,
        cost=40.0,
        net_pnl=net_pnl,
        exit_reason=ExitReason.MEAN_REVERT,
        entry_correlation=0.85,
    )


def test_log_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "trades.csv"
    trade = _make_trade()
    log_trade(trade, path)

    loaded = load_trades(path)
    assert len(loaded) == 1
    assert loaded[0].pair_label == "LINK/SOL"
    assert loaded[0].net_pnl == 500.0
    assert loaded[0].exit_reason == ExitReason.MEAN_REVERT


def test_append_multiple_trades(tmp_path: Path) -> None:
    path = tmp_path / "trades.csv"
    log_trade(_make_trade(net_pnl=100.0), path)
    log_trade(_make_trade(net_pnl=200.0), path)

    loaded = load_trades(path)
    assert len(loaded) == 2
    assert loaded[0].net_pnl == 100.0
    assert loaded[1].net_pnl == 200.0


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    loaded = load_trades(tmp_path / "nonexistent.csv")
    assert loaded == []
