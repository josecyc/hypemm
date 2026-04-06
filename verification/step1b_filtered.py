#!/usr/bin/env python3
"""Step 1b: Re-run backtest with correlation filter.

Only enters trades when 7-day rolling correlation > 0.7.
Also tests dropping marginal pairs entirely.

Usage:
    python -m verification.step1b_filtered
"""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from verification.config import (
    CANDLES_DIR,
    COINS,
    COOLDOWN_HOURS,
    CORR_HIGH,
    CORR_WINDOW_HOURS,
    COST_PER_SIDE_BPS,
    ENTRY_Z,
    EXIT_Z,
    GATE1_MAX_MONTH_DD,
    LOOKBACK_HOURS,
    MAX_HOLD_HOURS,
    NOTIONAL_PER_LEG,
    PAIRS,
    REPORTS_DIR,
    STOP_LOSS_Z,
)
from verification.step1_backtest import (
    Trade,
    compute_sharpe,
    daily_equity,
    load_all_candles,
    max_drawdown,
    monthly_stats,
    print_monthly_table,
    save_trades_csv,
)

console = Console()


def run_backtest_filtered(
    prices: pd.DataFrame,
    pair: tuple[str, str],
    corr_series: pd.Series,
    corr_threshold: float = CORR_HIGH,
    lookback: int = LOOKBACK_HOURS,
    entry_z: float = ENTRY_Z,
) -> list[Trade]:
    """Run stat arb with correlation gate: only enter when corr > threshold."""
    coin_a, coin_b = pair
    pair_label = f"{coin_a}/{coin_b}"

    pa = prices[coin_a].values
    pb = prices[coin_b].values
    timestamps = prices.index
    n = len(pa)

    if n < lookback + 10:
        return []

    # Align correlation series to price index
    corr_aligned = corr_series.reindex(timestamps).ffill().values

    log_ratio = np.log(pa / pb)
    roll_mean = np.full(n, np.nan)
    roll_std = np.full(n, np.nan)
    for i in range(lookback, n):
        window = log_ratio[i - lookback : i]
        roll_mean[i] = np.mean(window)
        roll_std[i] = np.std(window, ddof=1)

    z_scores = np.full(n, np.nan)
    for i in range(lookback, n):
        if roll_std[i] > 1e-10:
            z_scores[i] = (log_ratio[i] - roll_mean[i]) / roll_std[i]

    notional = NOTIONAL_PER_LEG
    rt_cost = notional * 2 * COST_PER_SIDE_BPS / 10_000 * 2

    trades: list[Trade] = []
    position = 0
    entry_idx = 0
    entry_z_val = 0.0
    cooldown_until = 0

    for i in range(lookback + 1, n):
        z = z_scores[i]
        if np.isnan(z):
            continue

        if position == 0:
            if i < cooldown_until:
                continue

            # CORRELATION GATE: skip entry if correlation is below threshold
            corr_val = corr_aligned[i] if i < len(corr_aligned) else np.nan
            if np.isnan(corr_val) or corr_val < corr_threshold:
                continue

            if z > entry_z:
                position = -1
                entry_idx = i
                entry_z_val = z
            elif z < -entry_z:
                position = 1
                entry_idx = i
                entry_z_val = z
        else:
            hours = i - entry_idx
            exit_reason = ""

            if position == 1:
                if z >= -EXIT_Z:
                    exit_reason = "mean_revert"
                elif z > STOP_LOSS_Z:
                    exit_reason = "stop_loss"
            elif position == -1:
                if z <= EXIT_Z:
                    exit_reason = "mean_revert"
                elif z < -STOP_LOSS_Z:
                    exit_reason = "stop_loss"

            if hours >= MAX_HOLD_HOURS:
                exit_reason = "time_stop"

            if not exit_reason:
                continue

            ea, eb = pa[entry_idx], pb[entry_idx]
            xa, xb = pa[i], pb[i]

            if position == 1:
                pnl_a = notional * (xa - ea) / ea
                pnl_b = notional * (eb - xb) / eb
            else:
                pnl_a = notional * (ea - xa) / ea
                pnl_b = notional * (xb - eb) / eb

            gross = pnl_a + pnl_b
            net = gross - rt_cost

            mae = 0.0
            for k in range(entry_idx + 1, i + 1):
                if position == 1:
                    interim_a = notional * (pa[k] - ea) / ea
                    interim_b = notional * (eb - pb[k]) / eb
                else:
                    interim_a = notional * (ea - pa[k]) / ea
                    interim_b = notional * (pb[k] - eb) / eb
                interim = interim_a + interim_b - rt_cost
                if interim < mae:
                    mae = interim

            direction = "long_ratio" if position == 1 else "short_ratio"
            trades.append(Trade(
                pair=pair_label, direction=direction,
                entry_ts=int(timestamps[entry_idx].timestamp() * 1000),
                exit_ts=int(timestamps[i].timestamp() * 1000),
                entry_z=entry_z_val, exit_z=z, hours_held=hours,
                entry_price_a=ea, entry_price_b=eb,
                exit_price_a=xa, exit_price_b=xb,
                pnl_leg_a=pnl_a, pnl_leg_b=pnl_b,
                gross_pnl=gross, cost=rt_cost, net_pnl=net,
                max_adverse_excursion=mae, exit_reason=exit_reason,
            ))

            position = 0
            cooldown_until = i + COOLDOWN_HOURS

    return trades


def compute_rolling_corr(prices: pd.DataFrame, pair: tuple[str, str]) -> pd.Series:
    """Compute rolling 7-day correlation of hourly returns."""
    returns = np.log(prices / prices.shift(1)).dropna()
    return returns[pair[0]].rolling(window=CORR_WINDOW_HOURS).corr(returns[pair[1]])


def main() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    console.print("\n[bold cyan]═══ Step 1b: Filtered Backtest (Correlation Gate) ═══[/bold cyan]\n")

    prices = load_all_candles()
    n_days = (prices.index[-1] - prices.index[0]).days
    console.print(f"  {len(prices)} bars, {n_days} days\n")

    # ── Test 1: All 6 pairs WITH correlation filter ──────────────────────
    console.print("[bold]Test 1: All 6 pairs + correlation filter (corr > 0.7)[/bold]\n")

    all_trades: list[Trade] = []
    for pair in PAIRS:
        corr = compute_rolling_corr(prices, pair)
        trades = run_backtest_filtered(prices, pair, corr, corr_threshold=CORR_HIGH)
        all_trades.extend(trades)
        wins = sum(1 for t in trades if t.net_pnl > 0)
        net = sum(t.net_pnl for t in trades)
        skipped = "—"
        wr = wins / len(trades) * 100 if trades else 0
        console.print(f"  {pair[0]}/{pair[1]}: {len(trades)} trades, {wr:.0f}% WR, ${net:+,.0f}")

    total_net = sum(t.net_pnl for t in all_trades)
    total_wins = sum(1 for t in all_trades if t.net_pnl > 0)
    total_wr = total_wins / len(all_trades) * 100 if all_trades else 0
    sharpe = compute_sharpe(all_trades)
    dd, _ = max_drawdown(all_trades)
    daily = total_net / n_days

    console.print(f"\n  [bold]TOTAL: {len(all_trades)} trades, {total_wr:.0f}% WR, ${total_net:+,.0f}, "
                  f"${daily:+,.0f}/day, Sharpe {sharpe:.2f}, Max DD ${dd:,.0f}[/bold]\n")

    months = monthly_stats(all_trades, prices)
    print_monthly_table(months)
    console.print()

    # Gate check
    profitable_months = sum(1 for m in months if m["net"] > 0)
    worst_dd = max((m["max_dd"] for m in months), default=0)
    console.print(f"  Profitable months: {profitable_months}/{len(months)}")
    console.print(f"  Worst month DD: ${worst_dd:,.0f} (gate: ${GATE1_MAX_MONTH_DD:,})")
    console.print(f"  Sharpe: {sharpe:.2f}")
    p1 = profitable_months >= 4
    p2 = sharpe >= 1.0
    p3 = worst_dd <= GATE1_MAX_MONTH_DD
    verdict1 = "PASS" if (p1 and p2 and p3) else "FAIL"
    c = "green" if verdict1 == "PASS" else "red"
    console.print(f"  [{c} bold]Verdict: {verdict1}[/{c} bold]\n")

    # ── Test 2: Drop weak pairs, keep only consistently profitable ───────
    console.print("[bold]Test 2: Best pairs only (LINK/SOL + DOGE/AVAX + SOL/AVAX) + corr filter[/bold]\n")

    # LINK/SOL was 0 negative months, DOGE/AVAX and SOL/AVAX were 1 negative month
    # but that month was the uncorrelated September. With the filter, they should improve.
    best_pairs = [("LINK", "SOL"), ("DOGE", "AVAX"), ("SOL", "AVAX")]

    best_trades: list[Trade] = []
    for pair in best_pairs:
        corr = compute_rolling_corr(prices, pair)
        trades = run_backtest_filtered(prices, pair, corr, corr_threshold=CORR_HIGH)
        best_trades.extend(trades)
        wins = sum(1 for t in trades if t.net_pnl > 0)
        net = sum(t.net_pnl for t in trades)
        wr = wins / len(trades) * 100 if trades else 0
        console.print(f"  {pair[0]}/{pair[1]}: {len(trades)} trades, {wr:.0f}% WR, ${net:+,.0f}")

    total_net2 = sum(t.net_pnl for t in best_trades)
    total_wr2 = sum(1 for t in best_trades if t.net_pnl > 0) / len(best_trades) * 100 if best_trades else 0
    sharpe2 = compute_sharpe(best_trades)
    dd2, _ = max_drawdown(best_trades)
    daily2 = total_net2 / n_days

    console.print(f"\n  [bold]TOTAL: {len(best_trades)} trades, {total_wr2:.0f}% WR, ${total_net2:+,.0f}, "
                  f"${daily2:+,.0f}/day, Sharpe {sharpe2:.2f}, Max DD ${dd2:,.0f}[/bold]\n")

    months2 = monthly_stats(best_trades, prices)
    print_monthly_table(months2)
    console.print()

    # ── Test 3: ALL 6 pairs, no filter (baseline comparison) ─────────────
    console.print("[bold]Test 3: Top 4 pairs only (LINK/SOL + DOGE/AVAX + SOL/AVAX + BTC/SOL) + corr filter[/bold]\n")

    top4_pairs = [("LINK", "SOL"), ("DOGE", "AVAX"), ("SOL", "AVAX"), ("BTC", "SOL")]
    top4_trades: list[Trade] = []
    for pair in top4_pairs:
        corr = compute_rolling_corr(prices, pair)
        trades = run_backtest_filtered(prices, pair, corr, corr_threshold=CORR_HIGH)
        top4_trades.extend(trades)
        wins = sum(1 for t in trades if t.net_pnl > 0)
        net = sum(t.net_pnl for t in trades)
        wr = wins / len(trades) * 100 if trades else 0
        console.print(f"  {pair[0]}/{pair[1]}: {len(trades)} trades, {wr:.0f}% WR, ${net:+,.0f}")

    total_net3 = sum(t.net_pnl for t in top4_trades)
    total_wr3 = sum(1 for t in top4_trades if t.net_pnl > 0) / len(top4_trades) * 100 if top4_trades else 0
    sharpe3 = compute_sharpe(top4_trades)
    dd3, _ = max_drawdown(top4_trades)
    daily3 = total_net3 / n_days

    console.print(f"\n  [bold]TOTAL: {len(top4_trades)} trades, {total_wr3:.0f}% WR, ${total_net3:+,.0f}, "
                  f"${daily3:+,.0f}/day, Sharpe {sharpe3:.2f}, Max DD ${dd3:,.0f}[/bold]\n")

    months3 = monthly_stats(top4_trades, prices)
    print_monthly_table(months3)

    profitable_months3 = sum(1 for m in months3 if m["net"] > 0)
    worst_dd3 = max((m["max_dd"] for m in months3), default=0)
    console.print()
    console.print(f"  Profitable months: {profitable_months3}/{len(months3)}")
    console.print(f"  Worst month DD: ${worst_dd3:,.0f}")
    console.print(f"  Sharpe: {sharpe3:.2f}")
    p1 = profitable_months3 >= 4
    p2 = sharpe3 >= 1.0
    p3 = worst_dd3 <= GATE1_MAX_MONTH_DD
    v3 = "PASS" if (p1 and p2 and p3) else "FAIL"
    c = "green" if v3 == "PASS" else "red"
    console.print(f"  [{c} bold]Verdict: {v3}[/{c} bold]\n")

    # ── Summary comparison ───────────────────────────────────────────────
    console.print("[bold cyan]═══ Comparison Summary ═══[/bold cyan]\n")
    st = Table(show_header=True, header_style="bold")
    st.add_column("Configuration")
    st.add_column("Trades", justify="right")
    st.add_column("WR", justify="right")
    st.add_column("Net", justify="right")
    st.add_column("Daily", justify="right")
    st.add_column("Sharpe", justify="right")
    st.add_column("Max DD", justify="right")
    st.add_column("Gate", justify="center")

    for label, tr, wr, net, d, s, dd_val, v in [
        ("6 pairs + corr filter", len(all_trades), total_wr, total_net, daily, sharpe, dd, verdict1),
        ("3 best + corr filter", len(best_trades), total_wr2, total_net2, daily2, sharpe2, dd2, "—"),
        ("4 best + corr filter", len(top4_trades), total_wr3, total_net3, daily3, sharpe3, dd3, v3),
    ]:
        nc = "green" if net > 0 else "red"
        vc = "green" if v == "PASS" else "red" if v == "FAIL" else "dim"
        st.add_row(label, str(tr), f"{wr:.0f}%", f"[{nc}]${net:+,.0f}[/{nc}]",
                    f"${d:+,.0f}", f"{s:.2f}", f"${dd_val:,.0f}", f"[{vc}]{v}[/{vc}]")

    console.print(st)

    # Save the best configuration
    save_trades_csv(top4_trades, REPORTS_DIR / "backtest_trades_filtered.csv")
    eq = daily_equity(top4_trades)
    if not eq.empty:
        eq.to_csv(REPORTS_DIR / "daily_equity_filtered.csv", index=False)

    console.print()


if __name__ == "__main__":
    main()
