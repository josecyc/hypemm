"""Tests for engine state persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from hypemm.config import StrategyConfig
from hypemm.models import Direction, PairConfig, Signal, StateCorruptionError
from hypemm.persistence.state import load_state, save_state
from hypemm.strategy.engine import StrategyEngine


def _make_signal(pair: PairConfig, z: float, corr: float = 0.9) -> Signal:
    return Signal(
        pair=pair,
        z_score=z,
        correlation=corr,
        price_a=15.0,
        price_b=150.0,
        timestamp_ms=0,
        n_bars=100,
    )


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    pair = PairConfig("LINK", "SOL")
    config = StrategyConfig(pairs=(pair,))
    engine = StrategyEngine(config)

    # Enter a position
    sig = _make_signal(pair, z=-2.5)
    orders = engine.process_bar({pair.label: sig}, timestamp_ms=1000)
    engine.confirm_entry(orders[0], 15.0, 150.0, 1000)  # type: ignore[arg-type]

    # Save
    state_path = tmp_path / "state.json"
    save_state(engine, state_path, start_time="2026-04-01T00:00:00")

    # Load into new engine
    engine2 = StrategyEngine(config)
    start_time = load_state(engine2, state_path)

    assert start_time == "2026-04-01T00:00:00"
    pos = engine2.positions[pair.label]
    assert pos is not None
    assert pos.direction == Direction.LONG_RATIO
    assert pos.entry_price_a == 15.0


def test_load_missing_file_raises(tmp_path: Path) -> None:
    config = StrategyConfig()
    engine = StrategyEngine(config)
    with pytest.raises(FileNotFoundError):
        load_state(engine, tmp_path / "nonexistent.json")


def test_load_corrupt_file_raises(tmp_path: Path) -> None:
    config = StrategyConfig()
    engine = StrategyEngine(config)
    path = tmp_path / "bad.json"
    path.write_text("not json{{{")
    with pytest.raises(StateCorruptionError):
        load_state(engine, path)
