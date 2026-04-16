"""Paper trading loop: poll prices, compute signals, execute orders."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime, timezone

from rich.console import Console
from rich.live import Live

from hypemm.config import AppConfig
from hypemm.dashboard import build_dashboard
from hypemm.data import seed_price_buffer
from hypemm.engine import StrategyEngine
from hypemm.execution import PaperExecutionAdapter
from hypemm.funding import accrue_hourly_funding, fetch_latest_funding_rates
from hypemm.models import (
    CompletedTrade,
    DataFetchError,
    EntryOrder,
    ExitOrder,
    HypeMMError,
)
from hypemm.persistence import load_state, load_trades, log_hourly_snapshot, log_trade, save_state
from hypemm.price_buffer import HourlyPriceBuffer
from hypemm.signals import compute_pair_signal

console = Console()
logger = logging.getLogger(__name__)


def run_paper_loop(app: AppConfig, fresh: bool = False) -> None:
    """Run the paper trading monitor loop."""
    config = app.strategy
    infra = app.infra

    engine = StrategyEngine(config)
    adapter = PaperExecutionAdapter(infra.rest_url)
    state_path = infra.paper_trades_dir / "state.json"
    trades_path = infra.paper_trades_dir / "paper_trades.csv"
    snapshot_path = infra.paper_trades_dir / "hourly_snapshots.csv"
    start_time = datetime.now(timezone.utc).isoformat()

    completed_trades: list[CompletedTrade] = []
    if not fresh and state_path.exists():
        start_time = load_state(engine, state_path)
        completed_trades = load_trades(trades_path)
        logging.info("Resumed with %d completed trades", len(completed_trades))

    buffer = HourlyPriceBuffer(config.all_coins)
    seed_price_buffer(buffer, config, infra)

    logging.info("Starting paper trade monitor (Ctrl+C to stop)")

    try:
        with Live(console=console, refresh_per_second=0.5) as live:
            while True:
                prices: dict[str, float] = {}
                for coin in config.all_coins:
                    try:
                        prices[coin] = adapter.fetch_mid(coin)
                    except DataFetchError:
                        logging.warning("Failed to fetch price for %s", coin)
                    time.sleep(0.3)

                now_ms = int(time.time() * 1000)
                epoch_hour = now_ms // 3_600_000
                for coin, price in prices.items():
                    buffer.update_live(coin, price, epoch_hour)
                hour_changed = buffer.advance_hour(epoch_hour)

                signals = {}
                for pair in config.pairs:
                    pa = buffer.get_prices(pair.coin_a)
                    pb = buffer.get_prices(pair.coin_b)
                    sig = compute_pair_signal(pa, pb, config, pair, now_ms)
                    if sig:
                        signals[pair.label] = sig

                if hour_changed:
                    _accrue_funding(engine, adapter, config.all_coins, config.notional_per_leg)
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
                            accrued = order.position.funding_paid
                            trade = engine.confirm_exit(order, fa, fb, now_ms)
                            if accrued != 0.0:
                                trade = replace(
                                    trade,
                                    funding_cost=accrued,
                                    net_pnl=trade.net_pnl - accrued,
                                )
                            log_trade(trade, trades_path)
                            completed_trades.append(trade)

                    log_hourly_snapshot(engine, signals, config, snapshot_path)
                    save_state(engine, state_path, start_time)

                live.update(build_dashboard(engine, signals, completed_trades, config, start_time))
                time.sleep(infra.poll_interval_sec)

    except KeyboardInterrupt:
        save_state(engine, state_path, start_time)
        logging.info("Paper trading stopped. State saved.")
    finally:
        adapter.close()


def _accrue_funding(
    engine: StrategyEngine,
    adapter: PaperExecutionAdapter,
    coins: list[str],
    notional: float,
) -> None:
    """Fetch latest funding rates and accrue one hour on each open position."""
    if not any(p is not None for p in engine.positions.values()):
        return
    try:
        rates = fetch_latest_funding_rates(adapter.client, adapter.rest_url, coins)
    except HypeMMError as e:
        logger.warning("Funding accrual skipped: %s", e)
        return
    accrue_hourly_funding(engine.positions, rates, notional)
