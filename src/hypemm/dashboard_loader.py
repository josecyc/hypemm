"""Load a DashboardSnapshot from on-disk runner artifacts.

The runner persists trades, engine state, and per-hour signal snapshots to
files in `infra.paper_trades_dir`. This module reconstructs a single
DashboardSnapshot from those files so the dashboard can render without
holding any in-memory connection to the runner. That decoupling means:

  - The dashboard process can restart independently of the runner.
  - Iteration on dashboard.py doesn't lose price-buffer warmup state.
  - History is recreated from `paper_trades.csv` on every refresh.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

from hypemm.config import AppConfig, RiskConfig, StrategyConfig
from hypemm.engine import StrategyEngine
from hypemm.models import CompletedTrade, OpenPosition, PairConfig, Signal
from hypemm.persistence import load_state, load_trades
from hypemm.risk import RiskReport, compute_risk_report

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DashboardSnapshot:
    """All state the dashboard needs to render one frame."""

    config: StrategyConfig
    risk_config: RiskConfig
    start_time: str
    completed_trades: list[CompletedTrade]
    positions: dict[str, OpenPosition | None]
    cooldowns: dict[str, int]
    signals: dict[str, Signal] = field(default_factory=dict)
    n_bars: int = 0
    last_snapshot_iso: str = ""
    risk_report: RiskReport | None = None
    poll_interval_sec: int = 60
    live_mode: bool = False


def load_dashboard_snapshot(app: AppConfig, *, fresh: bool = False) -> DashboardSnapshot:
    """Reconstruct a DashboardSnapshot from disk.

    fresh=True: ignore paper_trades.csv and state.json — render an empty
    starting view as if the runner just launched. Useful for confirming
    a clean cutover after wiping data.
    """
    paper_dir = app.infra.paper_trades_dir
    state_path = paper_dir / "state.json"
    trades_path = paper_dir / "paper_trades.csv"
    latest_path = paper_dir / "latest_snapshot.csv"
    hourly_path = paper_dir / "hourly_snapshots.csv"
    mode_path = paper_dir / "mode.txt"

    engine = StrategyEngine(app.strategy)
    start_time = ""
    completed_trades: list[CompletedTrade] = []

    if not fresh:
        if state_path.exists():
            try:
                start_time = load_state(engine, state_path)
            except Exception as e:
                logger.warning("State file unreadable, ignoring: %s", e)
        completed_trades = load_trades(trades_path) if trades_path.exists() else []

    # Prefer latest_snapshot.csv (refreshed every tick) over hourly_snapshots.csv
    # (appended hourly). Fall back if the runner is older or hasn't written yet.
    source = latest_path if latest_path.exists() else hourly_path
    signals, last_iso, n_bars = _load_latest_signals(source, app.strategy.pairs)

    live_mode = False
    if mode_path.exists():
        try:
            live_mode = mode_path.read_text().strip().upper() == "LIVE"
        except OSError:
            live_mode = False

    risk_report = compute_risk_report(
        engine,
        signals,
        completed_trades,
        app.risk,
        app.strategy.notional_per_leg,
    )
    engine.halt_entries = risk_report.halts_entry

    return DashboardSnapshot(
        config=app.strategy,
        risk_config=app.risk,
        start_time=start_time,
        completed_trades=completed_trades,
        positions=dict(engine.positions),
        cooldowns=dict(engine.cooldowns),
        signals=signals,
        n_bars=n_bars,
        last_snapshot_iso=last_iso,
        risk_report=risk_report,
        poll_interval_sec=app.infra.poll_interval_sec,
        live_mode=live_mode,
    )


def _load_latest_signals(
    path: Path, pairs: tuple[PairConfig, ...]
) -> tuple[dict[str, Signal], str, int]:
    """Reconstruct per-pair Signals from the most recent snapshot row per pair."""
    if not path.exists():
        return {}, "", 0

    latest: dict[str, dict[str, str]] = {}
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            label = row["pair"]
            latest[label] = row

    signals: dict[str, Signal] = {}
    last_iso = ""
    n_bars = 0
    pair_by_label = {p.label: p for p in pairs}
    for label, row in latest.items():
        pair = pair_by_label.get(label)
        if pair is None:
            continue
        try:
            z = float(row["z_score"]) if row["z_score"] else None
            corr = float(row["correlation"]) if row["correlation"] else None
            pa = float(row["price_a"]) if row["price_a"] else None
            pb = float(row["price_b"]) if row["price_b"] else None
            nb = int(row["n_bars"]) if row["n_bars"] else 0
        except (ValueError, KeyError):
            continue
        if z is None or pa is None or pb is None:
            continue
        try:
            ts_iso = row["timestamp"]
            ts_ms = int(_iso_to_ms(ts_iso))
        except (ValueError, KeyError):
            ts_ms = 0
            ts_iso = ""

        signals[label] = Signal(
            pair=pair,
            z_score=z,
            correlation=corr,
            price_a=pa,
            price_b=pb,
            timestamp_ms=ts_ms,
            n_bars=nb,
        )
        if ts_iso > last_iso:
            last_iso = ts_iso
        if nb > n_bars:
            n_bars = nb

    return signals, last_iso, n_bars


def _iso_to_ms(iso_str: str) -> int:
    """Parse an ISO 8601 datetime to UTC ms."""
    from datetime import datetime

    dt = datetime.fromisoformat(iso_str)
    return int(dt.timestamp() * 1000)
