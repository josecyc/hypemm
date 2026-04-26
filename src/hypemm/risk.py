"""Portfolio-level risk monitor: kill switches and strategy drift detection.

The monitor consumes engine state, current signals, and completed trade history
and emits a list of RiskSignal objects. Each signal is OK / WARN / HALT.

HALT signals block new entries (existing positions are managed by the engine's
own exit logic). WARN signals are flagged on the dashboard but don't gate.

Thresholds follow THESIS section 5.3.8:
- Concurrent unrealized -50% of backtest worst (-$10K) → warn
- Concurrent unrealized -100% of backtest worst (-$20K) → halt
- Daily realized loss -$5K → halt new entries for the rest of the UTC day
- Time-stop ratio > 30% on last 20 trades → warn (THESIS suggests reducing size)
- Win-rate < 55% on last 30 trades → warn
- Active pair correlation < 0.65 → warn (filter blocks new entries by design)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from hypemm.config import RiskConfig
from hypemm.engine import StrategyEngine
from hypemm.math import compute_unrealized_pnl
from hypemm.models import CompletedTrade, ExitReason, Signal


class RiskStatus(str, Enum):
    OK = "OK"
    WARN = "WARN"
    HALT = "HALT"


@dataclass(frozen=True)
class RiskSignal:
    """One risk indicator with its current status and threshold context."""

    name: str
    status: RiskStatus
    value: float
    threshold: float
    detail: str
    halts_entry: bool


@dataclass(frozen=True)
class RiskReport:
    """Aggregated risk state for one tick."""

    signals: tuple[RiskSignal, ...] = field(default_factory=tuple)

    @property
    def halts_entry(self) -> bool:
        return any(s.halts_entry for s in self.signals)

    @property
    def has_warning(self) -> bool:
        return any(s.status != RiskStatus.OK for s in self.signals)

    @property
    def worst_status(self) -> RiskStatus:
        if any(s.status == RiskStatus.HALT for s in self.signals):
            return RiskStatus.HALT
        if any(s.status == RiskStatus.WARN for s in self.signals):
            return RiskStatus.WARN
        return RiskStatus.OK


def compute_risk_report(
    engine: StrategyEngine,
    signals: dict[str, Signal],
    completed_trades: list[CompletedTrade],
    risk_config: RiskConfig,
    notional_per_leg: float,
    now_ms: int | None = None,
) -> RiskReport:
    """Build a RiskReport from current state."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    out: list[RiskSignal] = [
        _daily_pnl_signal(completed_trades, risk_config, now_ms),
        _concurrent_unrealized_signal(engine, signals, notional_per_leg, risk_config),
        _win_rate_signal(completed_trades, risk_config),
        _time_stop_signal(completed_trades, risk_config),
        _correlation_drift_signal(engine, signals, risk_config),
    ]
    return RiskReport(signals=tuple(out))


# -- Individual signal computations --


def _daily_pnl_signal(
    trades: list[CompletedTrade],
    cfg: RiskConfig,
    now_ms: int,
) -> RiskSignal:
    """Realized P&L over the last 24 hours."""
    cutoff_ms = now_ms - 24 * 3_600_000
    daily = sum(t.net_pnl for t in trades if t.exit_ts >= cutoff_ms)

    if daily <= cfg.daily_loss_halt:
        status = RiskStatus.HALT
        halts = True
        detail = f"24h realized ${daily:+,.0f} ≤ halt ${cfg.daily_loss_halt:+,.0f}"
    elif daily <= cfg.daily_loss_halt * 0.5:
        status = RiskStatus.WARN
        halts = False
        detail = f"24h realized ${daily:+,.0f} approaching halt"
    else:
        status = RiskStatus.OK
        halts = False
        detail = f"24h realized ${daily:+,.0f}"

    return RiskSignal(
        name="daily_pnl",
        status=status,
        value=daily,
        threshold=cfg.daily_loss_halt,
        detail=detail,
        halts_entry=halts,
    )


def _concurrent_unrealized_signal(
    engine: StrategyEngine,
    signals: dict[str, Signal],
    notional: float,
    cfg: RiskConfig,
) -> RiskSignal:
    """Mark-to-market unrealized P&L across all open positions."""
    total = 0.0
    for label, pos in engine.positions.items():
        if pos is None:
            continue
        sig = signals.get(label)
        if sig is None:
            continue
        total += compute_unrealized_pnl(pos, sig.price_a, sig.price_b, notional)

    if total <= cfg.unrealized_halt:
        status = RiskStatus.HALT
        halts = True
        detail = f"unrealized ${total:+,.0f} ≤ halt ${cfg.unrealized_halt:+,.0f}"
    elif total <= cfg.unrealized_warn:
        status = RiskStatus.WARN
        halts = False
        detail = f"unrealized ${total:+,.0f} ≤ warn ${cfg.unrealized_warn:+,.0f}"
    else:
        status = RiskStatus.OK
        halts = False
        detail = f"unrealized ${total:+,.0f}"

    return RiskSignal(
        name="concurrent_unrealized",
        status=status,
        value=total,
        threshold=cfg.unrealized_halt,
        detail=detail,
        halts_entry=halts,
    )


def _win_rate_signal(
    trades: list[CompletedTrade],
    cfg: RiskConfig,
) -> RiskSignal:
    """Rolling win-rate over the last N trades vs OOS expectation."""
    window = trades[-cfg.win_rate_window :]
    if len(window) < cfg.win_rate_min_trades:
        return RiskSignal(
            name="win_rate_drift",
            status=RiskStatus.OK,
            value=0.0,
            threshold=cfg.win_rate_warn,
            detail=f"{len(window)}/{cfg.win_rate_min_trades} trades — warming up",
            halts_entry=False,
        )

    wins = sum(1 for t in window if t.net_pnl > 0)
    wr = wins / len(window)

    if wr < cfg.win_rate_warn:
        status = RiskStatus.WARN
        detail = f"WR {wr:.0%} on last {len(window)} < warn {cfg.win_rate_warn:.0%}"
    else:
        status = RiskStatus.OK
        detail = f"WR {wr:.0%} on last {len(window)}"

    return RiskSignal(
        name="win_rate_drift",
        status=status,
        value=wr,
        threshold=cfg.win_rate_warn,
        detail=detail,
        halts_entry=False,
    )


def _time_stop_signal(
    trades: list[CompletedTrade],
    cfg: RiskConfig,
) -> RiskSignal:
    """Fraction of recent trades exiting via time_stop. THESIS: >30% = reduce size."""
    window = trades[-cfg.time_stop_window :]
    if len(window) < cfg.time_stop_min_trades:
        return RiskSignal(
            name="time_stop_drift",
            status=RiskStatus.OK,
            value=0.0,
            threshold=cfg.time_stop_warn_pct,
            detail=f"{len(window)}/{cfg.time_stop_min_trades} trades — warming up",
            halts_entry=False,
        )

    n_ts = sum(1 for t in window if t.exit_reason == ExitReason.TIME_STOP)
    pct = n_ts / len(window)

    if pct > cfg.time_stop_warn_pct:
        status = RiskStatus.WARN
        detail = f"time_stop {pct:.0%} on last {len(window)} > warn {cfg.time_stop_warn_pct:.0%}"
    else:
        status = RiskStatus.OK
        detail = f"time_stop {pct:.0%} on last {len(window)}"

    return RiskSignal(
        name="time_stop_drift",
        status=status,
        value=pct,
        threshold=cfg.time_stop_warn_pct,
        detail=detail,
        halts_entry=False,
    )


def _correlation_drift_signal(
    engine: StrategyEngine,
    signals: dict[str, Signal],
    cfg: RiskConfig,
) -> RiskSignal:
    """Warn if any active pair's correlation has fallen below the warn threshold.

    The corr filter only gates new entries; an active position whose corr
    collapsed mid-trade is the dangerous case (THESIS section 5.1).
    """
    breached: list[tuple[str, float]] = []
    for label, pos in engine.positions.items():
        if pos is None:
            continue
        sig = signals.get(label)
        if sig is None or sig.correlation is None:
            continue
        if sig.correlation < cfg.corr_warn_threshold:
            breached.append((label, sig.correlation))

    if breached:
        worst_pair, worst_corr = min(breached, key=lambda x: x[1])
        detail = (
            f"{worst_pair} corr {worst_corr:.2f} < "
            f"{cfg.corr_warn_threshold:.2f} ({len(breached)} pair(s))"
        )
        return RiskSignal(
            name="correlation_drift",
            status=RiskStatus.WARN,
            value=worst_corr,
            threshold=cfg.corr_warn_threshold,
            detail=detail,
            halts_entry=False,
        )

    return RiskSignal(
        name="correlation_drift",
        status=RiskStatus.OK,
        value=cfg.corr_warn_threshold,
        threshold=cfg.corr_warn_threshold,
        detail="all active pairs above threshold",
        halts_entry=False,
    )
