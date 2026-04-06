#!/usr/bin/env python3
"""Step 3: Live orderbook depth analysis.

Collects L2 snapshots every 5 minutes for 2 hours and assesses
execution feasibility for the stat arb strategy.

Usage:
    python -m verification.step3_orderbook
"""
from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from rich.console import Console
from rich.table import Table

from verification.config import (
    COINS,
    DEPTH_BPS_LEVELS,
    GATE3_MIN_DEPTH_10BPS,
    GATE3_MIN_EASY_PAIRS,
    NOTIONAL_PER_LEG,
    OB_COLLECTION_DURATION_SEC,
    OB_SNAPSHOT_INTERVAL_SEC,
    PAIRS,
    RATE_LIMIT_SEC,
    REPORTS_DIR,
    REST_URL,
    SNAPSHOTS_DIR,
)

console = Console()


def fetch_book(client: httpx.Client, coin: str) -> dict | None:
    """Fetch L2 book snapshot."""
    try:
        r = client.post(REST_URL, json={"type": "l2Book", "coin": coin}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        console.print(f"  [red]Failed to fetch {coin} book: {e}[/red]")
        return None


def analyze_book(data: dict) -> dict:
    """Analyze a single L2 book snapshot."""
    levels = data.get("levels", [])
    if len(levels) < 2 or not levels[0] or not levels[1]:
        return {}

    bids = [(float(l["px"]), float(l["sz"])) for l in levels[0]]
    asks = [(float(l["px"]), float(l["sz"])) for l in levels[1]]

    best_bid = bids[0][0] if bids else 0
    best_ask = asks[0][0] if asks else 0
    mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0

    if mid <= 0:
        return {}

    spread_bps = (best_ask - best_bid) / mid * 10_000

    # Depth at various bps levels
    depth = {}
    for bps in DEPTH_BPS_LEVELS:
        threshold = mid * bps / 10_000
        bid_depth = sum(px * sz for px, sz in bids if mid - px <= threshold)
        ask_depth = sum(px * sz for px, sz in asks if px - mid <= threshold)
        depth[bps] = bid_depth + ask_depth

    return {
        "mid": mid,
        "spread_bps": spread_bps,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "n_bid_levels": len(bids),
        "n_ask_levels": len(asks),
        **{f"depth_{bps}bps": d for bps, d in depth.items()},
    }


def fill_rating(avg_depth_5bps: float, avg_depth_10bps: float) -> str:
    """Assess fill feasibility for $50K leg."""
    target = NOTIONAL_PER_LEG
    if avg_depth_5bps > target * 2:
        return "Easy"
    if avg_depth_5bps > target:
        return "Likely"
    if avg_depth_10bps > target:
        return "Tight"
    return "Difficult"


def main() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    console.print("\n[bold cyan]═══ Step 3: Orderbook Depth Analysis ═══[/bold cyan]\n")

    n_snapshots = OB_COLLECTION_DURATION_SEC // OB_SNAPSHOT_INTERVAL_SEC
    console.print(
        f"  Collecting {n_snapshots} snapshots per coin over "
        f"{OB_COLLECTION_DURATION_SEC // 60} minutes "
        f"(every {OB_SNAPSHOT_INTERVAL_SEC // 60} min)\n"
    )

    all_snapshots: dict[str, list[dict]] = {coin: [] for coin in COINS}

    with httpx.Client() as client:
        for snap_i in range(n_snapshots):
            snap_time = datetime.now(timezone.utc)
            console.print(
                f"  Snapshot {snap_i + 1}/{n_snapshots} "
                f"({snap_time.strftime('%H:%M:%S UTC')})",
                end="",
            )

            for coin in COINS:
                time.sleep(RATE_LIMIT_SEC)
                data = fetch_book(client, coin)
                if data:
                    analysis = analyze_book(data)
                    if analysis:
                        analysis["timestamp"] = snap_time.isoformat()
                        analysis["coin"] = coin
                        all_snapshots[coin].append(analysis)

            console.print(" — done")

            # Wait for next interval (minus time spent fetching)
            if snap_i < n_snapshots - 1:
                elapsed = (datetime.now(timezone.utc) - snap_time).total_seconds()
                wait = max(0, OB_SNAPSHOT_INTERVAL_SEC - elapsed)
                if wait > 0:
                    console.print(f"    Waiting {wait:.0f}s until next snapshot...")
                    time.sleep(wait)

    # ── Save raw snapshots ───────────────────────────────────────────────
    for coin, snaps in all_snapshots.items():
        if snaps:
            path = SNAPSHOTS_DIR / f"{coin}_depth.csv"
            fields = list(snaps[0].keys())
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                writer.writerows(snaps)

    # ── 3a: Summary per coin ─────────────────────────────────────────────
    console.print("\n[bold]3a. Orderbook Depth Summary[/bold]\n")

    coin_stats = {}
    t1 = Table(show_header=True, header_style="bold")
    t1.add_column("Coin")
    t1.add_column("Avg Spread", justify="right")
    for bps in DEPTH_BPS_LEVELS:
        t1.add_column(f"Depth@{bps}bps", justify="right")
    t1.add_column("Fill Rating", justify="center")

    for coin in COINS:
        snaps = all_snapshots[coin]
        if not snaps:
            continue

        avg_spread = sum(s["spread_bps"] for s in snaps) / len(snaps)
        depths = {}
        for bps in DEPTH_BPS_LEVELS:
            key = f"depth_{bps}bps"
            vals = [s[key] for s in snaps if key in s]
            depths[bps] = sum(vals) / len(vals) if vals else 0

        rating = fill_rating(depths.get(5, 0), depths.get(10, 0))
        coin_stats[coin] = {
            "avg_spread_bps": avg_spread,
            "depths": depths,
            "rating": rating,
            "n_snapshots": len(snaps),
        }

        rating_color = "green" if rating == "Easy" else "yellow" if rating in ("Likely", "Tight") else "red"
        row = [
            coin,
            f"{avg_spread:.1f} bps",
        ]
        for bps in DEPTH_BPS_LEVELS:
            d = depths.get(bps, 0)
            row.append(f"${d:,.0f}")
        row.append(f"[{rating_color}]{rating}[/{rating_color}]")
        t1.add_row(*row)

    console.print(t1)
    console.print()

    # ── 3b: Pair viability ───────────────────────────────────────────────
    console.print("[bold]3b. Pair Viability Matrix[/bold]\n")

    t2 = Table(show_header=True, header_style="bold")
    t2.add_column("Pair")
    t2.add_column("Leg A Fill")
    t2.add_column("Leg B Fill")
    t2.add_column("Pair Viable", justify="center")
    t2.add_column("Rec. Leg Size", justify="right")

    easy_pairs = 0
    pair_viability = {}
    for pair in PAIRS:
        a, b = pair
        label = f"{a}/{b}"
        ra = coin_stats.get(a, {}).get("rating", "Unknown")
        rb = coin_stats.get(b, {}).get("rating", "Unknown")

        if ra == "Easy" and rb == "Easy":
            viable = "YES"
            rec_size = "$50K"
            easy_pairs += 1
        elif ra in ("Easy", "Likely") and rb in ("Easy", "Likely"):
            viable = "MAYBE"
            rec_size = "$25K"
        elif "Difficult" in (ra, rb):
            viable = "NO"
            rec_size = "$10K max"
        else:
            viable = "MAYBE"
            rec_size = "$25K"

        pair_viability[label] = {"viable": viable, "rec_size": rec_size}

        v_color = "green" if viable == "YES" else "yellow" if viable == "MAYBE" else "red"
        t2.add_row(label, ra, rb, f"[{v_color}]{viable}[/{v_color}]", rec_size)

    console.print(t2)
    console.print()

    # ── Gate 3 verdict ───────────────────────────────────────────────────
    console.print("[bold cyan]═══ Step 3 Gate Assessment ═══[/bold cyan]\n")

    # Check depth for best pair
    best_pair_coins = ["ETH", "SOL"]
    best_depth = min(
        coin_stats.get(c, {}).get("depths", {}).get(10, 0) for c in best_pair_coins
    )

    checks = []
    checks.append((
        f"Pairs with 'Easy fill' at $50K: {easy_pairs} (need {GATE3_MIN_EASY_PAIRS})",
        easy_pairs >= GATE3_MIN_EASY_PAIRS,
    ))
    checks.append((
        f"ETH/SOL min depth at 10bps: ${best_depth:,.0f} (need ${GATE3_MIN_DEPTH_10BPS:,})",
        best_depth >= GATE3_MIN_DEPTH_10BPS,
    ))

    all_pass = True
    for desc, passed in checks:
        icon = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
        console.print(f"  {icon} {desc}")
        if not passed:
            all_pass = False

    verdict = "PASS" if all_pass else "FAIL"
    color = "green" if all_pass else "red"
    console.print(f"\n  [bold {color}]Step 3 verdict: {verdict}[/bold {color}]\n")

    # Save analysis
    analysis = {
        "coin_stats": {
            coin: {
                "avg_spread_bps": s["avg_spread_bps"],
                "depths": {str(k): v for k, v in s["depths"].items()},
                "rating": s["rating"],
                "n_snapshots": s["n_snapshots"],
            }
            for coin, s in coin_stats.items()
        },
        "pair_viability": pair_viability,
        "verdict": verdict,
    }
    with open(REPORTS_DIR / "orderbook_analysis.json", "w") as f:
        json.dump(analysis, f, indent=2)

    return verdict


if __name__ == "__main__":
    main()
