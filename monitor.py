#!/usr/bin/env python3
"""Hyperliquid RWA — Market Making Edge Monitor.

Connects to Hyperliquid and Binance WebSockets, compares orderbook spreads
in real-time, and scores the market making edge on HIP-3 RWA spot markets.

No API keys needed — read-only public data only.

Usage:
    python monitor.py
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from rich.console import Console
from rich.live import Live
from rich.table import Table

from analysis import (
    CONTROL_PAIR,
    MARKET_PAIRS,
    edge_verdict,
    format_usd,
    is_spot,
    trade_stats,
    volume_imbalance,
)
from display import build_display
from feeds.binance import BinanceFeed
from feeds.hyperliquid import HyperliquidFeed
from logger import CSVLogger

console = Console()


def all_hl_coins() -> list[str]:
    return [p["hl"] for p in MARKET_PAIRS] + [CONTROL_PAIR["hl"]]


def all_bn_symbols() -> list[str]:
    syms = [p["binance"] for p in MARKET_PAIRS if p.get("binance")]
    syms.append(CONTROL_PAIR["binance"])
    return syms


# ── Startup summary ──────────────────────────────────────────────────


async def startup_summary(hl: HyperliquidFeed) -> None:
    console.print(
        "\n[bold cyan]═══ Hyperliquid RWA — Startup Summary ═══[/bold cyan]\n"
    )

    # Find all HIP-3 spot pairs (USDC pairs with HIP-3 deployed tokens)
    hip3_pairs = []
    for coin, ctx in hl.asset_ctxs.items():
        if ctx.get("source") != "spot":
            continue
        if not coin.startswith("@"):
            continue
        token_indices = ctx.get("token_indices", [])
        # Check if paired with USDC (token 0) and has a HIP-3 token
        if 0 not in token_indices:
            continue
        other_indices = [i for i in token_indices if i != 0]
        if not other_indices:
            continue
        other_token = hl.spot_tokens.get(other_indices[0], {})
        deployer_share = other_token.get("deployerTradingFeeShare", "0")
        if deployer_share != "1.0":
            continue
        token_name = other_token.get("name", "?")
        hip3_pairs.append((coin, token_name, ctx))

    if not hip3_pairs:
        console.print("[yellow]No HIP-3 RWA spot pairs found.[/yellow]\n")
        return

    # Sort by 24h volume descending
    def _vol(item):
        try:
            return float(item[2].get("dayNtlVlm", "0") or "0")
        except (ValueError, TypeError):
            return 0.0

    hip3_pairs.sort(key=_vol, reverse=True)

    t = Table(title="HIP-3 Spot Pairs (USDC)", show_header=True, header_style="bold")
    t.add_column("Pair", style="bold")
    t.add_column("Token", style="bold")
    t.add_column("Mid Price", justify="right")
    t.add_column("24h Volume", justify="right")
    t.add_column("Spread (bps)", justify="right")
    t.add_column("Status")

    shown = 0
    for coin, token_name, ctx in hip3_pairs:
        try:
            mid_raw = ctx.get("midPx")
            mid = float(mid_raw) if mid_raw and mid_raw != "None" else None
            vol = float(ctx.get("dayNtlVlm", "0") or "0")
        except (ValueError, TypeError):
            continue

        # Get spread from book if we have it
        book = hl.books.get(coin)
        spread = book.spread_bps() if book else None
        spread_str = f"{spread:.1f}" if spread is not None else "—"

        # Determine if this is a monitored pair
        monitored = coin in [p["hl"] for p in MARKET_PAIRS]
        status = "[green]MONITORING[/green]" if monitored else (
            "[dim]Low vol[/dim]" if vol < 100 else "[yellow]Available[/yellow]"
        )

        mid_str = f"${mid:,.2f}" if mid else "—"

        t.add_row(coin, token_name, mid_str, format_usd(vol), spread_str, status)
        shown += 1
        if shown >= 25:
            break

    console.print(t)
    console.print(f"\n  [dim]Total HIP-3 USDC pairs found: {len(hip3_pairs)}[/dim]")
    console.print()


# ── CSV snapshot ──────────────────────────────────────────────────────


def log_snapshot(logger: CSVLogger, hl: HyperliquidFeed, bn: BinanceFeed) -> None:
    pairs = MARKET_PAIRS + [CONTROL_PAIR]
    for pair in pairs:
        hl_coin = pair["hl"]
        bn_sym = pair.get("binance")

        hl_book = hl.books.get(hl_coin)
        bn_book = bn.books.get(bn_sym) if bn_sym else None

        hl_s = hl_book.spread_bps() if hl_book else None
        bn_s = bn_book.spread_bps() if bn_book else None

        ratio = (
            round(hl_s / bn_s, 2)
            if hl_s is not None and bn_s is not None and bn_s > 0
            else None
        )
        edge = (
            round(hl_s - bn_s, 2)
            if hl_s is not None and bn_s is not None
            else None
        )

        stats = trade_stats(hl.trades, hl_coin)
        imb = volume_imbalance(hl.trades, hl_coin)

        ctx = hl.asset_ctxs.get(hl_coin, {})
        funding = ctx.get("funding") if not is_spot(hl_coin) else None

        mid = hl_book.mid_price if hl_book else None
        oracle_raw = ctx.get("oraclePx") if not is_spot(hl_coin) else None
        oracle_div = None
        if mid and oracle_raw and mid > 0:
            try:
                oracle_div = round((mid - float(oracle_raw)) / mid * 10_000, 2)
            except (ValueError, TypeError):
                pass

        d10 = hl_book.depth_at_bps(10) if hl_book else 0.0
        vtext, _ = edge_verdict(hl_s, bn_s, stats[300]["count"], d10)

        logger.log(
            pair.get("hl_name", hl_coin),
            {
                "hl_spread_bps": round(hl_s, 2) if hl_s is not None else None,
                "ref_spread_bps": round(bn_s, 2) if bn_s is not None else None,
                "spread_ratio": ratio,
                "edge_bps": edge,
                "trades_1m": stats[60]["count"],
                "trades_5m": stats[300]["count"],
                "trades_60m": stats[3600]["count"],
                "avg_trade_size_usd": round(stats[300]["avg_size_usd"], 2),
                "depth_5bps": round(hl_book.depth_at_bps(5), 2) if hl_book else None,
                "depth_10bps": round(d10, 2) if d10 else None,
                "depth_25bps": round(hl_book.depth_at_bps(25), 2) if hl_book else None,
                "depth_50bps": round(hl_book.depth_at_bps(50), 2) if hl_book else None,
                "volume_imbalance_pct": round(imb, 1),
                "funding_rate": funding,
                "oracle_mid_div_bps": oracle_div,
                "verdict": vtext,
            },
        )


# ── Main ──────────────────────────────────────────────────────────────


async def main() -> None:
    hl = HyperliquidFeed(all_hl_coins())
    bn = BinanceFeed(all_bn_symbols())
    logger = CSVLogger()

    # Fetch metadata
    console.print("[cyan]Fetching Hyperliquid metadata...[/cyan]")
    try:
        await hl.fetch_meta()
    except Exception as e:
        console.print(f"[red]Failed to fetch metadata: {e}[/red]")
        return

    # Fetch initial data
    console.print("[cyan]Fetching initial orderbooks and trades...[/cyan]")
    await asyncio.gather(hl.fetch_initial_books(), hl.fetch_recent_trades())

    await startup_summary(hl)

    console.print("[green]Starting live monitor... (Ctrl+C to quit)[/green]\n")

    # Launch background feed tasks
    tasks = [
        asyncio.create_task(hl.connect_ws()),
        asyncio.create_task(bn.connect_ws()),
        asyncio.create_task(hl.refresh_asset_ctxs_loop()),
    ]

    try:
        with Live(console=console, refresh_per_second=2) as live:
            last_log = 0.0
            while True:
                live.update(build_display(hl, bn))
                hl.prune_trades()

                now = time.time()
                if now - last_log >= 30:
                    log_snapshot(logger, hl, bn)
                    last_log = now

                await asyncio.sleep(0.5)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        console.print("\n[yellow]Shutting down...[/yellow]")
        hl.stop()
        bn.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
