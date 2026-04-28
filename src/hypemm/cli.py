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
    daily_pnl: dict[str, float] = {}
    num_trades: dict[str, int] = {}
    for t in sorted(trades, key=lambda tr: tr.exit_ts):
        day = datetime.fromtimestamp(t.exit_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        daily_pnl[day] = daily_pnl.get(day, 0.0) + t.net_pnl
        num_trades[day] = num_trades.get(day, 0) + 1

    rows: list[dict[str, object]] = []
    cumulative = 0.0
    for day in sorted(daily_pnl):
        cumulative += daily_pnl[day]
        rows.append(
            {
                "date": day,
                "daily_pnl": daily_pnl[day],
                "num_trades": num_trades[day],
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
        load_slippage_profile,
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

    # Auto-load slippage profile if it exists from `hypemm snapshot-slippage`
    profile = load_slippage_profile(
        app.infra.run_dir / "slippage_profile.json",
        percentile=args.slippage_percentile,
    )
    if profile:
        logging.info(
            "Using per-pair slippage profile (%s): %s",
            args.slippage_percentile,
            {k: round(v, 2) for k, v in profile.items()},
        )

    if args.sweep:
        run_parameter_sweep(prices, config, sweep=app.sweep, funding=funding)
        return

    n_days = (prices.index[-1] - prices.index[0]).days
    logging.info("%d hourly bars, %d days", len(prices), n_days)

    unfiltered_config = replace(config, corr_threshold=-1.0)
    trades_unfiltered = run_backtest_all_pairs(
        prices, unfiltered_config, funding=funding, slippage_profile=profile
    )
    bt_unfiltered = summarize_backtest(trades_unfiltered, prices)

    trades = run_backtest_all_pairs(prices, config, funding=funding, slippage_profile=profile)
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


def cmd_snapshot_slippage(args: argparse.Namespace) -> None:
    """Build a per-pair slippage profile by polling HL L2 books.

    Polls the L2 book for each coin once per `--interval` seconds for
    `--duration` minutes, walks the book at the configured notional in both
    directions, and saves the per-pair median + p90 slippage in bps to
    {run_dir}/slippage_profile.json. The backtest reads this file (when
    present) to apply realistic per-pair spread-crossing costs.
    """
    import statistics
    import time

    import httpx

    from hypemm.orderbook import InsufficientDepthError, book_vwap

    app = load_config(Path(args.config))
    coins = app.strategy.all_coins
    notional = app.strategy.notional_per_leg
    info_url = app.infra.rest_url
    poll_interval = args.interval
    duration = args.duration * 60

    samples: dict[str, list[float]] = {coin: [] for coin in coins}
    client = httpx.Client(timeout=10)
    deadline = time.monotonic() + duration
    n_polls = 0

    logging.info(
        "Snapshotting slippage at $%s notional for %d coins, %ds (interval %ss)",
        f"{notional:,.0f}",
        len(coins),
        duration,
        poll_interval,
    )
    try:
        while time.monotonic() < deadline:
            for coin in coins:
                try:
                    buy = book_vwap(client, info_url, coin, True, notional)
                    sell = book_vwap(client, info_url, coin, False, notional)
                    samples[coin].append((buy.slippage_bps + sell.slippage_bps) / 2)
                except (InsufficientDepthError, Exception) as e:
                    logging.warning("%s slip sample skipped: %s", coin, e)
            n_polls += 1
            time.sleep(poll_interval)
    finally:
        client.close()

    profile: dict[str, dict[str, float | int]] = {}
    for coin, vals in samples.items():
        if not vals:
            logging.warning("%s: no samples collected", coin)
            continue
        profile[coin] = {
            "median_bps": statistics.median(vals),
            "p90_bps": (statistics.quantiles(vals, n=10)[8] if len(vals) >= 10 else max(vals)),
            "max_bps": max(vals),
            "samples": len(vals),
        }

    out = {
        "notional_per_leg": notional,
        "polls": n_polls,
        "duration_seconds": duration,
        "pairs": profile,
    }
    out_path = app.infra.run_dir / "slippage_profile.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_report(out_path, out)
    logging.info("Saved slippage profile to %s", out_path)
    print()
    print(f"{'coin':<6} {'samples':>7} {'median bps':>11} {'p90 bps':>9} {'max bps':>9}")
    for coin, p in sorted(profile.items()):
        print(
            f"{coin:<6} {p['samples']:>7} {p['median_bps']:>11.2f} "
            f"{p['p90_bps']:>9.2f} {p['max_bps']:>9.2f}"
        )


def cmd_dashboard(args: argparse.Namespace) -> None:
    """Render the dashboard from on-disk runner artifacts.

    The dashboard process is fully decoupled from the runner: it reads
    state.json, paper_trades.csv, and latest_snapshot.csv (or
    hourly_snapshots.csv as fallback), reconstructs the snapshot, and
    re-renders Rich Live every --refresh seconds. Iterate freely on
    dashboard.py without restarting the runner.
    """
    import time

    from rich.console import Console
    from rich.live import Live

    from hypemm.dashboard import build_dashboard
    from hypemm.dashboard_loader import load_dashboard_snapshot

    app = load_config(Path(args.config))
    console = Console(force_terminal=True)

    if args.once:
        snapshot = load_dashboard_snapshot(app, fresh=args.fresh, trades_rows=args.trades_rows)
        console.print(build_dashboard(snapshot))
        return

    # screen=True opens an alternate-screen buffer. Rich's cursor-up redraw
    # in inline mode (screen=False) doesn't handle a frame whose height
    # grows or shrinks between updates — over SSH+tmux this manifests as
    # stacked Panel headers. Alt-screen mode draws fresh into a scratch
    # buffer; tmux restores the original pane content when the dashboard
    # exits cleanly.
    with Live(
        console=console,
        refresh_per_second=4,
        screen=True,
        auto_refresh=False,
        transient=False,
    ) as live:
        while True:
            try:
                snapshot = load_dashboard_snapshot(
                    app, fresh=args.fresh, trades_rows=args.trades_rows
                )
                live.update(build_dashboard(snapshot), refresh=True)
            except Exception as e:
                logging.warning("dashboard refresh skipped: %s", e)
            time.sleep(args.refresh)


def cmd_trades(args: argparse.Namespace) -> None:
    """Print the full completed-trades log with full datetimes, z-scores, and corr.

    The dashboard's live panel only shows the most recent N trades (alt-screen
    Live can't scroll). This command renders the full history into Rich's
    pager so you can scroll with `less` keybindings, or pipe elsewhere with
    --no-pager.
    """
    from rich.console import Console

    from hypemm.dashboard import build_trades_log_table
    from hypemm.persistence import load_trades

    app = load_config(Path(args.config))
    trades_path = app.infra.paper_trades_dir / "paper_trades.csv"
    trades = load_trades(trades_path)

    if not trades:
        print(f"No trades found at {trades_path}")
        return

    n = len(trades)
    title = (
        f"Completed Trades (last {args.tail} of {n})"
        if args.tail > 0
        else f"Completed Trades ({n} total)"
    )
    table = build_trades_log_table(
        trades,
        max_rows=args.tail if args.tail > 0 else None,
        title=title,
    )

    # Force a wide console so the date/z/corr columns render in full. The
    # pager (`less -RS`, Rich's default) lets users scroll horizontally;
    # piping to other tools also benefits from full-width rows.
    console = Console(width=140)
    if args.no_pager:
        console.print(table)
    else:
        with console.pager(styles=True):
            console.print(table)


def cmd_run(args: argparse.Namespace) -> None:
    """Start paper or live trading."""
    from dataclasses import replace as dc_replace

    from hypemm.execution import build_adapter
    from hypemm.runner import run_paper_loop

    app = load_config(Path(args.config))

    # In live mode, real fill prices already include spread-crossing, so
    # subtracting a simulated slippage on top would double-count. Zero it.
    # Paper / backtest keep the configured value to project realistic costs.
    if args.live and app.strategy.slippage_per_side_bps != 0.0:
        logging.info(
            "live mode: zeroing slippage_per_side_bps (was %.2f) — actual "
            "fills already reflect spread crossing",
            app.strategy.slippage_per_side_bps,
        )
        app = dc_replace(app, strategy=dc_replace(app.strategy, slippage_per_side_bps=0.0))

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
        notional = app.strategy.notional_per_leg
        n_legs = len(app.strategy.pairs) * 2
        max_margin = notional * n_legs / app.infra.leverage
        logging.warning(
            "LIVE MODE — orders will hit real Hyperliquid markets for the "
            "account in HYPERLIQUID_ACCOUNT. Sizing: $%.0f/leg × %d legs at %dx "
            "leverage → $%.0f max margin. See docs/LIVE_DEPLOYMENT.md.",
            notional,
            n_legs,
            app.infra.leverage,
            max_margin,
        )

    adapter = build_adapter(
        app.infra.rest_url,
        live=args.live,
        leverage=app.infra.leverage,
        is_cross_margin=app.infra.is_cross_margin,
        max_slippage_bps=app.infra.max_slippage_bps,
        ioc_aggression_bps=app.infra.ioc_aggression_bps,
        fill_poll_seconds=app.infra.fill_poll_seconds,
        fill_timeout_seconds=app.infra.fill_timeout_seconds,
    )
    run_paper_loop(
        app,
        fresh=args.fresh,
        adapter=adapter,
        live_mode=args.live,
        force_reconcile=args.force_reconcile,
    )


def main() -> None:
    """Main CLI entry point."""
    # Load .env from repo root so HYPERLIQUID_* and RPO_KEYSTORE_PWD don't have
    # to be re-exported every shell session. .env is gitignored.
    from dotenv import load_dotenv

    load_dotenv()
    _setup_logging()

    parser = argparse.ArgumentParser(
        prog="hypemm",
        description="Cross-perpetual statistical arbitrage on Hyperliquid",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    fetch_p = sub.add_parser("fetch", help="Fetch candle data")
    fetch_p.add_argument("--force", action="store_true", help="Re-fetch even if up-to-date")
    fetch_p.add_argument("--config", required=True, help="Config file path")
    fetch_p.set_defaults(func=cmd_fetch)

    bt_p = sub.add_parser("backtest", help="Run backtest")
    bt_p.add_argument("--sweep", action="store_true", help="Run parameter sweep")
    bt_p.add_argument("--config", required=True, help="Config file path")
    bt_p.add_argument(
        "--slippage-percentile",
        choices=["median_bps", "p90_bps", "max_bps"],
        default="median_bps",
        help="Which percentile to read from slippage_profile.json (default: median)",
    )
    bt_p.set_defaults(func=cmd_backtest)

    val_p = sub.add_parser("validate", help="Run validation gates")
    val_p.add_argument("--config", required=True, help="Config file path")
    val_p.set_defaults(func=cmd_validate)

    wf_p = sub.add_parser("walkforward", help="Run walk-forward validation")
    wf_p.add_argument("--config", required=True, help="Config file path")
    wf_p.add_argument("--train-years", type=int, default=2, help="Initial training window (years)")
    wf_p.add_argument("--test-months", type=int, default=12, help="Test window (months)")
    wf_p.add_argument("--step-months", type=int, default=12, help="Step between folds (months)")
    wf_p.set_defaults(func=cmd_walkforward)

    snap_p = sub.add_parser(
        "snapshot-slippage",
        help="Sample HL L2 books to build a per-pair slippage profile",
    )
    snap_p.add_argument("--config", required=True, help="Config file path")
    snap_p.add_argument(
        "--duration",
        type=int,
        default=10,
        help="Total polling duration in minutes (default: 10)",
    )
    snap_p.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Seconds between polls (default: 30)",
    )
    snap_p.set_defaults(func=cmd_snapshot_slippage)

    dash_p = sub.add_parser(
        "dashboard",
        help="Render the dashboard from on-disk runner artifacts (decoupled from runner)",
    )
    dash_p.add_argument("--config", required=True, help="Config file path")
    dash_p.add_argument(
        "--refresh",
        type=float,
        default=5.0,
        help="Seconds between dashboard refreshes (default: 5)",
    )
    dash_p.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore on-disk history; render an empty starting view",
    )
    dash_p.add_argument(
        "--once",
        action="store_true",
        help="Render once and exit (no Live loop) — useful for snapshots/CI",
    )
    dash_p.add_argument(
        "--trades-rows",
        type=int,
        default=15,
        help=(
            "How many recent completed trades to show in the live panel "
            "(default: 15). The full log is always available via `hypemm trades`."
        ),
    )
    dash_p.set_defaults(func=cmd_dashboard)

    trades_p = sub.add_parser(
        "trades",
        help="Print the full completed-trades log (paged, scrollable)",
    )
    trades_p.add_argument("--config", required=True, help="Config file path")
    trades_p.add_argument(
        "--tail",
        type=int,
        default=0,
        help="Show only the last N trades (0 = full log, default)",
    )
    trades_p.add_argument(
        "--no-pager",
        action="store_true",
        help="Disable the pager (just print to stdout)",
    )
    trades_p.set_defaults(func=cmd_trades)

    run_p = sub.add_parser("run", help="Start paper or live trading")
    run_p.add_argument("--fresh", action="store_true", help="Ignore saved state")
    run_p.add_argument("--config", required=True, help="Config file path")
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
    run_p.add_argument(
        "--force-reconcile",
        action="store_true",
        help=(
            "Live only: proceed even if engine state diverges from exchange "
            "positions. Engine state will be trusted; mismatched on-exchange "
            "positions will be ignored until they exit naturally"
        ),
    )
    run_p.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
