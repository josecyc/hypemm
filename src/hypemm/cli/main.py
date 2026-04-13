"""CLI entry points for hypemm."""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone

from rich.console import Console
from rich.live import Live

from hypemm.config import InfraConfig, StrategyConfig

console = Console()


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_fetch(args: argparse.Namespace) -> None:
    """Fetch candle data from Hyperliquid."""
    from hypemm.data.candles import fetch_all_candles

    config = StrategyConfig()
    infra = InfraConfig()
    fetch_all_candles(config.all_coins, infra, force=args.force)


def cmd_backtest(args: argparse.Namespace) -> None:
    """Run backtest with default or filtered parameters."""
    from hypemm.analysis.backtest import run_backtest_all_pairs
    from hypemm.analysis.stats import compute_sharpe, max_drawdown, monthly_breakdown
    from hypemm.data.loader import load_candles

    config = StrategyConfig()
    infra = InfraConfig()

    prices = load_candles(infra.candles_dir, config.all_coins)
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

    infra.reports_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "total_trades": len(trades),
        "total_net": net,
        "win_rate": wr,
        "sharpe": sharpe,
        "max_drawdown": dd,
        "monthly": months,
        "verdict": "PASS" if sharpe >= 1.0 else "FAIL",
    }
    with open(infra.reports_dir / "backtest_summary.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)


def cmd_sweep(args: argparse.Namespace) -> None:
    """Run parameter sweep."""
    from hypemm.analysis.sweep import run_parameter_sweep
    from hypemm.data.loader import load_candles

    config = StrategyConfig()
    infra = InfraConfig()
    prices = load_candles(infra.candles_dir, config.all_coins)
    run_parameter_sweep(prices, config)


def cmd_correlation(args: argparse.Namespace) -> None:
    """Run correlation stability analysis."""
    from hypemm.analysis.correlation import run_correlation_analysis
    from hypemm.data.loader import load_candles

    config = StrategyConfig()
    infra = InfraConfig()
    prices = load_candles(infra.candles_dir, config.all_coins)
    run_correlation_analysis(prices, config, infra.reports_dir)


def cmd_orderbook(args: argparse.Namespace) -> None:
    """Run live orderbook depth analysis."""
    from hypemm.analysis.orderbook import run_orderbook_analysis

    config = StrategyConfig()
    infra = InfraConfig()
    run_orderbook_analysis(config, infra)


def cmd_synthesize(args: argparse.Namespace) -> None:
    """Run go/no-go synthesis."""
    from hypemm.analysis.synthesize import run_synthesis

    config = StrategyConfig()
    infra = InfraConfig()
    verdict = run_synthesis(infra.reports_dir, config)
    logging.info("Final verdict: %s", verdict)


def cmd_paper(args: argparse.Namespace) -> None:
    """Start paper trading monitor."""
    from hypemm.dashboard.display import build_dashboard
    from hypemm.data.price_buffer import HourlyPriceBuffer
    from hypemm.execution.paper import PaperExecutionAdapter
    from hypemm.models import CompletedTrade, EntryOrder, ExitOrder
    from hypemm.persistence.state import load_state, save_state
    from hypemm.persistence.trade_log import load_trades, log_hourly_snapshot, log_trade
    from hypemm.strategy.engine import StrategyEngine
    from hypemm.strategy.signals import compute_pair_signal

    config = StrategyConfig()
    infra = InfraConfig()
    engine = StrategyEngine(config)
    adapter = PaperExecutionAdapter(infra.rest_url)
    state_path = infra.paper_trades_dir / "state.json"
    trades_path = infra.paper_trades_dir / "paper_trades.csv"
    snapshot_path = infra.paper_trades_dir / "hourly_snapshots.csv"
    start_time = datetime.now(timezone.utc).isoformat()

    # Resume state
    completed_trades: list[CompletedTrade] = []
    if not args.fresh and state_path.exists():
        start_time = load_state(engine, state_path)
        completed_trades = load_trades(trades_path)
        logging.info("Resumed with %d completed trades", len(completed_trades))

    # Seed price buffer
    from hypemm.data.candles import seed_price_buffer

    buffer = HourlyPriceBuffer(config.all_coins)
    seed_price_buffer(buffer, config, infra)

    last_signal_hour = -1
    logging.info("Starting paper trade monitor (Ctrl+C to stop)")

    try:
        with Live(console=console, refresh_per_second=0.5) as live:
            while True:
                # Fetch prices
                prices: dict[str, float] = {}
                for coin in config.all_coins:
                    try:
                        prices[coin] = adapter._fetch_mid(coin)
                    except Exception:
                        pass
                    time.sleep(0.3)

                now_ms = int(time.time() * 1000)
                epoch_hour = now_ms // 3_600_000
                for coin, price in prices.items():
                    buffer.update_live(coin, price, epoch_hour)
                hour_changed = buffer.advance_hour(epoch_hour)

                # Compute signals
                signals = {}
                for pair in config.pairs:
                    pa = buffer.get_prices(pair.coin_a)
                    pb = buffer.get_prices(pair.coin_b)
                    sig = compute_pair_signal(pa, pb, config, pair, now_ms)
                    if sig:
                        signals[pair.label] = sig

                # Process hourly
                current_hour = datetime.now(timezone.utc).hour
                if current_hour != last_signal_hour:
                    last_signal_hour = current_hour
                    orders = engine.process_bar(signals, now_ms)
                    for order in orders:
                        if isinstance(order, EntryOrder):
                            fa, fb = adapter.get_fill_prices(
                                order.pair, order.direction, config.notional_per_leg
                            )
                            engine.confirm_entry(order, fa, fb, now_ms)
                        elif isinstance(order, ExitOrder):
                            fa, fb = adapter.get_fill_prices(
                                order.pair, order.position.direction, config.notional_per_leg
                            )
                            trade = engine.confirm_exit(order, fa, fb, now_ms)
                            log_trade(trade, trades_path)
                            completed_trades.append(trade)

                    if hour_changed:
                        log_hourly_snapshot(engine, signals, config, snapshot_path)
                        save_state(engine, state_path, start_time)

                live.update(
                    build_dashboard(
                        engine,
                        signals,
                        completed_trades,
                        config,
                        start_time,
                    )
                )
                time.sleep(infra.poll_interval_sec)

    except KeyboardInterrupt:
        save_state(engine, state_path, start_time)
        logging.info("Paper trading stopped. State saved.")
    finally:
        adapter.close()


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
    fetch_p.set_defaults(func=cmd_fetch)

    bt_p = sub.add_parser("backtest", help="Run backtest")
    bt_p.set_defaults(func=cmd_backtest)

    sweep_p = sub.add_parser("sweep", help="Run parameter sweep")
    sweep_p.set_defaults(func=cmd_sweep)

    corr_p = sub.add_parser("correlation", help="Correlation analysis")
    corr_p.set_defaults(func=cmd_correlation)

    ob_p = sub.add_parser("orderbook", help="Orderbook depth analysis")
    ob_p.set_defaults(func=cmd_orderbook)

    syn_p = sub.add_parser("synthesize", help="Go/no-go synthesis")
    syn_p.set_defaults(func=cmd_synthesize)

    paper_p = sub.add_parser("paper", help="Start paper trading")
    paper_p.add_argument("--fresh", action="store_true", help="Ignore saved state")
    paper_p.set_defaults(func=cmd_paper)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
