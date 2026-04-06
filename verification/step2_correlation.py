#!/usr/bin/env python3
"""Step 2: Correlation stability analysis.

Checks whether the structural assumption (pairs are correlated and
divergences are temporary) holds across the full history.

Usage:
    python -m verification.step2_correlation
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from verification.config import (
    CANDLES_DIR,
    COINS,
    CORR_HIGH,
    CORR_LOW,
    CORR_WINDOW_HOURS,
    GATE2_MAX_BREAKDOWN_HOURS,
    GATE2_MIN_HIGH_CORR_PCT,
    GATE2_MIN_HIGH_CORR_WR,
    PAIRS,
    REPORTS_DIR,
)

console = Console()


def load_returns() -> pd.DataFrame:
    """Load candles and compute hourly log returns."""
    frames = {}
    for coin in COINS:
        path = CANDLES_DIR / f"{coin}_1h.csv"
        df = pd.read_csv(path)
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("datetime").sort_index()
        df = df[~df.index.duplicated(keep="first")]
        frames[coin] = df["close"]

    prices = pd.DataFrame(frames).ffill().dropna()
    returns = np.log(prices / prices.shift(1)).dropna()
    return returns, prices


def rolling_correlation(returns: pd.DataFrame, pair: tuple[str, str], window: int) -> pd.Series:
    """Compute rolling Pearson correlation between two return series."""
    a, b = pair
    return returns[a].rolling(window=window).corr(returns[b])


def correlation_regimes(corr_series: pd.Series) -> dict:
    """Classify correlation into HIGH/MEDIUM/LOW regimes."""
    total = corr_series.dropna().count()
    if total == 0:
        return {"high_pct": 0, "med_pct": 0, "low_pct": 0, "mean": 0, "min": 0}

    high = (corr_series > CORR_HIGH).sum()
    low = (corr_series < CORR_LOW).sum()
    med = total - high - low

    return {
        "high_pct": high / total * 100,
        "med_pct": med / total * 100,
        "low_pct": low / total * 100,
        "mean": corr_series.dropna().mean(),
        "std": corr_series.dropna().std(),
        "min": corr_series.dropna().min(),
        "max": corr_series.dropna().max(),
    }


def find_breakdowns(corr_series: pd.Series, threshold: float = CORR_LOW) -> list[dict]:
    """Find continuous periods where correlation is below threshold."""
    below = corr_series < threshold
    breakdowns = []
    in_breakdown = False
    start = None

    for ts, is_below in below.items():
        if pd.isna(is_below):
            continue
        if is_below and not in_breakdown:
            in_breakdown = True
            start = ts
        elif not is_below and in_breakdown:
            in_breakdown = False
            duration_hours = int((ts - start).total_seconds() / 3600)
            min_corr = corr_series[start:ts].min()
            breakdowns.append({
                "start": start.isoformat(),
                "end": ts.isoformat(),
                "duration_hours": duration_hours,
                "min_corr": float(min_corr),
            })

    # Handle ongoing breakdown
    if in_breakdown and start is not None:
        end = corr_series.index[-1]
        duration_hours = int((end - start).total_seconds() / 3600)
        min_corr = corr_series[start:].min()
        breakdowns.append({
            "start": start.isoformat(),
            "end": end.isoformat(),
            "duration_hours": duration_hours,
            "min_corr": float(min_corr),
        })

    return breakdowns


def conditional_pnl(trades_path: str, corr_data: dict[str, pd.Series]) -> dict:
    """Split trades by correlation regime at entry and compare performance."""
    try:
        trades_df = pd.read_csv(trades_path)
    except FileNotFoundError:
        return {}

    results = {}
    for pair_label, corr_series in corr_data.items():
        pair_trades = trades_df[trades_df["pair"] == pair_label]
        if pair_trades.empty:
            continue

        high_corr_trades = []
        low_corr_trades = []

        for _, row in pair_trades.iterrows():
            entry_dt = pd.Timestamp(row["entry_ts"], unit="ms", tz="UTC")
            # Find nearest correlation value
            idx = corr_series.index.get_indexer([entry_dt], method="ffill")[0]
            if idx < 0:
                continue
            corr_at_entry = corr_series.iloc[idx]
            if pd.isna(corr_at_entry):
                continue

            if corr_at_entry > CORR_HIGH:
                high_corr_trades.append(row)
            else:
                low_corr_trades.append(row)

        def _stats(rows):
            if not rows:
                return {"n": 0, "wr": 0, "avg_pnl": 0, "total": 0}
            nets = [r["net_pnl"] for r in rows]
            wins = sum(1 for n in nets if n > 0)
            return {
                "n": len(rows),
                "wr": wins / len(rows) * 100,
                "avg_pnl": sum(nets) / len(rows),
                "total": sum(nets),
            }

        results[pair_label] = {
            "high_corr": _stats(high_corr_trades),
            "low_corr": _stats(low_corr_trades),
        }

    return results


def main() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    console.print("\n[bold cyan]═══ Step 2: Correlation Stability Analysis ═══[/bold cyan]\n")

    console.print("[cyan]Loading data and computing returns...[/cyan]")
    returns, prices = load_returns()
    console.print(f"  {len(returns)} hourly return observations\n")

    # ── 2a: Rolling correlation and regime classification ─────────────────
    console.print("[bold]2a. Rolling Correlation Regimes[/bold]\n")

    corr_data: dict[str, pd.Series] = {}
    regime_results = []

    t1 = Table(show_header=True, header_style="bold")
    t1.add_column("Pair")
    t1.add_column("Mean Corr", justify="right")
    t1.add_column("Std", justify="right")
    t1.add_column("Min", justify="right")
    t1.add_column("HIGH >0.7", justify="right")
    t1.add_column("MED", justify="right")
    t1.add_column("LOW <0.5", justify="right")

    for pair in PAIRS:
        label = f"{pair[0]}/{pair[1]}"
        corr = rolling_correlation(returns, pair, CORR_WINDOW_HOURS)
        corr_data[label] = corr
        regimes = correlation_regimes(corr)
        regime_results.append({"pair": label, **regimes})

        high_color = "green" if regimes["high_pct"] >= GATE2_MIN_HIGH_CORR_PCT else "red"
        t1.add_row(
            label,
            f"{regimes['mean']:.3f}",
            f"{regimes['std']:.3f}",
            f"{regimes['min']:.3f}",
            f"[{high_color}]{regimes['high_pct']:.0f}%[/{high_color}]",
            f"{regimes['med_pct']:.0f}%",
            f"{regimes['low_pct']:.0f}%",
        )

    console.print(t1)
    console.print()

    # Save correlations CSV
    corr_df = pd.DataFrame(corr_data)
    corr_df.to_csv(REPORTS_DIR / "correlations.csv")

    # ── 2b: Correlation breakdown events ─────────────────────────────────
    console.print("[bold]2b. Correlation Breakdown Events (corr < 0.5)[/bold]\n")

    all_breakdowns = {}
    for pair in PAIRS:
        label = f"{pair[0]}/{pair[1]}"
        bds = find_breakdowns(corr_data[label])
        all_breakdowns[label] = bds

        if bds:
            console.print(f"  {label}: {len(bds)} breakdowns")
            for bd in bds[:3]:  # Show first 3
                console.print(
                    f"    {bd['start'][:10]} → {bd['end'][:10]}: "
                    f"{bd['duration_hours']}h, min corr {bd['min_corr']:.3f}"
                )
            if len(bds) > 3:
                console.print(f"    ... and {len(bds) - 3} more")
        else:
            console.print(f"  {label}: [green]No breakdowns[/green]")

    console.print()

    # ── 2c: Conditional P&L ──────────────────────────────────────────────
    console.print("[bold]2c. Conditional P&L (High vs Low Correlation at Entry)[/bold]\n")

    trades_path = REPORTS_DIR / "backtest_trades.csv"
    cond_pnl = conditional_pnl(str(trades_path), corr_data)

    if cond_pnl:
        t2 = Table(show_header=True, header_style="bold")
        t2.add_column("Pair")
        t2.add_column("High Corr Trades", justify="right")
        t2.add_column("High Corr WR", justify="right")
        t2.add_column("High Corr Avg", justify="right")
        t2.add_column("Low Corr Trades", justify="right")
        t2.add_column("Low Corr WR", justify="right")
        t2.add_column("Low Corr Avg", justify="right")

        for pair_label, data in cond_pnl.items():
            h = data["high_corr"]
            l = data["low_corr"]
            h_color = "green" if h["wr"] > 70 else "yellow" if h["wr"] > 50 else "red"
            l_color = "green" if l["wr"] > 70 else "yellow" if l["wr"] > 50 else "red"
            t2.add_row(
                pair_label,
                str(h["n"]),
                f"[{h_color}]{h['wr']:.0f}%[/{h_color}]",
                f"${h['avg_pnl']:+,.0f}",
                str(l["n"]),
                f"[{l_color}]{l['wr']:.0f}%[/{l_color}]",
                f"${l['avg_pnl']:+,.0f}",
            )

        console.print(t2)
    else:
        console.print("  [yellow]No trade data found. Run step1_backtest.py first.[/yellow]")

    console.print()

    # ── Gate 2 verdict ───────────────────────────────────────────────────
    console.print("[bold cyan]═══ Step 2 Gate Assessment ═══[/bold cyan]\n")

    checks = []

    # Check: all pairs > 65% HIGH correlation
    all_high_enough = all(r["high_pct"] >= GATE2_MIN_HIGH_CORR_PCT for r in regime_results)
    pcts = [f"{r['pair']}: {r['high_pct']:.0f}%" for r in regime_results]
    checks.append((
        f"All pairs >{GATE2_MIN_HIGH_CORR_PCT}% time in HIGH corr: {', '.join(pcts)}",
        all_high_enough,
    ))

    # Check: high-corr trades win rate
    if cond_pnl:
        high_corr_wrs = [d["high_corr"]["wr"] for d in cond_pnl.values() if d["high_corr"]["n"] > 0]
        avg_high_wr = sum(high_corr_wrs) / len(high_corr_wrs) if high_corr_wrs else 0
        checks.append((
            f"Avg win rate on high-corr entries: {avg_high_wr:.0f}% (need >{GATE2_MIN_HIGH_CORR_WR}%)",
            avg_high_wr >= GATE2_MIN_HIGH_CORR_WR,
        ))

    # Check: no breakdown > 2 weeks
    max_breakdown = 0
    max_bd_pair = ""
    for label, bds in all_breakdowns.items():
        for bd in bds:
            if bd["duration_hours"] > max_breakdown:
                max_breakdown = bd["duration_hours"]
                max_bd_pair = label
    checks.append((
        f"Max breakdown: {max_breakdown}h / {max_breakdown/24:.0f}d on {max_bd_pair} (limit {GATE2_MAX_BREAKDOWN_HOURS}h)",
        max_breakdown <= GATE2_MAX_BREAKDOWN_HOURS,
    ))

    all_pass = True
    for desc, passed in checks:
        icon = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
        console.print(f"  {icon} {desc}")
        if not passed:
            all_pass = False

    verdict = "PASS" if all_pass else "FAIL"
    color = "green" if all_pass else "red"
    console.print(f"\n  [bold {color}]Step 2 verdict: {verdict}[/bold {color}]\n")

    # Recommendation
    if cond_pnl:
        low_wr_pairs = [
            pair for pair, d in cond_pnl.items()
            if d["low_corr"]["n"] > 5 and d["low_corr"]["wr"] < 50
        ]
        if low_wr_pairs:
            console.print(
                f"  [yellow]Recommendation: Add correlation filter for {', '.join(low_wr_pairs)}. "
                f"Only enter when 7d rolling corr > {CORR_HIGH}.[/yellow]"
            )

    # Save analysis
    analysis = {
        "regimes": regime_results,
        "breakdowns": {k: v for k, v in all_breakdowns.items()},
        "conditional_pnl": {
            k: {kk: vv for kk, vv in v.items()} for k, v in cond_pnl.items()
        } if cond_pnl else {},
        "verdict": verdict,
    }
    with open(REPORTS_DIR / "correlation_analysis.json", "w") as f:
        json.dump(analysis, f, indent=2, default=str)

    return verdict


if __name__ == "__main__":
    main()
