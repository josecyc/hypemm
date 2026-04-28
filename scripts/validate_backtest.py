"""Stress-test the backtest to see if the Sharpe 4.83 result is real.

Runs a series of hostile adjustments and reports how each affects P&L:
1. Baseline (current backtest)
2. Next-bar fill (entry/exit at bar i+1 close instead of bar i close)
3. Realistic funding costs (estimated for Hyperliquid perps)
4. Taker fees (5bps per side instead of 2bps)
5. Combined realistic (next-bar + funding + taker)
6. Walk-forward (train/test split)
7. Parameter sensitivity from the sweep
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from hypemm.backtest import (
    compute_sharpe,
    max_drawdown,
    run_backtest_all_pairs,
    run_parameter_sweep,
)
from hypemm.config import StrategyConfig, load_config
from hypemm.data import load_candles
from hypemm.engine import StrategyEngine
from hypemm.funding import load_funding
from hypemm.math import (
    compute_log_ratios,
    compute_z_scores,
    rolling_correlation,
)
from hypemm.models import CompletedTrade, EntryOrder, ExitOrder, PairConfig, Signal


def run_backtest_with_lag(
    prices: pd.DataFrame,
    pair: PairConfig,
    config: StrategyConfig,
    fill_lag_bars: int = 0,
) -> list[CompletedTrade]:
    """Run backtest but fill at bar i+lag instead of bar i.

    fill_lag_bars=0 is the original behavior.
    fill_lag_bars=1 fills at next bar's close (more realistic).
    """
    pa = prices[pair.coin_a].values
    pb = prices[pair.coin_b].values
    timestamps = prices.index
    n = len(pa)

    if n < config.lookback_hours + 10:
        return []

    log_ratios = compute_log_ratios(np.asarray(pa), np.asarray(pb))
    z_scores = compute_z_scores(log_ratios, config.lookback_hours)
    ret_a = np.diff(np.log(pa))
    ret_b = np.diff(np.log(pb))
    corr = rolling_correlation(ret_a, ret_b, config.corr_window_hours)
    corr_values = np.concatenate([[np.nan], corr])

    engine = StrategyEngine(replace(config, pairs=(pair,)))
    completed: list[CompletedTrade] = []

    for i in range(config.lookback_hours + 1, n - fill_lag_bars):
        z = z_scores[i]
        if np.isnan(z):
            continue

        c = corr_values[i] if not np.isnan(corr_values[i]) else None
        ts_ms = int(timestamps[i].timestamp() * 1000)
        fill_idx = i + fill_lag_bars

        signal = Signal(
            pair=pair,
            z_score=float(z),
            correlation=c,
            price_a=float(pa[i]),
            price_b=float(pb[i]),
            timestamp_ms=ts_ms,
            n_bars=i + 1,
        )

        orders = engine.process_bar({pair.label: signal}, ts_ms)

        for order in orders:
            fill_a = float(pa[fill_idx])
            fill_b = float(pb[fill_idx])
            if isinstance(order, EntryOrder):
                engine.confirm_entry(order, fill_a, fill_b, ts_ms)
            elif isinstance(order, ExitOrder):
                trade = engine.confirm_exit(order, fill_a, fill_b, ts_ms)
                completed.append(trade)

    return completed


def apply_funding(
    trades: list[CompletedTrade], funding_bps_per_hour: float
) -> list[CompletedTrade]:
    """Deduct funding cost from each trade. Funding applies to BOTH legs gross notional."""
    adjusted = []
    for t in trades:
        funding_cost = 2 * 50_000 * (funding_bps_per_hour / 10_000) * t.hours_held
        new_net = t.net_pnl - funding_cost
        adjusted.append(replace(t, net_pnl=new_net, cost=t.cost + funding_cost))
    return adjusted


def apply_extra_cost(
    trades: list[CompletedTrade], extra_bps_per_side: float
) -> list[CompletedTrade]:
    """Add extra cost per side. 2 legs × 2 sides × notional."""
    adjusted = []
    for t in trades:
        extra = 50_000 * (extra_bps_per_side / 10_000) * 4
        new_net = t.net_pnl - extra
        adjusted.append(replace(t, net_pnl=new_net, cost=t.cost + extra))
    return adjusted


def report(label: str, trades: list[CompletedTrade]) -> None:
    if not trades:
        print(f"{label}: no trades")
        return
    net = sum(t.net_pnl for t in trades)
    wins = sum(1 for t in trades if t.net_pnl > 0)
    wr = wins / len(trades) * 100
    sh = compute_sharpe(trades)
    dd = max_drawdown(trades)
    avg = net / len(trades)
    print(
        f"{label:<40} n={len(trades):>4}  net=${net:>+9,.0f}  "
        f"avg=${avg:>+6.0f}  WR={wr:>4.0f}%  Sharpe={sh:>5.2f}  DD=${dd:>7,.0f}"
    )


def exit_reason_distribution(trades: list[CompletedTrade]) -> None:
    counts: dict[str, int] = {}
    for t in trades:
        counts[str(t.exit_reason)] = counts.get(str(t.exit_reason), 0) + 1
    total = len(trades)
    print("\nExit reason distribution:")
    for reason, count in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {reason:<30} {count:>4} ({count / total * 100:>4.1f}%)")


def hold_time_distribution(trades: list[CompletedTrade]) -> None:
    hours = [t.hours_held for t in trades]
    print("\nHold time (hours):")
    print(
        f"  mean={np.mean(hours):.1f}  median={np.median(hours):.1f}  "
        f"min={min(hours)}  max={max(hours)}"
    )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    app = load_config(Path(args.config))
    config = app.strategy
    prices = load_candles(app.infra.candles_dir, config.all_coins)

    # Load real funding and align prices to the funding window for apples-to-apples
    funding = load_funding(app.infra.funding_dir, config.all_coins)
    aligned_start = max(prices.index[0], funding.index[0])
    aligned_end = min(prices.index[-1], funding.index[-1])
    prices = prices.loc[aligned_start:aligned_end]
    funding = funding.loc[aligned_start:aligned_end]

    print(f"Data: {len(prices)} hourly bars, " f"{(prices.index[-1] - prices.index[0]).days} days")
    print(f"Funding: {len(funding)} records, " f"{funding.index[0]} -> {funding.index[-1]}\n")

    # ==== Baseline (no funding) ====
    print("=" * 110)
    print("BASELINE NO FUNDING (2bps per side, same-bar fill — the Sharpe 4.83 number)")
    print("=" * 110)
    base = run_backtest_all_pairs(prices, config)
    report("Baseline, no funding", base)
    exit_reason_distribution(base)
    hold_time_distribution(base)

    # ==== REAL FUNDING (the actual answer) ====
    print("\n" + "=" * 110)
    print("REAL FUNDING (actual Hyperliquid hourly rates, same-bar fill)")
    print("=" * 110)
    real = run_backtest_all_pairs(prices, config, funding=funding)
    report("Baseline + real funding", real)

    # Show per-trade funding cost stats
    funding_costs = [t.funding_cost for t in real]
    print(
        f"\n  Per-trade funding cost:  mean=${np.mean(funding_costs):+.2f}  "
        f"median=${np.median(funding_costs):+.2f}  "
        f"min=${min(funding_costs):+.2f}  max=${max(funding_costs):+.2f}"
    )
    total_funding = sum(funding_costs)
    print(
        f"  Total funding paid: ${total_funding:+,.0f} "
        f"({'cost' if total_funding > 0 else 'CREDIT'})"
    )

    # Per-pair with real funding
    print("\n  Per-pair with real funding:")
    from collections import defaultdict

    per_pair: dict[str, list[CompletedTrade]] = defaultdict(list)
    for t in real:
        per_pair[t.pair_label].append(t)
    for pair_label, ts in per_pair.items():
        net = sum(t.net_pnl for t in ts)
        fund = sum(t.funding_cost for t in ts)
        sh = compute_sharpe(ts)
        print(
            f"    {pair_label:<12} n={len(ts):>3} net=${net:>+7,.0f} "
            f"funding=${fund:>+7,.0f} Sharpe={sh:.2f}"
        )

    # ==== Stress 1: Next-bar fill ====
    print("\n" + "=" * 100)
    print("STRESS 1: Next-bar fill (fills 1 hour after signal, i.e. at t+1 close)")
    print("=" * 100)
    lag_trades: list[CompletedTrade] = []
    for pair in config.pairs:
        lag_trades.extend(run_backtest_with_lag(prices, pair, config, fill_lag_bars=1))
    report("Next-bar fill (+1h)", lag_trades)

    # ==== Most realistic: next-bar + real funding + taker fees ====
    print("\n" + "=" * 110)
    print("MOST REALISTIC: next-bar fill + REAL funding + taker fees")
    print("=" * 110)
    lag_with_real_funding: list[CompletedTrade] = []
    for pair in config.pairs:
        lag_with_real_funding.extend(run_backtest_with_lag(prices, pair, config, fill_lag_bars=1))
    # Apply real funding to lag trades
    from hypemm.funding import compute_funding_cost

    adjusted = []
    for t in lag_with_real_funding:
        try:
            fc = compute_funding_cost(
                t.direction,
                config.notional_per_leg,
                t.entry_ts,
                t.exit_ts,
                funding[t.pair_label.split("/")[0]],
                funding[t.pair_label.split("/")[1]],
            )
        except ValueError:
            fc = 0.0
        adjusted.append(replace(t, funding_cost=fc, net_pnl=t.net_pnl - fc))
    report("Next-bar + real funding", adjusted)
    realistic = apply_extra_cost(adjusted, extra_bps_per_side=3.0)
    report("Next-bar + real funding + taker fees", realistic)

    # ==== Stress 2: Funding rates ====
    # Hyperliquid funding: typically ~0.01% per hour on one side.
    # For a stat arb with both legs long/short, the NET funding depends on
    # relative funding rates between A and B, which can be ±. Conservatively
    # assume a small drag on the short leg: 0.5bps/hour on the $50K notional.
    print("\n" + "=" * 100)
    print("STRESS 2: Funding cost (0.5bps/hour per leg, ~$5/hour on $100K gross)")
    print("=" * 100)
    funded = apply_funding(base, funding_bps_per_hour=0.5)
    report("Baseline + 0.5bps/h funding", funded)

    funded_heavy = apply_funding(base, funding_bps_per_hour=1.0)
    report("Baseline + 1.0bps/h funding", funded_heavy)

    # ==== Stress 3: Higher fees (taker) ====
    print("\n" + "=" * 100)
    print("STRESS 3: Taker fees instead of maker (+3bps per side round-trip)")
    print("=" * 100)
    taker = apply_extra_cost(base, extra_bps_per_side=3.0)
    report("Baseline + taker fees (5bps total)", taker)

    # ==== Stress 4: Combined realistic ====
    print("\n" + "=" * 100)
    print("STRESS 4: COMBINED REALISTIC (next-bar fill + 0.5bps/h funding + taker fees)")
    print("=" * 100)
    combined = apply_extra_cost(apply_funding(lag_trades, 0.5), 3.0)
    report("All 3 adjustments", combined)

    combined_heavy = apply_extra_cost(apply_funding(lag_trades, 1.0), 3.0)
    report("All 3 (heavy funding 1.0bps/h)", combined_heavy)

    # ==== Walk-forward ====
    print("\n" + "=" * 100)
    print("WALK-FORWARD: train on first 60%, test on last 40%")
    print("=" * 100)
    n = len(prices)
    split = int(n * 0.6)
    train_prices = prices.iloc[:split]
    test_prices = prices.iloc[split:]
    print(f"Train: {train_prices.index[0]} to {train_prices.index[-1]} ({len(train_prices)} bars)")
    print(f"Test:  {test_prices.index[0]} to {test_prices.index[-1]} ({len(test_prices)} bars)\n")

    # Find best params on train
    from hypemm.config import SweepConfig

    print("Parameter sweep on TRAIN:")
    sweep_train = run_parameter_sweep(train_prices, config, sweep=SweepConfig())
    best = max(sweep_train, key=lambda r: r.sharpe)
    print(
        f"\nBest on train: lookback={best.lookback}, entry_z={best.entry_z}, "
        f"Sharpe={best.sharpe:.2f}"
    )

    # Apply best on test
    best_config = replace(config, lookback_hours=best.lookback, entry_z=best.entry_z)
    test_trades = run_backtest_all_pairs(test_prices, best_config)
    print("\nApplied to TEST:")
    report(f"Train-optimal (lb={best.lookback}, z={best.entry_z}) on test", test_trades)

    # Also show what the currently-configured params do on test only
    config_trades_test = run_backtest_all_pairs(test_prices, config)
    report(
        f"Current config (lb={config.lookback_hours}, z={config.entry_z}) on test",
        config_trades_test,
    )

    # ==== Parameter sensitivity ====
    print("\n" + "=" * 100)
    print("PARAMETER SENSITIVITY (full-period sweep around current config)")
    print("=" * 100)
    sweep_full = run_parameter_sweep(prices, config, sweep=SweepConfig())
    print(f"\n{'lookback':>10} {'entry_z':>8} {'trades':>7} {'net':>10} {'Sharpe':>7}")
    for row in sorted(sweep_full, key=lambda r: -r.sharpe):
        print(
            f"{row.lookback:>10} {row.entry_z:>8.1f} {row.trades:>7} "
            f"${row.net:>+8,.0f} {row.sharpe:>7.2f}"
        )


if __name__ == "__main__":
    main()
