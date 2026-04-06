#!/usr/bin/env python3
"""Final synthesis: combine results from Steps 1-3 into a go/no-go verdict.

Usage:
    python -m verification.synthesize
"""
from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from verification.config import PAIRS, REPORTS_DIR

console = Console()


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def main() -> None:
    console.print("\n")
    console.print(Panel(
        "[bold cyan]STAT ARB VERIFICATION PIPELINE — FINAL REPORT[/bold cyan]",
        border_style="cyan",
        expand=True,
    ))
    console.print()

    bt = load_json(REPORTS_DIR / "backtest_summary.json")
    corr = load_json(REPORTS_DIR / "correlation_analysis.json")
    ob = load_json(REPORTS_DIR / "orderbook_analysis.json")

    missing = []
    if not bt:
        missing.append("backtest_summary.json (run step1_backtest.py)")
    if not corr:
        missing.append("correlation_analysis.json (run step2_correlation.py)")
    if not ob:
        missing.append("orderbook_analysis.json (run step3_orderbook.py)")

    if missing:
        console.print("[red]Missing data files:[/red]")
        for m in missing:
            console.print(f"  - {m}")
        console.print("\n[yellow]Run the missing steps first.[/yellow]")
        return

    # ── Step verdicts ────────────────────────────────────────────────────
    v1 = bt.get("verdict", "UNKNOWN")
    v2 = corr.get("verdict", "UNKNOWN")
    v3 = ob.get("verdict", "UNKNOWN")

    def verdict_style(v):
        if v == "PASS":
            return "[green bold]PASS[/green bold]"
        if v == "FAIL":
            return "[red bold]FAIL[/red bold]"
        return f"[yellow]{v}[/yellow]"

    console.print(f"  Step 1 (Extended Backtest):      {verdict_style(v1)}")

    if bt:
        console.print(
            f"    {bt.get('date_range', '?')} | {bt.get('total_trades', 0)} trades | "
            f"Sharpe {bt.get('sharpe', 0):.2f} | "
            f"Net ${bt.get('total_net', 0):+,.0f}"
        )

    console.print(f"  Step 2 (Correlation Stability):  {verdict_style(v2)}")

    if corr and corr.get("regimes"):
        regimes = corr["regimes"]
        avg_high = sum(r["high_pct"] for r in regimes) / len(regimes)
        console.print(f"    Avg HIGH corr: {avg_high:.0f}%")

    console.print(f"  Step 3 (Orderbook Depth):        {verdict_style(v3)}")

    if ob and ob.get("pair_viability"):
        viable = sum(1 for v in ob["pair_viability"].values() if v["viable"] == "YES")
        console.print(f"    Viable pairs at $50K: {viable}/{len(ob['pair_viability'])}")

    console.print()

    # ── Overall verdict ──────────────────────────────────────────────────
    verdicts = [v1, v2, v3]
    n_pass = sum(1 for v in verdicts if v == "PASS")
    n_fail = sum(1 for v in verdicts if v == "FAIL")

    if n_pass == 3:
        overall = "GO"
        overall_color = "green"
    elif n_fail >= 2:
        overall = "NO-GO"
        overall_color = "red"
    else:
        overall = "CONDITIONAL"
        overall_color = "yellow"

    console.print(Panel(
        f"[bold {overall_color}]Overall Verdict: {overall}[/bold {overall_color}]",
        border_style=overall_color,
    ))
    console.print()

    # ── Detailed recommendations ─────────────────────────────────────────
    if overall == "GO":
        console.print("[bold green]Recommendation:[/bold green]")
        console.print("  Build execution bot, paper trade 1-2 weeks on ETH/SOL,")
        console.print("  then deploy $5K per leg as initial live test.")

    elif overall == "NO-GO":
        console.print("[bold red]Recommendation:[/bold red]")
        console.print("  Strategy does not survive extended validation. Do not deploy.")
        console.print()
        if v1 == "FAIL":
            console.print("  Backtest failure reasons:")
            console.print(f"    Profitable months: {bt.get('profitable_months', 0)}/{bt.get('total_months', 0)}")
            console.print(f"    Sharpe: {bt.get('sharpe', 0):.2f}")
            console.print(f"    Max drawdown: ${bt.get('max_drawdown', 0):,.0f}")
        if v2 == "FAIL":
            console.print("  Correlation failure: pairs are not stable enough for stat arb")
        if v3 == "FAIL":
            console.print("  Depth failure: cannot execute at target size without slippage")

    else:  # CONDITIONAL
        console.print("[bold yellow]Recommendation: Deploy with restrictions[/bold yellow]\n")

        # Filter to viable pairs
        viable_pairs = []
        for pair in PAIRS:
            label = f"{pair[0]}/{pair[1]}"
            pv = ob.get("pair_viability", {}).get(label, {})
            if pv.get("viable") in ("YES", "MAYBE"):
                viable_pairs.append(label)

        if viable_pairs:
            console.print(f"  Viable pairs: {', '.join(viable_pairs)}")

        # Check if correlation filter needed
        if corr.get("conditional_pnl"):
            low_wr_pairs = []
            for pair, data in corr["conditional_pnl"].items():
                lc = data.get("low_corr", {})
                if lc.get("n", 0) > 5 and lc.get("wr", 100) < 50:
                    low_wr_pairs.append(pair)
            if low_wr_pairs:
                console.print(f"  Add correlation filter for: {', '.join(low_wr_pairs)}")
                console.print("  Only enter when 7d rolling corr > 0.7")

        # Position sizing
        rec_sizes = {}
        for label, pv in ob.get("pair_viability", {}).items():
            rec_sizes[label] = pv.get("rec_size", "$25K")
        if rec_sizes:
            console.print(f"  Position sizes: {', '.join(f'{k}={v}' for k, v in rec_sizes.items())}")

        # Expected P&L
        if bt.get("monthly"):
            profitable = [m for m in bt["monthly"] if m["net"] > 0]
            if profitable:
                avg_daily = sum(m["net_per_day"] for m in profitable) / len(profitable)
                console.print(f"  Expected daily P&L (profitable months avg): ${avg_daily:+,.0f}")

        console.print()
        console.print("  [bold]Key risks to monitor:[/bold]")
        console.print("  - Correlation breakdown: exit all positions if 7d corr < 0.3")
        console.print("  - Drawdown limit: halt trading if daily loss > $5K")
        console.print("  - Regime change: monitor BTC trend; strategy may fail in strong trends")
        console.print("  - Alpha decay: compare monthly returns to this backtest")

    console.print()

    # ── Per-pair detail table ────────────────────────────────────────────
    if bt.get("monthly"):
        console.print("[bold]Per-Pair Summary[/bold]\n")

        # Get per-pair stats from trades CSV
        try:
            import pandas as pd
            trades_df = pd.read_csv(REPORTS_DIR / "backtest_trades.csv")

            pt = Table(show_header=True, header_style="bold")
            pt.add_column("Pair")
            pt.add_column("Trades", justify="right")
            pt.add_column("Win Rate", justify="right")
            pt.add_column("Net P&L", justify="right")
            pt.add_column("Sharpe", justify="right")
            pt.add_column("Corr Regime", justify="right")
            pt.add_column("Depth", justify="center")
            pt.add_column("Verdict", justify="center")

            for pair in PAIRS:
                label = f"{pair[0]}/{pair[1]}"
                pair_trades = trades_df[trades_df["pair"] == label]

                n = len(pair_trades)
                wins = (pair_trades["net_pnl"] > 0).sum()
                wr = wins / n * 100 if n > 0 else 0
                net = pair_trades["net_pnl"].sum()

                # Correlation regime
                regime = "?"
                for r in corr.get("regimes", []):
                    if r["pair"] == label:
                        regime = f"{r['high_pct']:.0f}% HIGH"
                        break

                # Depth
                pv = ob.get("pair_viability", {}).get(label, {})
                depth_v = pv.get("viable", "?")

                # Pair verdict
                if net > 0 and wr > 60 and depth_v in ("YES", "MAYBE"):
                    pv_label = "[green]GO[/green]"
                elif net > 0:
                    pv_label = "[yellow]MAYBE[/yellow]"
                else:
                    pv_label = "[red]NO[/red]"

                net_c = "green" if net > 0 else "red"
                pt.add_row(
                    label,
                    str(n),
                    f"{wr:.0f}%",
                    f"[{net_c}]${net:+,.0f}[/{net_c}]",
                    "—",
                    regime,
                    depth_v,
                    pv_label,
                )

            console.print(pt)
        except Exception:
            pass

    console.print()


if __name__ == "__main__":
    main()
