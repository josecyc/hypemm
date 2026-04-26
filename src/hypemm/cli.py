"""CLI entry point for hypemm."""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from hypemm.config import load_config
from hypemm.models import CompletedTrade


def _write_report(path: Path, data: dict[str, object]) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _save_trades_csv(path: Path, trades: list[CompletedTrade]) -> None:
    import csv

    if not trades:
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "pair",
                    "direction",
                    "entry_ts",
                    "exit_ts",
                    "entry_z",
                    "exit_z",
                    "hours_held",
                    "entry_price_a",
                    "entry_price_b",
                    "exit_price_a",
                    "exit_price_b",
                    "pnl_leg_a",
                    "pnl_leg_b",
                    "gross_pnl",
                    "cost",
                    "funding_cost",
                    "net_pnl",
                    "max_adverse_excursion",
                    "exit_reason",
                    "entry_correlation",
                ],
            )
            writer.writeheader()
        return

    rows: list[dict[str, object]] = []
    for t in trades:
        row = asdict(t)
        row["pair"] = row.pop("pair_label")
        row["direction"] = t.direction.label
        row["exit_reason"] = str(t.exit_reason)
        rows.append(row)

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _daily_equity_rows(trades: list[CompletedTrade]) -> list[dict[str, object]]:
    daily: dict[str, dict[str, object]] = {}
    cumulative = 0.0
    for t in sorted(trades, key=lambda tr: tr.exit_ts):
        day = datetime.fromtimestamp(t.exit_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if day not in daily:
            daily[day] = {"date": day, "daily_pnl": 0.0, "num_trades": 0}
        daily[day]["daily_pnl"] = float(daily[day]["daily_pnl"]) + t.net_pnl
        daily[day]["num_trades"] = int(daily[day]["num_trades"]) + 1

    rows: list[dict[str, object]] = []
    for day in sorted(daily):
        cumulative += float(daily[day]["daily_pnl"])
        rows.append(
            {
                "date": day,
                "daily_pnl": float(daily[day]["daily_pnl"]),
                "num_trades": int(daily[day]["num_trades"]),
                "cumulative_pnl": cumulative,
            }
        )
    return rows


def _save_daily_equity_csv(path: Path, trades: list[CompletedTrade]) -> None:
    import csv

    rows = _daily_equity_rows(trades)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["date", "daily_pnl", "num_trades", "cumulative_pnl"],
        )
        writer.writeheader()
        writer.writerows(rows)


def _setup_logging(log_file: str | None = None) -> None:
    """Configure logging.

    By default writes to stderr. When `log_file` is given, also tees to that
    file — used by the live runner so the Rich UI stays clean while keeping
    a persistent record of HTTP calls and risk events.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def cmd_fetch(args: argparse.Namespace) -> None:
    """Fetch candle and funding data from Hyperliquid."""
    from hypemm.data import fetch_all_candles
    from hypemm.funding import fetch_all_funding

    app = load_config(Path(args.config))
    fetch_all_candles(app.strategy.all_coins, app.infra, force=args.force)
    fetch_all_funding(app.strategy.all_coins, app.infra, force=args.force)


def cmd_backtest(args: argparse.Namespace) -> None:
    """Run backtest with Gate 1 (Sharpe) and Gate 2 (correlation) checks."""
    from hypemm.backtest import (
        check_backtest_gate,
        run_backtest_all_pairs,
        run_parameter_sweep,
        summarize_backtest,
    )
    from hypemm.correlation import check_correlation_gate, compute_correlation_stability
    from hypemm.data import load_candles
    from hypemm.funding import load_funding

    app = load_config(Path(args.config))
    config = app.strategy
    prices = load_candles(app.infra.candles_dir, config.all_coins)
    funding = load_funding(app.infra.funding_dir, config.all_coins)

    if args.sweep:
        run_parameter_sweep(prices, config, sweep=app.sweep, funding=funding)
        return

    n_days = (prices.index[-1] - prices.index[0]).days
    logging.info("%d hourly bars, %d days", len(prices), n_days)

    unfiltered_config = replace(config, corr_threshold=-1.0)
    trades_unfiltered = run_backtest_all_pairs(prices, unfiltered_config, funding=funding)
    bt_unfiltered = summarize_backtest(trades_unfiltered, prices)

    trades = run_backtest_all_pairs(prices, config, funding=funding)
    bt_result = summarize_backtest(trades, prices)

    logging.info(
        "TOTAL: %d trades, %.0f%% WR, $%+.0f, Sharpe %.2f, Max DD $%.0f",
        len(trades),
        bt_result.win_rate,
        bt_result.total_net,
        bt_result.sharpe,
        bt_result.max_drawdown,
    )
    for m in bt_result.monthly:
        logging.info(
            "%s: %s trades, $%+.0f net",
            m["month"],
            m["trades"],
            float(str(m["net"])),
        )

    gate1 = check_backtest_gate(bt_result, app.gates)
    logging.info("Gate 1 (Backtest): %s", gate1.detail)

    regimes, breakdowns = compute_correlation_stability(prices, config)
    gate2 = check_correlation_gate(regimes, breakdowns, app.gates)
    logging.info("Gate 2 (Correlation): %s", gate2.detail)

    app.infra.reports_dir.mkdir(parents=True, exist_ok=True)
    _save_trades_csv(app.infra.reports_dir / "backtest_trades.csv", trades_unfiltered)
    _save_trades_csv(app.infra.reports_dir / "backtest_trades_filtered.csv", trades)
    _save_daily_equity_csv(app.infra.reports_dir / "daily_equity.csv", trades_unfiltered)
    _save_daily_equity_csv(app.infra.reports_dir / "daily_equity_filtered.csv", trades)
    _write_report(
        app.infra.reports_dir / "backtest_summary.json",
        {
            "date_range": (
                f"{prices.index[0].strftime('%Y-%m-%d')}"
                f" → {prices.index[-1].strftime('%Y-%m-%d')}"
            ),
            "n_days": n_days,
            "unfiltered_total_trades": len(trades_unfiltered),
            "unfiltered_total_net": bt_unfiltered.total_net,
            "unfiltered_win_rate": bt_unfiltered.win_rate,
            "unfiltered_sharpe": bt_unfiltered.sharpe,
            "unfiltered_max_drawdown": bt_unfiltered.max_drawdown,
            "unfiltered_monthly": bt_unfiltered.monthly,
            "total_trades": len(trades),
            "total_net": bt_result.total_net,
            "win_rate": bt_result.win_rate,
            "sharpe": bt_result.sharpe,
            "max_drawdown": bt_result.max_drawdown,
            "monthly": bt_result.monthly,
            "verdict": gate1.verdict,
        },
    )
    _write_report(
        app.infra.reports_dir / "correlation_analysis.json",
        {
            "regimes": regimes,
            "breakdowns": breakdowns,
            "verdict": gate2.verdict,
        },
    )


def cmd_validate(args: argparse.Namespace) -> None:
    """Run Gate 3 (orderbook) and produce final GO/NO-GO verdict."""
    from hypemm.validate import (
        check_orderbook_gate,
        collect_orderbook_data,
        run_synthesis,
    )

    app = load_config(Path(args.config))
    reports = app.infra.reports_dir

    required = ["backtest_summary.json", "correlation_analysis.json"]
    missing = [f for f in required if not (reports / f).exists()]
    if missing:
        logging.error(
            "Missing prerequisite files: %s. Run 'hypemm backtest' first.",
            ", ".join(missing),
        )
        raise SystemExit(1)

    logging.info("=== Gate 3: Orderbook ===")
    coin_stats, pair_viability = collect_orderbook_data(app.strategy, app.infra, app.gates)
    gate3 = check_orderbook_gate(coin_stats, pair_viability, app.gates)

    _write_report(
        reports / "orderbook_analysis.json",
        {
            "coin_stats": {k: dict(v) for k, v in coin_stats.items()},
            "pair_viability": pair_viability,
            "verdict": gate3.verdict,
        },
    )

    overall = run_synthesis(reports)
    logging.info("Final verdict: %s", overall)


def cmd_walkforward(args: argparse.Namespace) -> None:
    """Run walk-forward validation with statistical metrics."""
    from hypemm.data import load_candles
    from hypemm.funding import load_funding
    from hypemm.walkforward import run_walk_forward

    app = load_config(Path(args.config))
    config = app.strategy
    prices = load_candles(app.infra.candles_dir, config.all_coins)
    funding = load_funding(app.infra.funding_dir, config.all_coins)

    n_days = (prices.index[-1] - prices.index[0]).days
    logging.info(
        "Walk-forward: %d bars, %d days (%s → %s)",
        len(prices),
        n_days,
        prices.index[0].strftime("%Y-%m-%d"),
        prices.index[-1].strftime("%Y-%m-%d"),
    )

    result = run_walk_forward(
        prices,
        config,
        funding=funding,
        train_years=args.train_years,
        test_months=args.test_months,
        step_months=args.step_months,
    )

    # Print per-fold results
    logging.info("")
    logging.info("=" * 70)
    logging.info("WALK-FORWARD RESULTS")
    logging.info("=" * 70)
    for w in result.windows:
        logging.info(
            "Fold %d: train [%s → %s] SR %.2f ($%+.0f) | "
            "test [%s → %s] %d trades, SR %.2f, $%+.0f, "
            "WR %.0f%%, DD $%.0f, $/day $%.0f",
            w.fold,
            w.train_start,
            w.train_end,
            w.train_sharpe,
            w.train_net,
            w.test_start,
            w.test_end,
            w.test_trades,
            w.test_sharpe,
            w.test_net,
            w.test_win_rate,
            w.test_max_dd,
            w.test_daily_avg,
        )

    logging.info("")
    logging.info("--- Aggregate OOS ---")
    logging.info("  Trades:     %d", result.oos_trades)
    logging.info("  Net P&L:    $%+.0f", result.oos_net)
    logging.info("  Sharpe:     %.2f", result.oos_sharpe)
    logging.info("  Win rate:   %.1f%%", result.oos_win_rate)
    logging.info("  Max DD:     $%.0f", result.oos_max_dd)
    logging.info("  Daily avg:  $%.0f", result.oos_daily_avg)

    logging.info("")
    logging.info("--- Statistical Robustness ---")
    logging.info("  PSR (vs SR=0):   %.1f%%", result.psr * 100)
    logging.info(
        "  DSR (%d trials):  %.1f%%",
        45 * 9,
        result.dsr * 100,
    )
    logging.info("  CVaR 95%%:        $%.0f", result.cvar_95)
    logging.info("  CVaR 99%%:        $%.0f", result.cvar_99)
    logging.info("  Sortino:         %.2f", result.sortino)
    logging.info("  Skewness:        %.2f", result.skewness)
    logging.info("  Kurtosis:        %.2f", result.kurtosis)

    # Verdict
    logging.info("")
    dsr_pct = result.dsr * 100
    if result.dsr > 0.95:
        logging.info("VERDICT: Edge survives deflation (DSR > 95%%). GO.")
    elif result.dsr > 0.85:
        logging.info(
            "VERDICT: Plausible but not confirmed (DSR %.0f%%)." " CAUTIOUS GO.",
            dsr_pct,
        )
    elif result.oos_sharpe > 0.5:
        logging.info(
            "VERDICT: Weak edge (DSR %.0f%%, OOS SR %.2f)." " REDUCE SIZE.",
            dsr_pct,
            result.oos_sharpe,
        )
    else:
        logging.info("VERDICT: No reliable edge found out-of-sample. NO GO.")

    # Save report
    app.infra.reports_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "config": args.config,
        "train_years": args.train_years,
        "test_months": args.test_months,
        "step_months": args.step_months,
        "n_folds": len(result.windows),
        "oos_trades": result.oos_trades,
        "oos_net": result.oos_net,
        "oos_sharpe": result.oos_sharpe,
        "oos_win_rate": result.oos_win_rate,
        "oos_max_dd": result.oos_max_dd,
        "oos_daily_avg": result.oos_daily_avg,
        "psr": result.psr,
        "dsr": result.dsr,
        "cvar_95": result.cvar_95,
        "cvar_99": result.cvar_99,
        "sortino": result.sortino,
        "skewness": result.skewness,
        "kurtosis": result.kurtosis,
        "windows": [
            {
                "fold": w.fold,
                "train": f"{w.train_start} → {w.train_end}",
                "test": f"{w.test_start} → {w.test_end}",
                "train_trades": w.train_trades,
                "train_sharpe": w.train_sharpe,
                "train_net": w.train_net,
                "test_trades": w.test_trades,
                "test_sharpe": w.test_sharpe,
                "test_net": w.test_net,
                "test_win_rate": w.test_win_rate,
                "test_max_dd": w.test_max_dd,
                "test_daily_avg": w.test_daily_avg,
            }
            for w in result.windows
        ],
    }
    _write_report(app.infra.reports_dir / "walkforward_report.json", report)
    logging.info("Report saved to %s", app.infra.reports_dir / "walkforward_report.json")


def cmd_run(args: argparse.Namespace) -> None:
    """Start paper or live trading."""
    from hypemm.execution import build_adapter
    from hypemm.runner import run_paper_loop

    app = load_config(Path(args.config))

    # Reroute logs to a file when --log-file is given so they don't smear over
    # the Rich Live dashboard. The runner re-renders in place; log lines printed
    # to stderr would interleave with frames and cause flicker.
    if args.log_file:
        log_path = Path(args.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        for h in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(h)
        logging.getLogger().addHandler(logging.FileHandler(log_path))
        logging.getLogger().setLevel(logging.INFO)
        # Inherit the existing format
        for h in logging.getLogger().handlers:
            h.setFormatter(
                logging.Formatter(
                    fmt="%(asctime)s [%(name)s] %(message)s",
                    datefmt="%H:%M:%S",
                )
            )

    if args.live:
        if not args.confirm_live:
            raise SystemExit(
                "live trading must be confirmed with --confirm-live "
                "(this will place real orders against the configured account)"
            )
        logging.warning(
            "LIVE MODE — orders will hit real Hyperliquid markets for the "
            "account in HYPERLIQUID_ACCOUNT. Capital recommended: $120K at 5x. "
            "See docs/LIVE_DEPLOYMENT.md."
        )

    adapter = build_adapter(app.infra.rest_url, live=args.live)
    run_paper_loop(app, fresh=args.fresh, adapter=adapter, live_mode=args.live)


def main() -> None:
    """Main CLI entry point."""
    _setup_logging()

    parser = argparse.ArgumentParser(
        prog="hypemm",
        description="Cross-perpetual statistical arbitrage on Hyperliquid",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    fetch_p = sub.add_parser("fetch", help="Fetch candle data")
    fetch_p.add_argument("--force", action="store_true", help="Re-fetch even if up-to-date")
    fetch_p.add_argument("--config", default="config.toml", help="Config file path")
    fetch_p.set_defaults(func=cmd_fetch)

    bt_p = sub.add_parser("backtest", help="Run backtest")
    bt_p.add_argument("--sweep", action="store_true", help="Run parameter sweep")
    bt_p.add_argument("--config", default="config.toml", help="Config file path")
    bt_p.set_defaults(func=cmd_backtest)

    val_p = sub.add_parser("validate", help="Run validation gates")
    val_p.add_argument("--config", default="config.toml", help="Config file path")
    val_p.set_defaults(func=cmd_validate)

    wf_p = sub.add_parser("walkforward", help="Run walk-forward validation")
    wf_p.add_argument("--config", default="config_binance_6y.toml", help="Config file path")
    wf_p.add_argument("--train-years", type=int, default=2, help="Initial training window (years)")
    wf_p.add_argument("--test-months", type=int, default=12, help="Test window (months)")
    wf_p.add_argument("--step-months", type=int, default=12, help="Step between folds (months)")
    wf_p.set_defaults(func=cmd_walkforward)

    run_p = sub.add_parser("run", help="Start paper or live trading")
    run_p.add_argument("--fresh", action="store_true", help="Ignore saved state")
    run_p.add_argument("--config", default="config.toml", help="Config file path")
    run_p.add_argument(
        "--live",
        action="store_true",
        help=(
            "Trade with real money via LiveExecutionAdapter "
            "(requires HYPERLIQUID_PRIVATE_KEY + HYPERLIQUID_ACCOUNT)"
        ),
    )
    run_p.add_argument(
        "--confirm-live",
        action="store_true",
        help="Required alongside --live to acknowledge real-money execution",
    )
    run_p.add_argument(
        "--log-file",
        default=None,
        help=(
            "Route logs to this file instead of stderr — keeps the Rich "
            "dashboard frame clean inside a tmux pane"
        ),
    )
    run_p.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
