"""Rich terminal dashboard renderer.

This module is pure rendering: takes a DashboardSnapshot and returns a Panel.
The snapshot is constructed by `dashboard_loader.load_dashboard_snapshot`
from on-disk runner artifacts, so the dashboard process is fully decoupled
from the runner process. Iterate on this file freely without restarting the
runner — `hypemm dashboard` re-reads disk every refresh.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hypemm.config import StrategyConfig
from hypemm.dashboard_loader import DashboardSnapshot
from hypemm.math import compute_unrealized_pnl
from hypemm.models import CompletedTrade, Direction, OpenPosition, Signal
from hypemm.risk import RiskReport, RiskStatus

_STATUS_COLOR = {
    RiskStatus.OK: "green",
    RiskStatus.WARN: "yellow",
    RiskStatus.HALT: "red",
}


def build_dashboard(snapshot: DashboardSnapshot) -> Panel:
    """Build the full paper trading dashboard from a snapshot."""
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    config = snapshot.config
    risk_report = snapshot.risk_report

    table = _build_signals_table(snapshot.signals, snapshot.positions, snapshot.cooldowns, config)
    total_unrealized = _total_unrealized(snapshot.positions, snapshot.signals, config)
    parts: list[Table | Text] = []

    if risk_report is not None and risk_report.halts_entry:
        parts.append(_build_halt_banner(risk_report))
        parts.append(Text(""))

    parts.append(table)
    parts.append(Text(""))

    if risk_report is not None:
        parts.append(_build_risk_panel(risk_report))
        parts.append(Text(""))

    if snapshot.completed_trades:
        parts.append(_build_trades_table(snapshot.completed_trades))
        parts.append(Text(""))

    parts.append(
        _build_summary(
            snapshot.positions,
            snapshot.completed_trades,
            total_unrealized,
            config,
            snapshot.start_time,
            poll_interval_sec=snapshot.poll_interval_sec,
            n_bars=snapshot.n_bars,
            last_snapshot_iso=snapshot.last_snapshot_iso,
        )
    )

    title_color = "red" if snapshot.live_mode else "cyan"
    title_label = "LIVE" if snapshot.live_mode else "Paper"
    border = "red" if (risk_report is not None and risk_report.halts_entry) else title_color

    return Panel(
        Group(*parts),
        title=f"[bold {title_color}]Stat Arb {title_label} Trading[/bold {title_color}]",
        subtitle=f"[dim]{now}[/dim]",
        border_style=border,
        expand=False,
    )


def _build_halt_banner(report: RiskReport) -> Text:
    """Big red banner shown above the signals table when entries are halted."""
    halts = [s for s in report.signals if s.halts_entry]
    detail = "; ".join(f"{s.name}: {s.detail}" for s in halts)
    return Text.from_markup(
        f"[white on red bold] !! ENTRIES HALTED !! [/white on red bold]  [red]{detail}[/red]"
    )


def _build_risk_panel(report: RiskReport) -> Table:
    """Per-signal risk dashboard panel."""
    t = Table(title="Risk Monitor", show_header=True, header_style="bold")
    t.add_column("Signal", width=22)
    t.add_column("Status", justify="center", width=8)
    t.add_column("Value", justify="right", width=14)
    t.add_column("Threshold", justify="right", width=14)
    t.add_column("Detail", overflow="fold")

    for s in report.signals:
        color = _STATUS_COLOR[s.status]
        status_str = f"[{color} bold]{s.status.value}[/{color} bold]"
        if s.halts_entry:
            status_str += " [red]⛔[/red]"
        t.add_row(
            s.name,
            status_str,
            _format_value(s.name, s.value),
            _format_value(s.name, s.threshold),
            s.detail,
        )
    return t


def _format_value(name: str, v: float) -> str:
    """Format a risk metric value based on signal type."""
    if name in {"win_rate_drift", "time_stop_drift"}:
        return f"{v:.0%}"
    if name == "correlation_drift":
        return f"{v:.2f}"
    return f"${v:+,.0f}"


def _build_signals_table(
    signals: dict[str, Signal],
    positions: dict[str, OpenPosition | None],
    cooldowns: dict[str, int],
    config: StrategyConfig,
) -> Table:
    """Build the signals/positions table."""
    t = Table(show_header=True, header_style="bold cyan")
    t.add_column("Pair", style="bold", width=12)
    t.add_column("Z-Score", justify="right", width=8)
    t.add_column("Corr", justify="right", width=7)
    t.add_column("Position", justify="center", width=9)
    t.add_column("Hold", justify="right", width=6)
    t.add_column("Unreal P&L", justify="right", width=11)
    t.add_column("Signal", justify="center", width=10)

    for pair in config.pairs:
        label = pair.label
        sig = signals.get(label)
        pos = positions.get(label)
        z = sig.z_score if sig else None
        corr = sig.correlation if sig else None

        z_str = _format_z(z, config.entry_z, config.exit_z)
        corr_str = _format_corr(corr, config.corr_threshold)
        pos_str, hold_str, pnl_str = _format_position(pos, sig, config)
        signal_str = _format_signal(z, corr, pos, cooldowns.get(label, 0), config)

        t.add_row(label, z_str, corr_str, pos_str, hold_str, pnl_str, signal_str)

    return t


def _build_trades_table(trades: list[CompletedTrade]) -> Table:
    """Build the completed trades history table (last 10)."""
    t = Table(title="Completed Trades", show_header=True, header_style="bold")
    t.add_column("Pair", width=12)
    t.add_column("Dir", justify="center", width=3)
    t.add_column("Entry", justify="right", width=7)
    t.add_column("Exit", justify="right", width=7)
    t.add_column("Hold", justify="right", width=5)
    t.add_column("Entry Z", justify="right", width=7)
    t.add_column("Net P&L", justify="right", width=10)
    t.add_column("Reason", width=12)

    for tr in trades[-10:]:
        d = "L" if tr.direction == Direction.LONG_RATIO else "S"
        nc = "green" if tr.net_pnl > 0 else "red"
        entry_hh = datetime.fromtimestamp(tr.entry_ts / 1000, tz=timezone.utc).strftime("%H:%M")
        exit_hh = datetime.fromtimestamp(tr.exit_ts / 1000, tz=timezone.utc).strftime("%H:%M")
        t.add_row(
            tr.pair_label,
            d,
            entry_hh,
            exit_hh,
            f"{tr.hours_held}h",
            f"{tr.entry_z:+.2f}",
            f"[{nc}]${tr.net_pnl:+,.0f}[/{nc}]",
            str(tr.exit_reason),
        )
    return t


def _build_summary(
    positions: dict[str, OpenPosition | None],
    trades: list[CompletedTrade],
    total_unrealized: float,
    config: StrategyConfig,
    start_time: str,
    poll_interval_sec: int,
    n_bars: int,
    last_snapshot_iso: str = "",
) -> Text:
    """Build summary statistics text — matches the legacy hype_mm dashboard layout."""
    total_realized = sum(tr.net_pnl for tr in trades)
    total_pnl = total_realized + total_unrealized
    n = len(trades)
    wins = sum(1 for tr in trades if tr.net_pnl > 0)
    wr = f"{wins}/{n} ({wins / n * 100:.0f}%)" if n else "0/0"

    rc = "green" if total_realized >= 0 else "red"
    uc = "green" if total_unrealized >= 0 else "red"
    tc = "green" if total_pnl >= 0 else "red"

    # Runtime / projections
    try:
        started = datetime.fromisoformat(start_time)
    except (TypeError, ValueError):
        started = datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)
    runtime_days = max((now - started).total_seconds() / 86400.0, 1e-9)

    daily_rate = total_realized / runtime_days if runtime_days > 0 else 0.0
    projected_annual = daily_rate * 365.0
    capital_5x = (config.notional_per_leg * 2 * len(config.pairs)) / 5.0
    apr_5x = (projected_annual / capital_5x) * 100.0 if capital_5x > 0 else 0.0
    drc = "green" if daily_rate >= 0 else "red"
    pac = "green" if projected_annual >= 0 else "red"

    open_positions = sum(1 for p in positions.values() if p is not None)
    max_pairs = len(config.pairs)
    exposure = open_positions * config.notional_per_leg * 2
    max_exposure = max_pairs * config.notional_per_leg * 2
    margin_5x = exposure / 5.0
    max_margin_5x = max_exposure / 5.0

    last_seen = (
        f"Last runner snapshot: {last_snapshot_iso[:19]}Z"
        if last_snapshot_iso
        else "Last runner snapshot: ---"
    )

    lines = [
        f"Trades: {n}  WR: {wr}  "
        f"Realized: [{rc}]${total_realized:+,.0f}[/{rc}]  "
        f"Unrealized: [{uc}]${total_unrealized:+,.0f}[/{uc}]  "
        f"Total: [{tc} bold]${total_pnl:+,.0f}[/{tc} bold]",
        f"Runtime: {runtime_days:.1f}d  "
        f"Daily rate: [{drc}]${daily_rate:+,.0f}/day[/{drc}]  "
        f"Projected annual: [{pac}]${projected_annual:+,.0f}[/{pac}]  "
        f"APR (5x): [{pac}]{apr_5x:+.0f}%[/{pac}]",
        f"Notional/leg: ${config.notional_per_leg:,.0f}  "
        f"Open: {open_positions}/{max_pairs} pairs  "
        f"Exposure: ${exposure:,.0f} / ${max_exposure:,.0f}  "
        f"Margin (5x): ${margin_5x:,.0f} / ${max_margin_5x:,.0f}",
        f"[dim]Bars: {n_bars} │ {last_seen} │ "
        f"Polling every {poll_interval_sec}s[/dim]",
    ]
    return Text.from_markup("\n".join(lines))


def _total_unrealized(
    positions: dict[str, OpenPosition | None],
    signals: dict[str, Signal],
    config: StrategyConfig,
) -> float:
    total = 0.0
    for pair in config.pairs:
        pos = positions.get(pair.label)
        sig = signals.get(pair.label)
        if pos and sig:
            total += compute_unrealized_pnl(pos, sig.price_a, sig.price_b, config.notional_per_leg)
    return total


def _format_z(z: float | None, entry_z: float, exit_z: float) -> str:
    if z is None:
        return "[dim]---[/dim]"
    if abs(z) > entry_z:
        return f"[bold yellow]{z:+.2f}[/bold yellow]"
    if abs(z) < exit_z:
        return f"[dim]{z:+.2f}[/dim]"
    return f"{z:+.2f}"


def _format_corr(corr: float | None, threshold: float) -> str:
    if corr is None:
        return "[dim]warm[/dim]"
    if corr < threshold:
        return f"[red]{corr:.3f}[/red]"
    return f"{corr:.3f}"


def _format_position(
    pos: OpenPosition | None,
    sig: Signal | None,
    config: StrategyConfig,
) -> tuple[str, str, str]:
    if pos is None:
        return ("[dim]---[/dim]", "[dim]---[/dim]", "[dim]---[/dim]")

    if pos.direction == Direction.LONG_RATIO:
        pos_str = "[cyan]LONG[/cyan]"
    else:
        pos_str = "[magenta]SHORT[/magenta]"
    hold_str = f"{pos.hours_held}h"

    if sig:
        upnl = compute_unrealized_pnl(pos, sig.price_a, sig.price_b, config.notional_per_leg)
        c = "green" if upnl > 0 else "red"
        pnl_str = f"[{c}]${upnl:+,.0f}[/{c}]"
    else:
        pnl_str = "[dim]---[/dim]"

    return pos_str, hold_str, pnl_str


def _format_signal(
    z: float | None,
    corr: float | None,
    pos: OpenPosition | None,
    cooldown: int,
    config: StrategyConfig,
) -> str:
    if pos is not None:
        return "[dim]in pos[/dim]"
    if cooldown > 0:
        return f"[dim]cool {cooldown}h[/dim]"
    if z is None or corr is None:
        return "[dim]---[/dim]"
    if corr < config.corr_threshold:
        return "[red]blocked[/red]"
    if z > config.entry_z:
        return "[yellow]SHORT?[/yellow]"
    if z < -config.entry_z:
        return "[yellow]LONG?[/yellow]"
    return "[dim]flat[/dim]"
