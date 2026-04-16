"""Rich terminal dashboard for paper trading."""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hypemm.config import StrategyConfig
from hypemm.engine import StrategyEngine
from hypemm.math import compute_unrealized_pnl
from hypemm.models import CompletedTrade, Direction, OpenPosition, Signal


def build_dashboard(
    engine: StrategyEngine,
    signals: dict[str, Signal],
    completed_trades: list[CompletedTrade],
    config: StrategyConfig,
    start_time: str,
) -> Panel:
    """Build the full paper trading dashboard."""
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    table = _build_signals_table(engine, signals, config)
    total_unrealized = _total_unrealized(engine, signals, config)
    parts: list[Table | Text] = [table, Text("")]

    if completed_trades:
        parts.append(_build_trades_table(completed_trades))
        parts.append(Text(""))

    parts.append(_build_summary(completed_trades, total_unrealized, config, start_time))

    return Panel(
        Group(*parts),
        title="[bold cyan]Stat Arb Paper Trading[/bold cyan]",
        subtitle=f"[dim]{now}[/dim]",
        border_style="cyan",
        expand=False,
    )


def _build_signals_table(
    engine: StrategyEngine,
    signals: dict[str, Signal],
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
        pos = engine.positions.get(label)
        z = sig.z_score if sig else None
        corr = sig.correlation if sig else None

        z_str = _format_z(z, config.entry_z, config.exit_z)
        corr_str = _format_corr(corr, config.corr_threshold)
        pos_str, hold_str, pnl_str = _format_position(pos, sig, config)
        signal_str = _format_signal(z, corr, pos, engine.cooldowns.get(label, 0), config)

        t.add_row(label, z_str, corr_str, pos_str, hold_str, pnl_str, signal_str)

    return t


def _build_trades_table(trades: list[CompletedTrade]) -> Table:
    """Build the completed trades history table."""
    t = Table(title="Completed Trades (last 10)", show_header=True, header_style="bold")
    t.add_column("Pair", width=12)
    t.add_column("Dir", justify="center", width=3)
    t.add_column("Hold", justify="right", width=5)
    t.add_column("Entry Z", justify="right", width=7)
    t.add_column("Net P&L", justify="right", width=10)
    t.add_column("Reason", width=12)

    for tr in trades[-10:]:
        d = "L" if tr.direction == Direction.LONG_RATIO else "S"
        nc = "green" if tr.net_pnl > 0 else "red"
        t.add_row(
            tr.pair_label,
            d,
            f"{tr.hours_held}h",
            f"{tr.entry_z:+.2f}",
            f"[{nc}]${tr.net_pnl:+,.0f}[/{nc}]",
            tr.exit_reason,
        )
    return t


def _build_summary(
    trades: list[CompletedTrade],
    total_unrealized: float,
    config: StrategyConfig,
    start_time: str,
) -> Text:
    """Build summary statistics text."""
    total_realized = sum(tr.net_pnl for tr in trades)
    total_pnl = total_realized + total_unrealized
    n = len(trades)
    wins = sum(1 for tr in trades if tr.net_pnl > 0)
    wr = f"{wins}/{n} ({wins / n * 100:.0f}%)" if n else "0/0"

    rc = "green" if total_realized >= 0 else "red"
    uc = "green" if total_unrealized >= 0 else "red"
    tc = "green" if total_pnl >= 0 else "red"

    lines = [
        f"Trades: {n}  WR: {wr}  "
        f"Realized: [{rc}]${total_realized:+,.0f}[/{rc}]  "
        f"Unrealized: [{uc}]${total_unrealized:+,.0f}[/{uc}]  "
        f"Total: [{tc} bold]${total_pnl:+,.0f}[/{tc} bold]",
        f"Notional/leg: ${config.notional_per_leg:,}  "
        f"Max pairs: {len(config.pairs)}  "
        f"[dim]Polling every {config.cooldown_hours}h cooldown[/dim]",
    ]
    return Text.from_markup("\n".join(lines))


def _total_unrealized(
    engine: StrategyEngine,
    signals: dict[str, Signal],
    config: StrategyConfig,
) -> float:
    total = 0.0
    for pair in config.pairs:
        pos = engine.positions.get(pair.label)
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
