"""CLI entry point for hypemm."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from hypemm.config import load_config


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_fetch(args: argparse.Namespace) -> None:
    """Fetch candle data from Hyperliquid."""
    from hypemm.data import fetch_all_candles

    app = load_config(Path(args.config))
    fetch_all_candles(app.strategy.all_coins, app.infra, force=args.force)


def cmd_backtest(args: argparse.Namespace) -> None:
    """Run backtest, optionally sweeping parameters."""
    from hypemm.backtest import (
        compute_sharpe,
        max_drawdown,
        monthly_breakdown,
        run_backtest_all_pairs,
        run_parameter_sweep,
    )
    from hypemm.data import load_candles

    app = load_config(Path(args.config))
    config = app.strategy
    prices = load_candles(app.infra.candles_dir, config.all_coins)

    if args.sweep:
        run_parameter_sweep(prices, config, sweep=app.sweep)
        return

    n_days = (prices.index[-1] - prices.index[0]).days
    logging.info("%d hourly bars, %d days", len(prices), n_days)

    trades = run_backtest_all_pairs(prices, config)
    net = sum(t.net_pnl for t in trades)
    sharpe = compute_sharpe(trades)
    dd = max_drawdown(trades)
    wr = sum(1 for t in trades if t.net_pnl > 0) / len(trades) * 100 if trades else 0

    logging.info(
        "TOTAL: %d trades, %.0f%% WR, $%+,.0f, Sharpe %.2f, Max DD $%,.0f",
        len(trades),
        wr,
        net,
        sharpe,
        dd,
    )

    months = monthly_breakdown(trades)
    for m in months:
        logging.info(
            "%s: %s trades, $%+,.0f net",
            m["month"],
            m["trades"],
            float(str(m["net"])),
        )

    app.infra.reports_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "total_trades": len(trades),
        "total_net": net,
        "win_rate": wr,
        "sharpe": sharpe,
        "max_drawdown": dd,
        "monthly": months,
        "verdict": "PASS" if sharpe >= app.gates.min_sharpe else "FAIL",
    }
    with open(app.infra.reports_dir / "backtest_summary.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)


def cmd_validate(args: argparse.Namespace) -> None:
    """Run validation gates."""
    from hypemm.data import load_candles
    from hypemm.validate import run_validation

    app = load_config(Path(args.config))
    config = app.strategy
    prices = load_candles(app.infra.candles_dir, config.all_coins)

    results = run_validation(prices, config, app.infra, app.gates)
    n_pass = sum(1 for g in results if g.passed)
    n_total = len(results)

    if n_pass == 3:
        logging.info("Final verdict: GO (%d/%d gates passed)", n_pass, n_total)
    elif n_pass == 0:
        logging.info("Final verdict: NO-GO (%d/%d gates passed)", n_pass, n_total)
    else:
        logging.info("Final verdict: CONDITIONAL (%d/%d gates passed)", n_pass, n_total)


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
