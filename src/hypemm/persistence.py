"""Engine state persistence and CSV trade logging."""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from hypemm.config import StrategyConfig
from hypemm.engine import StrategyEngine
from hypemm.math import compute_unrealized_pnl
from hypemm.models import (
    CompletedTrade,
    Direction,
    ExitReason,
    OpenPosition,
    Signal,
    StateCorruptionError,
)

logger = logging.getLogger(__name__)


# -- Engine state --


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


# -- Trade logging --


TRADE_FIELDS = [
    "pair_label",
    "direction",
    "entry_ts",
    "exit_ts",
    "entry_z",
    "exit_z",
    "hours_held",
    "entry_price_a",
    "entry_price_b",
    "exit_price_a",
    "exit_price_b",
    "pnl_leg_a",
    "pnl_leg_b",
    "gross_pnl",
    "cost",
    "net_pnl",
    "exit_reason",
    "entry_correlation",
    "funding_cost",
    "max_adverse_excursion",
]


def log_trade(trade: CompletedTrade, path: Path) -> None:
    """Append a completed trade to the CSV log."""
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    d = asdict(trade)
    d["direction"] = trade.direction.label
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow(d)


def load_trades(path: Path) -> list[CompletedTrade]:
    """Load completed trades from a CSV file."""
    if not path.exists():
        return []

    trades: list[CompletedTrade] = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            dir_str = row["direction"]
            direction = Direction.LONG_RATIO if dir_str == "long_ratio" else Direction.SHORT_RATIO
            trades.append(
                CompletedTrade(
                    pair_label=row["pair_label"],
                    direction=direction,
                    entry_ts=int(row["entry_ts"]),
                    exit_ts=int(row["exit_ts"]),
                    entry_z=float(row["entry_z"]),
                    exit_z=float(row["exit_z"]),
                    hours_held=int(row["hours_held"]),
                    entry_price_a=float(row["entry_price_a"]),
                    entry_price_b=float(row["entry_price_b"]),
                    exit_price_a=float(row["exit_price_a"]),
                    exit_price_b=float(row["exit_price_b"]),
                    pnl_leg_a=float(row["pnl_leg_a"]),
                    pnl_leg_b=float(row["pnl_leg_b"]),
                    gross_pnl=float(row["gross_pnl"]),
                    cost=float(row["cost"]),
                    net_pnl=float(row["net_pnl"]),
                    exit_reason=ExitReason(row["exit_reason"]),
                    entry_correlation=float(row["entry_correlation"]),
                    funding_cost=float(row.get("funding_cost", "0")),
                    max_adverse_excursion=float(row.get("max_adverse_excursion", "0")),
                )
            )
    return trades


# -- Hourly snapshots --


SNAPSHOT_FIELDS = [
    "timestamp",
    "pair",
    "z_score",
    "correlation",
    "price_a",
    "price_b",
    "n_bars",
    "position",
    "hours_held",
    "unrealized_pnl",
    "cooldown_remaining",
    "signal_status",
]


def _build_snapshot_rows(
    engine: StrategyEngine,
    signals: dict[str, Signal],
    config: StrategyConfig,
) -> list[dict[str, object]]:
    """One row per pair capturing the current signal + position state."""
    now = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, object]] = []
    for pair in config.pairs:
        label = pair.label
        sig = signals.get(label)
        pos = engine.positions.get(label)
        cooldown = engine.cooldowns.get(label, 0)
        z = sig.z_score if sig else None
        corr = sig.correlation if sig else None
        status = _signal_status(z, pos is not None, cooldown, corr, config)
        upnl = 0.0
        if pos and sig:
            upnl = compute_unrealized_pnl(pos, sig.price_a, sig.price_b, config.notional_per_leg)
        rows.append(
            {
                "timestamp": now,
                "pair": label,
                "z_score": round(z, 6) if z is not None else "",
                "correlation": round(corr, 6) if corr is not None else "",
                "price_a": sig.price_a if sig else "",
                "price_b": sig.price_b if sig else "",
                "n_bars": sig.n_bars if sig else "",
                "position": _pos_str(pos),
                "hours_held": pos.hours_held if pos else 0,
                "unrealized_pnl": round(upnl, 2),
                "cooldown_remaining": cooldown,
                "signal_status": status,
            }
        )
    return rows


def log_hourly_snapshot(
    engine: StrategyEngine,
    signals: dict[str, Signal],
    config: StrategyConfig,
    path: Path,
) -> None:
    """Append one row per pair to the hourly snapshot log."""
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    rows = _build_snapshot_rows(engine, signals, config)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SNAPSHOT_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def write_latest_snapshot(
    engine: StrategyEngine,
    signals: dict[str, Signal],
    config: StrategyConfig,
    path: Path,
) -> None:
    """Atomically overwrite a single-tick snapshot file used by the dashboard.

    Same row schema as log_hourly_snapshot, but contains only the current
    state (one row per pair). Written every tick; the dashboard process
    reads this for sub-hourly freshness without needing to share memory
    with the runner.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = _build_snapshot_rows(engine, signals, config)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SNAPSHOT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def _signal_status(
    z: float | None,
    in_position: bool,
    cooldown: int,
    corr: float | None,
    config: StrategyConfig,
) -> str:
    if z is None:
        return "warming_up"
    if in_position:
        return "in_position"
    if cooldown > 0:
        return "cooldown"
    if corr is not None and corr < config.corr_threshold:
        return "corr_blocked"
    if abs(z) > config.entry_z:
        return "signal_present"
    return "no_signal"


def _pos_str(pos: OpenPosition | None) -> str:
    if pos is None:
        return ""
    return pos.direction_str
