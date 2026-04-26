"""Paper trading loop: poll prices, compute signals, execute orders."""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.live import Live

from hypemm.config import AppConfig
from hypemm.dashboard import build_dashboard
from hypemm.data import seed_price_buffer
from hypemm.engine import StrategyEngine
from hypemm.execution import ExecutionAdapter, PaperExecutionAdapter
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
from hypemm.risk import RiskReport, RiskStatus, compute_risk_report
from hypemm.signals import compute_pair_signal

console = Console()
logger = logging.getLogger(__name__)


def run_paper_loop(
    app: AppConfig,
    fresh: bool = False,
    adapter: ExecutionAdapter | None = None,
    live_mode: bool = False,
) -> None:
    """Run the paper trading monitor loop.

    By default, paper-trades against the Hyperliquid mid-price. Pass an explicit
    adapter (e.g. LiveExecutionAdapter) to switch execution mode. live_mode only
    affects dashboard styling — the live adapter is what actually places orders.
    """
    config = app.strategy
    infra = app.infra

    engine = StrategyEngine(config)
    owns_adapter = adapter is None
    if adapter is None:
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

    mode_label = "LIVE" if live_mode else "paper"
    logging.info("Starting %s trade monitor (Ctrl+C to stop)", mode_label)

    # Render the Live UI when we're in any terminal context — including a
    # tmux pane where stdout isn't a tty in the strict sense (it's a pipe to
    # tmux's pty multiplexer). The UI is still meaningful and the user wants
    # it visible when attached to the session.
    show_ui = sys.stdout.isatty() or bool(os.environ.get("TMUX"))

    try:
        if show_ui:
            ui_console = Console(force_terminal=True, file=sys.stdout)
            with Live(console=ui_console, refresh_per_second=0.5, screen=False) as live:
                _run_loop(
                    engine=engine,
                    adapter=adapter,
                    app=app,
                    buffer=buffer,
                    completed_trades=completed_trades,
                    state_path=state_path,
                    trades_path=trades_path,
                    snapshot_path=snapshot_path,
                    start_time=start_time,
                    live=live,
                    live_mode=live_mode,
                )
        else:
            logging.info("No interactive TTY detected, running headless %s loop", mode_label)
            _run_loop(
                engine=engine,
                adapter=adapter,
                app=app,
                buffer=buffer,
                completed_trades=completed_trades,
                state_path=state_path,
                trades_path=trades_path,
                snapshot_path=snapshot_path,
                start_time=start_time,
                live=None,
                live_mode=live_mode,
            )

    except KeyboardInterrupt:
        save_state(engine, state_path, start_time)
        logging.info("%s trading stopped. State saved.", mode_label)
    finally:
        if owns_adapter:
            adapter.close()


def _run_loop(
    *,
    engine: StrategyEngine,
    adapter: ExecutionAdapter,
    app: AppConfig,
    buffer: HourlyPriceBuffer,
    completed_trades: list[CompletedTrade],
    state_path: Path,
    trades_path: Path,
    snapshot_path: Path,
    start_time: str,
    live: Live | None,
    live_mode: bool,
) -> None:
    config = app.strategy
    infra = app.infra
    risk_cfg = app.risk
    last_risk_status: RiskStatus = RiskStatus.OK

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

        risk_report = compute_risk_report(
            engine,
            signals,
            completed_trades,
            risk_cfg,
            config.notional_per_leg,
            now_ms=now_ms,
        )
        engine.halt_entries = risk_report.halts_entry

        if risk_report.worst_status != last_risk_status:
            _log_risk_change(last_risk_status, risk_report)
            last_risk_status = risk_report.worst_status

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
                        order.pair,
                        order.position.direction,
                        config.notional_per_leg,
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

        if live is not None:
            n_bars = buffer.bar_count
            live.update(
                build_dashboard(
                    engine,
                    signals,
                    completed_trades,
                    config,
                    start_time,
                    risk_report=risk_report,
                    live_mode=live_mode,
                    poll_interval_sec=infra.poll_interval_sec,
                    n_bars=n_bars,
                )
            )
        time.sleep(infra.poll_interval_sec)


def _log_risk_change(prev: RiskStatus, report: RiskReport) -> None:
    """Emit a log line when the worst risk status changes."""
    triggers = [s for s in report.signals if s.status != RiskStatus.OK]
    if triggers:
        details = "; ".join(f"{s.name}={s.detail}" for s in triggers)
    else:
        details = "all signals OK"
    logger.warning("RISK %s -> %s :: %s", prev.value, report.worst_status.value, details)


def _accrue_funding(
    engine: StrategyEngine,
    adapter: ExecutionAdapter,
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
