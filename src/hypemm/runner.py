"""Paper / live trading loop. Headless: no terminal UI.

Persists state, trades, hourly snapshots, and a per-tick latest_snapshot.csv
to disk. Render the dashboard as a separate process via `hypemm dashboard`,
which reads from those files. This decoupling means the dashboard can be
iterated on, restarted, or run from a different machine without disturbing
the runner's price-buffer warmup or exchange-bound state.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from hypemm.config import AppConfig
from hypemm.data import seed_price_buffer
from hypemm.engine import StrategyEngine
from hypemm.execution import ExecutionAdapter, ExecutionError, PaperExecutionAdapter
from hypemm.funding import accrue_hourly_funding, fetch_latest_funding_rates
from hypemm.models import (
    CompletedTrade,
    DataFetchError,
    EntryOrder,
    ExitOrder,
    HypeMMError,
)
from hypemm.persistence import (
    load_state,
    load_trades,
    log_hourly_snapshot,
    log_trade,
    save_state,
    write_latest_snapshot,
)
from hypemm.price_buffer import HourlyPriceBuffer
from hypemm.risk import RiskReport, RiskStatus, compute_risk_report
from hypemm.signals import compute_pair_signal

logger = logging.getLogger(__name__)


def run_paper_loop(
    app: AppConfig,
    fresh: bool = False,
    adapter: ExecutionAdapter | None = None,
    live_mode: bool = False,
    force_reconcile: bool = False,
) -> None:
    """Run the trading monitor loop. Headless — no terminal UI.

    Pass an explicit adapter (e.g. LiveExecutionAdapter) to switch execution
    mode. live_mode flips the dashboard label that gets persisted in the
    latest snapshot, so a dashboard reader can render it appropriately.
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
    latest_path = infra.paper_trades_dir / "latest_snapshot.csv"
    mode_path = infra.paper_trades_dir / "mode.txt"
    start_time = datetime.now(timezone.utc).isoformat()

    completed_trades: list[CompletedTrade] = []
    if not fresh and state_path.exists():
        start_time = load_state(engine, state_path)
        completed_trades = load_trades(trades_path)
        logger.info("Resumed with %d completed trades", len(completed_trades))

    buffer = HourlyPriceBuffer(config.all_coins)
    seed_price_buffer(buffer, config, infra)

    # Persist mode for the dashboard reader to pick up
    mode_path.parent.mkdir(parents=True, exist_ok=True)
    mode_path.write_text("LIVE" if live_mode else "paper")

    mode_label = "LIVE" if live_mode else "paper"

    if live_mode:
        from hypemm.reconcile import reconcile

        if not hasattr(adapter, "fetch_user_state"):
            raise RuntimeError(
                "live mode requires an adapter with fetch_user_state; use LiveExecutionAdapter"
            )
        user_state = adapter.fetch_user_state()
        divergences = reconcile(engine, user_state, config.notional_per_leg)
        if divergences:
            for d in divergences:
                logger.error(
                    "RECONCILE divergence: %s expected %s %.6f, exchange has %.6f",
                    d.coin,
                    d.expected_direction,
                    d.expected_size,
                    d.actual_size,
                )
            if not force_reconcile:
                raise RuntimeError(
                    f"reconciliation found {len(divergences)} divergence(s) between "
                    "engine state and exchange. Re-run with --force-reconcile to "
                    "ignore (engine state will be trusted), or close exchange "
                    "positions manually and restart with --fresh."
                )
            logger.warning(
                "RECONCILE proceeding despite %d divergence(s) — engine state takes precedence",
                len(divergences),
            )

    logger.info("Starting %s trade monitor (Ctrl+C to stop)", mode_label)

    try:
        _run_loop(
            engine=engine,
            adapter=adapter,
            app=app,
            buffer=buffer,
            completed_trades=completed_trades,
            state_path=state_path,
            trades_path=trades_path,
            snapshot_path=snapshot_path,
            latest_path=latest_path,
            start_time=start_time,
        )
    except KeyboardInterrupt:
        save_state(engine, state_path, start_time)
        logger.info("%s trading stopped. State saved.", mode_label)
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
    latest_path: Path,
    start_time: str,
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
                logger.warning("Failed to fetch price for %s", coin)
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
                # Per-order isolation: an exchange rejection on one pair (e.g.
                # tick-size, depth, leverage) must NOT kill the runner — other
                # pairs still need their orders processed and the loop must
                # keep ticking. We log loudly and the engine simply doesn't
                # see a confirmation, leaving the position state unchanged.
                # An open ExitOrder that fails is the worst case: position
                # stays open another hour and is retried on the next bar.
                try:
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
                except ExecutionError as e:
                    pair_label = (
                        order.pair.label
                        if isinstance(order, EntryOrder)
                        else order.position.pair.label
                    )
                    logger.error(
                        "Order failed for %s (%s) — skipping this bar, runner continues: %s",
                        pair_label,
                        type(order).__name__,
                        e,
                    )

            log_hourly_snapshot(engine, signals, config, snapshot_path)
            save_state(engine, state_path, start_time)

        # Per-tick: refresh the dashboard's view of current signals & state
        write_latest_snapshot(engine, signals, config, latest_path)

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
