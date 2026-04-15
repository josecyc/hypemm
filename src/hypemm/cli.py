"""CLI entry point for hypemm."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from hypemm.config import load_config


def _write_report(path: Path, data: dict[str, object]) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
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
    _write_report(
        app.infra.reports_dir / "backtest_summary.json",
        {
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


def cmd_run(args: argparse.Namespace) -> None:
    """Start paper or live trading."""
    from hypemm.runner import run_paper_loop

    app = load_config(Path(args.config))
    run_paper_loop(app, fresh=args.fresh)


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

    run_p = sub.add_parser("run", help="Start paper trading")
    run_p.add_argument("--fresh", action="store_true", help="Ignore saved state")
    run_p.add_argument("--config", default="config.toml", help="Config file path")
    run_p.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
