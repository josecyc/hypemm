"""Engine state persistence: save/load to JSON."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from hypemm.models import StateCorruptionError
from hypemm.strategy.engine import StrategyEngine

logger = logging.getLogger(__name__)


def save_state(engine: StrategyEngine, path: Path, start_time: str = "") -> None:
    """Persist engine state to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "start_time": start_time,
        "engine": engine.get_state(),
    }
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
    logger.info("State saved to %s", path)


def load_state(engine: StrategyEngine, path: Path) -> str:
    """Restore engine state from a JSON file.

    Returns the start_time string from the saved state.
    Raises StateCorruptionError if the file is corrupt.
    """
    if not path.exists():
        raise FileNotFoundError(f"State file not found: {path}")

    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise StateCorruptionError(f"Corrupt state file: {e}")

    engine_state = data.get("engine")
    if not isinstance(engine_state, dict):
        raise StateCorruptionError("Missing 'engine' key in state file")

    engine.load_state(engine_state)
    start_time = str(data.get("start_time", ""))

    n_pos = sum(1 for p in engine.positions.values() if p is not None)
    logger.info("State restored: %d open positions", n_pos)
    return start_time
