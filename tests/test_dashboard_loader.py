"""Tests for the file-backed dashboard snapshot loader."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from hypemm.config import (
    AppConfig,
    GateConfig,
    InfraConfig,
    RiskConfig,
    StrategyConfig,
    SweepConfig,
)
from hypemm.dashboard_loader import load_dashboard_snapshot
from hypemm.models import PairConfig
from hypemm.persistence import (
    SNAPSHOT_FIELDS,
    TRADE_FIELDS,
)


@pytest.fixture
def app(tmp_path: Path) -> AppConfig:
    """An AppConfig pointing at a fresh tmp data dir."""
    return AppConfig(
        strategy=StrategyConfig(
            pairs=(PairConfig("LINK", "SOL"), PairConfig("DOGE", "AVAX")),
        ),
        infra=InfraConfig(data_dir=tmp_path),
        gates=GateConfig(),
        sweep=SweepConfig(),
        risk=RiskConfig(),
    )


def _write_state(path: Path, positions: dict, cooldowns: dict, start_time: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "start_time": start_time,
        "engine": {"positions": positions, "cooldowns": cooldowns},
    }))


def _write_trades(path: Path, trades: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        writer.writeheader()
        writer.writerows(trades)


def _write_snapshot(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SNAPSHOT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _trade_row(pair: str, net: float, exit_ts: int) -> dict:
    return {
        "pair_label": pair, "direction": "long_ratio",
        "entry_ts": exit_ts - 5_000, "exit_ts": exit_ts,
        "entry_z": -2.5, "exit_z": -0.3, "hours_held": 5,
        "entry_price_a": 10.0, "entry_price_b": 100.0,
        "exit_price_a": 10.1, "exit_price_b": 99.5,
        "pnl_leg_a": net / 2, "pnl_leg_b": net / 2,
        "gross_pnl": net, "cost": 40.0, "net_pnl": net,
        "exit_reason": "mean_revert", "entry_correlation": 0.85,
        "funding_cost": 0.0, "max_adverse_excursion": 0.0,
    }


def test_load_empty_dir_returns_empty_snapshot(app: AppConfig) -> None:
    snap = load_dashboard_snapshot(app)
    assert snap.completed_trades == []
    assert all(p is None for p in snap.positions.values())
    assert snap.signals == {}
    assert snap.start_time == ""


def test_load_reconstructs_trades_from_csv(app: AppConfig) -> None:
    trades_path = app.infra.paper_trades_dir / "paper_trades.csv"
    _write_trades(trades_path, [
        _trade_row("LINK/SOL", 100.0, 1_700_000_000_000),
        _trade_row("DOGE/AVAX", -50.0, 1_700_000_500_000),
    ])
    snap = load_dashboard_snapshot(app)
    assert len(snap.completed_trades) == 2
    assert snap.completed_trades[0].pair_label == "LINK/SOL"
    assert snap.completed_trades[0].net_pnl == 100.0


def test_load_reconstructs_positions_and_start_time(app: AppConfig) -> None:
    state_path = app.infra.paper_trades_dir / "state.json"
    _write_state(
        state_path,
        positions={
            "LINK/SOL": {
                "coin_a": "LINK", "coin_b": "SOL",
                "direction": -1, "entry_z": 2.5,
                "entry_price_a": 10.0, "entry_price_b": 100.0,
                "entry_time_ms": 1_700_000_000_000,
                "entry_correlation": 0.85, "hours_held": 3,
                "funding_paid": 0.0,
            },
            "DOGE/AVAX": None,
        },
        cooldowns={"LINK/SOL": 0, "DOGE/AVAX": 1},
        start_time="2026-04-26T17:00:00+00:00",
    )
    snap = load_dashboard_snapshot(app)
    assert snap.start_time == "2026-04-26T17:00:00+00:00"
    assert snap.positions["LINK/SOL"] is not None
    assert snap.positions["LINK/SOL"].hours_held == 3
    assert snap.positions["DOGE/AVAX"] is None
    assert snap.cooldowns["DOGE/AVAX"] == 1


def test_load_reconstructs_signals_from_latest(app: AppConfig) -> None:
    latest_path = app.infra.paper_trades_dir / "latest_snapshot.csv"
    _write_snapshot(latest_path, [
        {
            "timestamp": "2026-04-27T01:00:00+00:00", "pair": "LINK/SOL",
            "z_score": 1.23, "correlation": 0.85,
            "price_a": 10.0, "price_b": 100.0, "n_bars": 250,
            "position": "", "hours_held": 0,
            "unrealized_pnl": 0.0, "cooldown_remaining": 0,
            "signal_status": "no_signal",
        },
        {
            "timestamp": "2026-04-27T01:00:00+00:00", "pair": "DOGE/AVAX",
            "z_score": -2.6, "correlation": 0.78,
            "price_a": 0.1, "price_b": 9.5, "n_bars": 250,
            "position": "", "hours_held": 0,
            "unrealized_pnl": 0.0, "cooldown_remaining": 0,
            "signal_status": "long_signal",
        },
    ])
    snap = load_dashboard_snapshot(app)
    assert "LINK/SOL" in snap.signals
    assert snap.signals["LINK/SOL"].z_score == 1.23
    assert snap.signals["DOGE/AVAX"].z_score == -2.6
    assert snap.n_bars == 250


def test_fresh_mode_ignores_state_and_trades(app: AppConfig) -> None:
    """--fresh should render an empty starting view even if files exist."""
    _write_trades(app.infra.paper_trades_dir / "paper_trades.csv", [
        _trade_row("LINK/SOL", 100.0, 1_700_000_000_000),
    ])
    _write_state(
        app.infra.paper_trades_dir / "state.json",
        positions={"LINK/SOL": None, "DOGE/AVAX": None},
        cooldowns={"LINK/SOL": 0, "DOGE/AVAX": 0},
        start_time="2026-04-26T17:00:00+00:00",
    )
    snap = load_dashboard_snapshot(app, fresh=True)
    assert snap.completed_trades == []
    assert snap.start_time == ""


def test_falls_back_to_hourly_snapshots_when_latest_absent(app: AppConfig) -> None:
    hourly_path = app.infra.paper_trades_dir / "hourly_snapshots.csv"
    # Write two hourly rows for LINK/SOL — should pick the most recent
    _write_snapshot(hourly_path, [
        {
            "timestamp": "2026-04-27T00:00:00+00:00", "pair": "LINK/SOL",
            "z_score": 0.5, "correlation": 0.8,
            "price_a": 10.0, "price_b": 100.0, "n_bars": 100,
            "position": "", "hours_held": 0,
            "unrealized_pnl": 0.0, "cooldown_remaining": 0,
            "signal_status": "no_signal",
        },
        {
            "timestamp": "2026-04-27T01:00:00+00:00", "pair": "LINK/SOL",
            "z_score": 1.5, "correlation": 0.8,
            "price_a": 10.0, "price_b": 100.0, "n_bars": 101,
            "position": "", "hours_held": 0,
            "unrealized_pnl": 0.0, "cooldown_remaining": 0,
            "signal_status": "no_signal",
        },
    ])
    snap = load_dashboard_snapshot(app)
    assert snap.signals["LINK/SOL"].z_score == 1.5
    assert snap.n_bars == 101


def test_live_mode_picked_up_from_mode_file(app: AppConfig) -> None:
    mode_path = app.infra.paper_trades_dir / "mode.txt"
    mode_path.parent.mkdir(parents=True, exist_ok=True)
    mode_path.write_text("LIVE")
    snap = load_dashboard_snapshot(app)
    assert snap.live_mode is True
