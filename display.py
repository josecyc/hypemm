"""Rich terminal UI for the edge monitor dashboard."""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from analysis import (
    CONTROL_PAIR,
    MARKET_PAIRS,
    check_alerts,
    edge_verdict,
    format_usd,
    is_cme_open,
    is_spot,
    is_us_market_open,
    trade_stats,
    volume_imbalance,
)

VERDICT_ICONS = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


def build_display(hl_feed, bn_feed) -> Panel:
    """Build the full dashboard renderable from current feed state."""
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    table = Table(
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        expand=True,
        pad_edge=True,
    )
    table.add_column("Market", style="bold white", min_width=22)
    table.add_column("HL Spread", justify="right")
    table.add_column("Ref Spread", justify="right")
    table.add_column("Ratio", justify="right")
    table.add_column("Edge(bps)", justify="right")
    table.add_column("Trades/5m", justify="right")
    table.add_column("Depth@10bps", justify="right")
    table.add_column("Verdict", justify="center", min_width=14)

    imbalances: list[tuple[str, float]] = []
    fundings: list[tuple[str, float]] = []

    def _add_row(pair: dict, control: bool = False) -> None:
        hl_coin = pair["hl"]
        hl_name = pair["hl_name"]
        bn_sym = pair.get("binance")

        hl_book = hl_feed.books.get(hl_coin)
        bn_book = bn_feed.books.get(bn_sym) if bn_sym else None

        hl_s = hl_book.spread_bps() if hl_book else None
        bn_s = bn_book.spread_bps() if bn_book else None

        ratio = (
            hl_s / bn_s
            if hl_s is not None and bn_s is not None and bn_s > 0
            else None
        )
        edge = (
            hl_s - bn_s
            if hl_s is not None and bn_s is not None
            else None
        )

        stats = trade_stats(hl_feed.trades, hl_coin)
        t5 = stats[300]["count"]
        d10 = hl_book.depth_at_bps(10) if hl_book else 0.0

        vtext, vcolor = edge_verdict(hl_s, bn_s, t5, d10)
        icon = VERDICT_ICONS.get(vcolor, "")

        bn_label = bn_sym.upper() if bn_sym else "—"
        label = f"{hl_name} / {bn_label}"

        # Mid price for context
        mid = hl_book.mid_price if hl_book else None
        mid_str = f" ${mid:,.2f}" if mid is not None else ""

        def _fmt_spread(s):
            if s is None:
                return "—"
            if s < 0.1:
                return f"{s:.2f} bps"
            return f"{s:.1f} bps"

        table.add_row(
            label,
            _fmt_spread(hl_s),
            _fmt_spread(bn_s),
            f"{ratio:.1f}x" if ratio is not None else "—",
            f"{edge:.1f} bps" if edge is not None else "—",
            str(t5),
            format_usd(d10) if d10 else "—",
            f"[{vcolor}]{icon} {vtext}[/{vcolor}]",
            style="dim" if control else None,
        )

        # Collect footer data
        imb = volume_imbalance(hl_feed.trades, hl_coin)
        imbalances.append((hl_name, imb))

        # Funding rate (perps only)
        ctx = hl_feed.asset_ctxs.get(hl_coin, {})
        if not is_spot(hl_coin):
            f_raw = ctx.get("funding")
            if f_raw is not None:
                try:
                    fundings.append((hl_name, float(f_raw)))
                except (ValueError, TypeError):
                    pass

    # RWA market rows
    for p in MARKET_PAIRS:
        _add_row(p)
    table.add_section()
    # Control row
    _add_row(CONTROL_PAIR, control=True)

    # ── Footer info ───────────────────────────────────────────────

    # Volume imbalance line
    imb_parts = []
    for name, imb in imbalances:
        warn = " ⚠️" if imb > 65 or imb < 35 else ""
        imb_parts.append(f"{name} {imb:.0f}% buy{warn}")
    imb_line = f"Volume imbalance (5m): {' │ '.join(imb_parts)}"

    # Funding rates (perps only)
    if fundings:
        fund_parts = [f"{name} {rate * 100:+.4f}%" for name, rate in fundings]
        fund_line = f"Funding rates: {' │ '.join(fund_parts)}"
    else:
        fund_line = "Funding rates: — (spot markets)"

    # Market hours
    cme = is_cme_open()
    us = is_us_market_open()
    hours_parts = []
    hours_parts.append(f"CME: {'open' if cme else '[bold]CLOSED[/bold]'}")
    hours_parts.append(f"US equities: {'open' if us else '[bold]CLOSED[/bold]'}")
    if not cme or not us:
        hours_parts.append("wider spreads expected")
    hours_line = f"Market hours: {' │ '.join(hours_parts)}"

    lines = [imb_line, fund_line, hours_line]

    # Alerts
    alerts = check_alerts(hl_feed)
    if alerts:
        lines.append("")
        lines.extend(alerts)

    info = Text.from_markup("\n".join(lines))

    return Panel(
        Group(table, Text(""), info),
        title="[bold cyan]Hyperliquid RWA — Market Making Edge Monitor[/bold cyan]",
        subtitle=f"[dim]Updated: {now}  │  HIP-3 spot pairs vs Binance[/dim]",
        border_style="blue",
        expand=True,
    )
