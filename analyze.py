#!/usr/bin/env python3
"""Analyze collected edge_log CSV data to determine MM viability.

Reads edge_log_*.csv files and produces a structured report answering:
1. Are spreads consistently wide enough to capture?
2. Is there enough trade flow to get filled?
3. What's the adverse selection signal (mid-price movement after trades)?
4. What's the estimated daily P&L for a simple MM strategy?
5. When is the edge strongest (time-of-day, market hours)?

Also connects live to estimate adverse selection from recent trade data.

Usage:
    python analyze.py              # analyze CSV logs + live snapshot
    python analyze.py --live-only  # skip CSV, just do live analysis
"""

from __future__ import annotations

import asyncio
import csv
import glob
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from analysis import MARKET_PAIRS, CONTROL_PAIR, format_usd, is_cme_open, is_us_market_open
from feeds.hyperliquid import HyperliquidFeed, Trade

console = Console()

# Hyperliquid spot fee schedule (as of 2026)
# Maker: depends on volume tier, assume worst case
MAKER_FEE_BPS = 1.0   # 0.01% = 1 bps (typical HIP-3 spot maker fee)
TAKER_FEE_BPS = 3.0   # 0.03% = 3 bps (typical taker fee)

# Assumed MM parameters
MM_QUOTE_OFFSET_BPS = 1.0  # quote 1 bps inside the current spread
HEDGE_COST_BPS = 1.0       # cost to hedge on CEX (Binance spread + fee)


# ── CSV Analysis ──────────────────────────────────────────────────────

def load_csv_logs() -> list[dict]:
    """Load all edge_log_*.csv files into a list of row dicts."""
    files = sorted(glob.glob("edge_log_*.csv"))
    if not files:
        return []

    rows = []
    for f in files:
        with open(f) as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                rows.append(row)
    return rows


def analyze_csv(rows: list[dict]) -> dict:
    """Analyze CSV data and return summary stats per market."""
    if not rows:
        return {}

    by_market: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_market[row["market"]].append(row)

    results = {}
    for market, mrows in by_market.items():
        spreads = []
        ref_spreads = []
        ratios = []
        trades_5m = []
        depths = []
        imbalances = []

        for r in mrows:
            if r.get("hl_spread_bps") and r["hl_spread_bps"] != "None":
                spreads.append(float(r["hl_spread_bps"]))
            if r.get("ref_spread_bps") and r["ref_spread_bps"] != "None":
                ref_spreads.append(float(r["ref_spread_bps"]))
            if r.get("spread_ratio") and r["spread_ratio"] != "None":
                ratios.append(float(r["spread_ratio"]))
            if r.get("trades_5m") and r["trades_5m"] != "None":
                trades_5m.append(int(r["trades_5m"]))
            if r.get("depth_10bps") and r["depth_10bps"] != "None":
                depths.append(float(r["depth_10bps"]))
            if r.get("volume_imbalance_pct") and r["volume_imbalance_pct"] != "None":
                imbalances.append(float(r["volume_imbalance_pct"]))

        results[market] = {
            "samples": len(mrows),
            "spread_mean": _mean(spreads),
            "spread_median": _median(spreads),
            "spread_p25": _percentile(spreads, 25),
            "spread_p75": _percentile(spreads, 75),
            "ref_spread_mean": _mean(ref_spreads),
            "ratio_mean": _mean(ratios),
            "trades_5m_mean": _mean(trades_5m),
            "trades_5m_median": _median(trades_5m),
            "depth_10bps_mean": _mean(depths),
            "imbalance_mean": _mean(imbalances),
            "imbalance_max": max(imbalances) if imbalances else None,
        }

    return results


# ── Live Adverse Selection Analysis ───────────────────────────────────

async def measure_adverse_selection(
    coins: list[str],
    duration_sec: int = 120,
) -> dict[str, dict]:
    """
    Connect live for `duration_sec` seconds and measure adverse selection.

    For each trade, record the mid-price at trade time and 5/15/30 seconds
    after. The mid-price move against the maker is the adverse selection cost.

    Returns per-coin stats: avg adverse selection at 5s/15s/30s in bps.
    """
    hl = HyperliquidFeed(coins)

    console.print(f"[cyan]Connecting live for {duration_sec}s to measure adverse selection...[/cyan]")
    await hl.fetch_meta()
    await hl.fetch_initial_books()

    ws_task = asyncio.create_task(hl.connect_ws())

    # Wait for WS to stabilize
    await asyncio.sleep(2)

    # Record trades with their mid-price at fill time
    trade_records: dict[str, list[dict]] = defaultdict(list)

    start = time.time()
    while time.time() - start < duration_sec:
        # Snapshot current trades and mids
        for coin in coins:
            book = hl.books.get(coin)
            mid = book.mid_price if book else None
            if mid is None:
                continue

            trades = hl.trades.get(coin, [])
            now_ms = int(time.time() * 1000)

            for t in trades:
                # Only record recent trades we haven't seen
                age_ms = now_ms - t.timestamp
                if age_ms < 1000:  # trade in the last second
                    trade_records[coin].append({
                        "trade": t,
                        "mid_at_fill": mid,
                        "fill_time": time.time(),
                        "mids_after": {},
                    })

        await asyncio.sleep(0.5)

        # Update post-fill mids for recorded trades
        for coin in coins:
            book = hl.books.get(coin)
            mid = book.mid_price if book else None
            if mid is None:
                continue

            now = time.time()
            for rec in trade_records.get(coin, []):
                elapsed = now - rec["fill_time"]
                for delay in [5, 15, 30]:
                    if delay not in rec["mids_after"] and elapsed >= delay:
                        rec["mids_after"][delay] = mid

    # Analyze results
    hl.stop()
    ws_task.cancel()
    try:
        await ws_task
    except asyncio.CancelledError:
        pass

    results = {}
    for coin in coins:
        records = trade_records.get(coin, [])
        if not records:
            results[coin] = {"n_trades": 0}
            continue

        # Deduplicate by trade timestamp
        seen = set()
        unique = []
        for r in records:
            key = (r["trade"].timestamp, r["trade"].price, r["trade"].size)
            if key not in seen:
                seen.add(key)
                unique.append(r)

        adverse_5s = []
        adverse_15s = []
        adverse_30s = []

        for rec in unique:
            mid_at_fill = rec["mid_at_fill"]
            if mid_at_fill <= 0:
                continue
            is_buy = rec["trade"].is_buy

            for delay, bucket in [(5, adverse_5s), (15, adverse_15s), (30, adverse_30s)]:
                mid_after = rec["mids_after"].get(delay)
                if mid_after is None:
                    continue
                # Adverse selection = how much mid moved against the maker
                # If taker bought (is_buy=True), mid should move UP = adverse for maker who sold
                # Adverse = (mid_after - mid_at_fill) / mid_at_fill * 10000 for buys
                # Adverse = (mid_at_fill - mid_after) / mid_at_fill * 10000 for sells
                if is_buy:
                    move_bps = (mid_after - mid_at_fill) / mid_at_fill * 10_000
                else:
                    move_bps = (mid_at_fill - mid_after) / mid_at_fill * 10_000
                bucket.append(move_bps)

        results[coin] = {
            "n_trades": len(unique),
            "adverse_5s_bps": _mean(adverse_5s),
            "adverse_15s_bps": _mean(adverse_15s),
            "adverse_30s_bps": _mean(adverse_30s),
            "avg_trade_usd": _mean([r["trade"].usd_value for r in unique]),
        }

    return results


# ── P&L Estimation ────────────────────────────────────────────────────

def estimate_pnl(
    spread_bps: float,
    trades_per_hour: float,
    avg_trade_usd: float,
    adverse_selection_bps: float,
) -> dict:
    """
    Estimate daily P&L for a simple MM strategy.

    Assumptions:
    - Quote at (spread/2 - offset) from mid on both sides
    - Capture = spread/2 - maker_fee - adverse_selection - hedge_cost
    - Fill rate = trades_per_hour (assume we get all trades — optimistic)
    """
    half_spread = spread_bps / 2
    gross_capture = half_spread - MM_QUOTE_OFFSET_BPS
    net_capture = gross_capture - MAKER_FEE_BPS - adverse_selection_bps - HEDGE_COST_BPS

    fills_per_day = trades_per_hour * 24
    daily_volume = fills_per_day * avg_trade_usd
    daily_pnl = daily_volume * net_capture / 10_000

    return {
        "half_spread_bps": half_spread,
        "gross_capture_bps": gross_capture,
        "net_capture_bps": net_capture,
        "fills_per_day": fills_per_day,
        "daily_volume_usd": daily_volume,
        "daily_pnl_usd": daily_pnl,
        "annualized_usd": daily_pnl * 365,
        "profitable": net_capture > 0,
    }


# ── Report Generation ─────────────────────────────────────────────────

def print_report(
    csv_stats: dict,
    adverse: dict,
    live_spreads: dict,
) -> None:
    console.print("\n[bold cyan]═══════════════════════════════════════════════[/bold cyan]")
    console.print("[bold cyan]  Market Making Viability Report[/bold cyan]")
    console.print("[bold cyan]═══════════════════════════════════════════════[/bold cyan]\n")

    # 1. Spread summary
    t1 = Table(title="1. Spread Analysis", show_header=True, header_style="bold")
    t1.add_column("Market", style="bold")
    t1.add_column("HL Spread (bps)", justify="right")
    t1.add_column("Ref Spread (bps)", justify="right")
    t1.add_column("Ratio", justify="right")
    t1.add_column("Edge Available?", justify="center")

    all_pairs = MARKET_PAIRS + [CONTROL_PAIR]
    for pair in all_pairs:
        name = pair["hl_name"]
        ls = live_spreads.get(pair["hl"], {})
        hl_s = ls.get("hl_spread")
        bn_s = ls.get("ref_spread")
        ratio = hl_s / bn_s if hl_s and bn_s and bn_s > 0 else None

        # Use CSV data if available, otherwise live
        cs = csv_stats.get(name, {})
        if cs.get("spread_mean"):
            hl_display = f"{cs['spread_mean']:.1f} (med {cs['spread_median']:.1f})"
        elif hl_s:
            hl_display = f"{hl_s:.1f}"
        else:
            hl_display = "—"

        ref_display = f"{bn_s:.2f}" if bn_s else "—"
        ratio_display = f"{ratio:.0f}x" if ratio else "—"
        edge = "[green]YES[/green]" if ratio and ratio > 3 else (
            "[yellow]MAYBE[/yellow]" if ratio and ratio > 1.5 else "[red]NO[/red]"
        )
        if not hl_s:
            edge = "[dim]NO DATA[/dim]"

        t1.add_row(name, hl_display, ref_display, ratio_display, edge)

    console.print(t1)
    console.print()

    # 2. Liquidity / fill rate
    t2 = Table(title="2. Fill Rate & Liquidity", show_header=True, header_style="bold")
    t2.add_column("Market", style="bold")
    t2.add_column("Trades/hr", justify="right")
    t2.add_column("Avg Trade", justify="right")
    t2.add_column("Depth@10bps", justify="right")
    t2.add_column("Fillable?", justify="center")

    for pair in all_pairs:
        name = pair["hl_name"]
        coin = pair["hl"]
        adv = adverse.get(coin, {})
        ls = live_spreads.get(coin, {})
        cs = csv_stats.get(name, {})

        n_trades = adv.get("n_trades", 0)
        # Estimate trades/hr from the measurement window
        trades_hr = n_trades * 30  # 2-min window → extrapolate to 1 hour
        avg_usd = adv.get("avg_trade_usd", 0)

        if cs.get("trades_5m_mean") is not None:
            trades_hr = cs["trades_5m_mean"] * 12  # 5m → 1hr
        if not avg_usd and n_trades > 0:
            avg_usd = 0

        depth = ls.get("depth_10bps", 0)

        fillable = "[green]YES[/green]" if trades_hr > 10 else (
            "[yellow]THIN[/yellow]" if trades_hr > 2 else "[red]DEAD[/red]"
        )

        t2.add_row(
            name,
            f"{trades_hr:.0f}" if trades_hr else "0",
            format_usd(avg_usd) if avg_usd else "—",
            format_usd(depth) if depth else "—",
            fillable,
        )

    console.print(t2)
    console.print()

    # 3. Adverse selection
    t3 = Table(title="3. Adverse Selection (mid-price move after fill)", show_header=True, header_style="bold")
    t3.add_column("Market", style="bold")
    t3.add_column("Trades Measured", justify="right")
    t3.add_column("5s Move (bps)", justify="right")
    t3.add_column("15s Move (bps)", justify="right")
    t3.add_column("30s Move (bps)", justify="right")
    t3.add_column("Toxic?", justify="center")

    for pair in all_pairs:
        coin = pair["hl"]
        name = pair["hl_name"]
        adv = adverse.get(coin, {})
        n = adv.get("n_trades", 0)
        a5 = adv.get("adverse_5s_bps")
        a15 = adv.get("adverse_15s_bps")
        a30 = adv.get("adverse_30s_bps")

        if n == 0:
            t3.add_row(name, "0", "—", "—", "—", "[dim]NO DATA[/dim]")
            continue

        # Toxic if adverse selection > half the spread
        ls = live_spreads.get(coin, {})
        half_spread = (ls.get("hl_spread") or 10) / 2
        toxic = "[red]YES[/red]" if a5 and a5 > half_spread else (
            "[yellow]WATCH[/yellow]" if a5 and a5 > half_spread * 0.5 else "[green]OK[/green]"
        )

        t3.add_row(
            name,
            str(n),
            f"{a5:+.2f}" if a5 is not None else "—",
            f"{a15:+.2f}" if a15 is not None else "—",
            f"{a30:+.2f}" if a30 is not None else "—",
            toxic,
        )

    console.print(t3)
    console.print()

    # 4. P&L estimate
    console.print(
        f"[bold]4. P&L Estimate[/bold] "
        f"[dim](maker fee={MAKER_FEE_BPS}bps, hedge cost={HEDGE_COST_BPS}bps, "
        f"quote offset={MM_QUOTE_OFFSET_BPS}bps)[/dim]\n"
    )

    t4 = Table(show_header=True, header_style="bold")
    t4.add_column("Market", style="bold")
    t4.add_column("Half Spread", justify="right")
    t4.add_column("Adverse Sel.", justify="right")
    t4.add_column("Net Capture", justify="right")
    t4.add_column("Fills/day", justify="right")
    t4.add_column("Daily P&L", justify="right")
    t4.add_column("Verdict", justify="center")

    for pair in all_pairs:
        coin = pair["hl"]
        name = pair["hl_name"]
        ls = live_spreads.get(coin, {})
        adv = adverse.get(coin, {})
        cs = csv_stats.get(name, {})

        hl_s = cs.get("spread_mean") or ls.get("hl_spread")
        if not hl_s:
            t4.add_row(name, "—", "—", "—", "—", "—", "[dim]NO DATA[/dim]")
            continue

        a5 = adv.get("adverse_5s_bps") or 0
        n_trades = adv.get("n_trades", 0)
        avg_usd = adv.get("avg_trade_usd") or 500  # default assumption

        # Estimate trades per hour
        if cs.get("trades_5m_mean"):
            trades_hr = cs["trades_5m_mean"] * 12
        elif n_trades > 0:
            trades_hr = n_trades * 30  # 2-min window
        else:
            trades_hr = 0

        pnl = estimate_pnl(hl_s, trades_hr, avg_usd, abs(a5))

        net_color = "green" if pnl["net_capture_bps"] > 0 else "red"
        pnl_color = "green" if pnl["daily_pnl_usd"] > 0 else "red"

        verdict = "[green]GO[/green]" if pnl["profitable"] and pnl["daily_pnl_usd"] > 10 else (
            "[yellow]MARGINAL[/yellow]" if pnl["profitable"] else "[red]NO[/red]"
        )

        t4.add_row(
            name,
            f"{pnl['half_spread_bps']:.1f} bps",
            f"{abs(a5):.1f} bps" if a5 else "?",
            f"[{net_color}]{pnl['net_capture_bps']:+.1f} bps[/{net_color}]",
            f"{pnl['fills_per_day']:.0f}",
            f"[{pnl_color}]${pnl['daily_pnl_usd']:+,.0f}[/{pnl_color}]",
            verdict,
        )

    console.print(t4)
    console.print()

    # 5. Conclusion
    console.print("[bold]5. Conclusion[/bold]\n")

    # Build conclusion from data
    viable_markets = []
    for pair in MARKET_PAIRS:
        coin = pair["hl"]
        name = pair["hl_name"]
        ls = live_spreads.get(coin, {})
        adv = adverse.get(coin, {})

        hl_s = ls.get("hl_spread")
        n_trades = adv.get("n_trades", 0)
        a5 = adv.get("adverse_5s_bps")

        if hl_s and hl_s > 5 and n_trades > 0:
            half = hl_s / 2
            net = half - MAKER_FEE_BPS - (abs(a5) if a5 else 0) - HEDGE_COST_BPS - MM_QUOTE_OFFSET_BPS
            if net > 0:
                viable_markets.append((name, hl_s, net, n_trades))

    if viable_markets:
        console.print("[green]Viable MM opportunities detected:[/green]")
        for name, spread, net, n in viable_markets:
            console.print(f"  • [bold]{name}[/bold]: {spread:.1f} bps spread, {net:+.1f} bps net capture, {n} trades in sample")
        console.print()
        console.print("  [yellow]Caveats:[/yellow]")
    else:
        console.print("[yellow]No clearly profitable opportunities at current activity levels.[/yellow]")
        console.print()
        console.print("  [yellow]Key issues:[/yellow]")

    console.print("  • These are [bold]spot[/bold] markets, not perps — no funding income, different risk profile")
    console.print("  • Adverse selection measurement needs 24h+ of data across market hours for confidence")
    console.print("  • Fill rate is the #1 constraint — wide spreads are worthless without volume")
    console.print("  • Hedging RWA spot exposure requires CEX access (Binance PAXG for gold, nothing for stocks)")
    console.print("  • Consider running the monitor during US market hours when stock pairs should be more active")
    console.print()

    # Recommendations
    console.print("[bold]Recommended next steps:[/bold]")
    console.print("  1. Run [bold]monitor.py[/bold] for 48h+ to collect CSV data across all market conditions")
    console.print("  2. Re-run [bold]analyze.py[/bold] with that data for statistically meaningful results")
    console.print("  3. Focus on [bold]XAUT0 (gold)[/bold] — only market with consistent activity + hedgeable")
    console.print("  4. Test during [bold]CME hours[/bold] (Sun 5PM–Fri 4PM CT) when gold has real price discovery")
    console.print("  5. Build a paper-trading bot to validate fill rates before committing capital\n")


# ── Utilities ─────────────────────────────────────────────────────────

def _mean(xs: list) -> float | None:
    return sum(xs) / len(xs) if xs else None

def _median(xs: list) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return (s[n // 2] + s[(n - 1) // 2]) / 2

def _percentile(xs: list, p: int) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * p / 100
    f = int(k)
    c = f + 1 if f + 1 < len(s) else f
    return s[f] + (s[c] - s[f]) * (k - f)


# ── Main ──────────────────────────────────────────────────────────────

async def main() -> None:
    live_only = "--live-only" in sys.argv

    all_pairs = MARKET_PAIRS + [CONTROL_PAIR]
    coins = [p["hl"] for p in all_pairs]
    bn_map = {p["hl"]: p.get("binance") for p in all_pairs}

    # 1. CSV analysis
    csv_stats = {}
    if not live_only:
        rows = load_csv_logs()
        if rows:
            console.print(f"[cyan]Loaded {len(rows)} CSV rows from {len(set(r.get('timestamp','')[:10] for r in rows))} day(s)[/cyan]")
            csv_stats = analyze_csv(rows)
        else:
            console.print("[yellow]No CSV logs found. Run monitor.py first to collect data.[/yellow]")

    # 2. Live snapshot for current spreads
    console.print("[cyan]Fetching live market data...[/cyan]")
    hl = HyperliquidFeed(coins)
    await hl.fetch_meta()
    await hl.fetch_initial_books()

    from feeds.binance import BinanceFeed
    bn_symbols = [p["binance"] for p in all_pairs if p.get("binance")]
    bn = BinanceFeed(bn_symbols)

    # Brief WS connection for Binance spreads
    bn_task = asyncio.create_task(bn.connect_ws())
    await asyncio.sleep(3)

    live_spreads = {}
    for pair in all_pairs:
        coin = pair["hl"]
        book = hl.books.get(coin)
        bn_sym = pair.get("binance")
        bn_book = bn.books.get(bn_sym) if bn_sym else None

        live_spreads[coin] = {
            "hl_spread": book.spread_bps() if book else None,
            "ref_spread": bn_book.spread_bps() if bn_book else None,
            "depth_10bps": book.depth_at_bps(10) if book else 0,
            "mid": book.mid_price if book else None,
        }

    bn.stop()
    bn_task.cancel()
    try:
        await bn_task
    except asyncio.CancelledError:
        pass

    # 3. Adverse selection measurement (2 min live)
    adverse = await measure_adverse_selection(coins, duration_sec=120)

    # 4. Print report
    print_report(csv_stats, adverse, live_spreads)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
