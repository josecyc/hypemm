"""Compare live paper-trades.csv against backtest on identical window.

Decomposes the live-vs-backtest gap into:
  - Slippage (matched trades, gross_pnl delta)
  - Missed signals (trades in backtest but not live, and vice versa)
  - Funding cost (estimated from HL hourly funding rates over each trade window)

Run periodically as live data accumulates to track parity drift. Use the
output to recalibrate cost_per_side_bps + slippage_per_side_bps in the
strategy config.

Usage:
    uv run python scripts/calibrate.py \\
        --live data/runs/paper/optimized_4pair/paper_trades.csv \\
        --candles data/market/hyperliquid/candles \\
        --funding data/market/hyperliquid/funding \\
        --config configs/backtest/original_4pair_hl.toml
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path

import pandas as pd

from hypemm.backtest import run_backtest_all_pairs
from hypemm.config import load_config
from hypemm.data import load_candles
from hypemm.funding import compute_funding_cost, load_funding
from hypemm.models import Direction


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--live", required=True, help="paper_trades.csv path")
    p.add_argument("--candles", required=True, help="candles dir for backtest")
    p.add_argument("--funding", required=True, help="funding dir for backtest")
    p.add_argument("--config", required=True)
    p.add_argument(
        "--match-tolerance-hours",
        type=int,
        default=2,
        help="Max entry-time delta to consider a live and backtest trade matched",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    app = load_config(Path(args.config))
    config = app.strategy

    prices = load_candles(Path(args.candles), config.all_coins)
    funding_df = load_funding(Path(args.funding), config.all_coins)
    end = prices.index[-1]
    prices = prices.loc[prices.index >= end - pd.Timedelta(days=60)]
    funding_df = funding_df.loc[funding_df.index >= end - pd.Timedelta(days=60)]

    bt_no_fund = run_backtest_all_pairs(prices, config, funding=None)
    bt_with_fund = run_backtest_all_pairs(prices, config, funding=funding_df)

    live = list(csv.DictReader(open(args.live)))
    if not live:
        print("No live trades to calibrate against.")
        return

    live_start = int(datetime.fromisoformat(live[0]["entry_time"]).timestamp() * 1000)
    bt_no_overlap = [t for t in bt_no_fund if t.entry_ts >= live_start - 7_200_000]
    bt_with_overlap = [t for t in bt_with_fund if t.entry_ts >= live_start - 7_200_000]

    print(f"Window: {live[0]['entry_time']} -> {live[-1]['exit_time']}")
    live_net = sum(float(t["net_pnl"]) for t in live)
    bt_no_net = sum(t.net_pnl for t in bt_no_overlap)
    bt_w_net = sum(t.net_pnl for t in bt_with_overlap)
    print(f"  Live (legacy, no funding):  {len(live):>3} trades, ${live_net:>+8,.0f}")
    print(f"  Backtest WITHOUT funding:   {len(bt_no_overlap):>3} trades, ${bt_no_net:>+8,.0f}")
    print(f"  Backtest WITH HL funding:   {len(bt_with_overlap):>3} trades, ${bt_w_net:>+8,.0f}")

    matched, unmatched_live = [], []
    pool = list(bt_no_overlap)
    tol_ms = args.match_tolerance_hours * 3_600_000
    for lt in live:
        lts = int(datetime.fromisoformat(lt["entry_time"]).timestamp() * 1000)
        cands = [t for t in pool if t.pair_label == lt["pair"] and abs(t.entry_ts - lts) <= tol_ms]
        if cands:
            best = min(cands, key=lambda t: abs(t.entry_ts - lts))
            matched.append((lt, best))
            pool.remove(best)
        else:
            unmatched_live.append(lt)
    unmatched_bt = pool

    notional = config.notional_per_leg
    turn = notional * 4

    if matched:
        slip_total = sum(float(lt["gross_pnl"]) - bt.gross_pnl for lt, bt in matched)
        slip_per_trade = slip_total / len(matched)
        slip_bps = -slip_per_trade / turn * 10_000
        print(
            f"\nMatched: {len(matched)}/{len(live)} live "
            f"(unmatched: {len(unmatched_live)} live, {len(unmatched_bt)} backtest)"
        )
        print(
            f"Slippage (live gross - backtest gross): ${slip_total:>+8,.0f} "
            f"({slip_per_trade:+.0f}/trade, {slip_bps:+.2f} bps/side)"
        )

    missed_live = sum(t.net_pnl for t in unmatched_bt)
    missed_bt = sum(float(t["net_pnl"]) for t in unmatched_live)
    print(f"P&L of {len(unmatched_bt)} signals live missed:  ${missed_live:>+8,.0f}")
    print(f"P&L of {len(unmatched_live)} signals bt missed:    ${missed_bt:>+8,.0f}")

    # Estimate funding for live trades
    total_funding = 0.0
    per_pair: dict[str, float] = {}
    skipped = 0
    for lt in live:
        coin_a, coin_b = lt["pair"].split("/")
        direction = (
            Direction.LONG_RATIO if lt["direction"] == "long_ratio" else Direction.SHORT_RATIO
        )
        entry_ts = (
            int(datetime.fromisoformat(lt["entry_time"]).timestamp() * 1000) // 3_600_000
        ) * 3_600_000
        exit_ts = (
            int(datetime.fromisoformat(lt["exit_time"]).timestamp() * 1000) // 3_600_000
        ) * 3_600_000
        if exit_ts <= entry_ts:
            exit_ts = entry_ts + 3_600_000
        try:
            fc = compute_funding_cost(
                direction,
                notional,
                entry_ts,
                exit_ts,
                funding_df[coin_a],
                funding_df[coin_b],
            )
        except (KeyError, ValueError):
            skipped += 1
            continue
        total_funding += fc
        per_pair[lt["pair"]] = per_pair.get(lt["pair"], 0.0) + fc

    print("\nFunding (estimated for live trades using HL rates):")
    print(
        f"  Total: ${total_funding:+,.0f}  (${total_funding/max(len(live)-skipped,1):+.1f}/trade)"
    )
    for p, f in sorted(per_pair.items()):
        print(f"  {p}: ${f:+.0f}")

    # Suggested calibration
    print("\nSuggested config calibration (paper-trading only):")
    if matched:
        observed_total_bps = config.cost_per_side_bps - slip_bps
        print(f"  cost_per_side_bps:       {config.cost_per_side_bps} (HL fee, unchanged)")
        print(
            f"  slippage_per_side_bps:   {-slip_bps:+.2f} "
            f"(observed; negative = paper outperforms backtest)"
        )
        print(
            f"  effective per-side cost: {observed_total_bps:.2f} bps "
            "(use this for live deployment)"
        )


if __name__ == "__main__":
    main()
