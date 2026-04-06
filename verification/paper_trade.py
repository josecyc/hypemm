#!/usr/bin/env python3
"""Paper trading monitor for the cross-perp stat arb strategy.

Connects live to Hyperliquid, computes signals in real-time, logs
theoretical trades, and tracks P&L.

Usage:
    python -m verification.paper_trade
"""
from __future__ import annotations

import csv
import json
import math
import time
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from verification.config import (
    COOLDOWN_HOURS,
    CORR_HIGH,
    CORR_WINDOW_HOURS,
    COST_PER_SIDE_BPS,
    DATA_DIR,
    ENTRY_Z,
    EXIT_Z,
    LOOKBACK_HOURS,
    MAX_HOLD_HOURS,
    NOTIONAL_PER_LEG,
    REST_URL,
    STOP_LOSS_Z,
)

console = Console()

# Pairs to trade (filtered to the best ones)
PAIRS = [
    ("LINK", "SOL"),
    ("DOGE", "AVAX"),
    ("SOL", "AVAX"),
    ("BTC", "SOL"),
]

COINS = list(set(c for p in PAIRS for c in p))
POLL_INTERVAL_SEC = 60  # check every minute
LOG_DIR = DATA_DIR / "paper_trades"
STATE_FILE = LOG_DIR / "state.json"

# Leverage simulation (display only — does not affect P&L calculation)
DEFAULT_LEVERAGE = 5


@dataclass
class Position:
    pair: str
    direction: int         # 1 = long ratio, -1 = short ratio
    entry_z: float
    entry_price_a: float
    entry_price_b: float
    entry_time: str
    entry_corr: float
    hours_held: int = 0


@dataclass
class PaperTrade:
    pair: str
    direction: str
    entry_time: str
    exit_time: str
    entry_z: float
    exit_z: float
    hours_held: int
    entry_price_a: float
    entry_price_b: float
    exit_price_a: float
    exit_price_b: float
    gross_pnl: float
    cost: float
    net_pnl: float
    entry_corr: float
    exit_reason: str


class HourlyPriceBuffer:
    """Buffer of hourly close prices.

    The backtest uses hourly candle closes — one data point per hour.
    This buffer replicates that: the seed phase loads historical hourly
    candles, and the live phase updates only the *current* (latest) bar
    in-place on each poll.  A new bar is appended only when the hour
    rolls over, keeping the timescale identical to the backtest.

    Tracks the actual UTC hour of the last appended bar using the
    candle timestamp (not wall-clock), so the seed-to-live handoff
    is correct regardless of when the process starts within an hour.
    """

    def __init__(self, max_hours: int = 300):
        self.prices: dict[str, deque[float]] = {c: deque(maxlen=max_hours) for c in COINS}
        self.last_bar_hour: int = -1  # UTC hour of the latest bar in the buffer

    def seed(self, coin: str, hourly_closes: list[float], last_candle_open_hour: int) -> None:
        """Bulk-load historical hourly closes.

        last_candle_open_hour is the UTC hour of the final candle's
        OPEN time (e.g. 19 for the 19:00-20:00 bar).  We store it
        directly: the bar "belongs" to its open hour, and live polls
        at the same wall-clock hour should overwrite it, while a new
        wall-clock hour should append.
        """
        for px in hourly_closes:
            self.prices[coin].append(px)
        self.last_bar_hour = last_candle_open_hour

    def update_live(self, coin: str, price: float, utc_hour: int) -> None:
        """Update with a live mid-price.

        If utc_hour matches last_bar_hour, overwrite the latest bar
        (intra-hour update). Otherwise, append a new bar for the new hour.
        """
        buf = self.prices[coin]
        if not buf:
            buf.append(price)
            return

        if utc_hour != self.last_bar_hour:
            buf.append(price)
        else:
            buf[-1] = price

    def advance_hour(self, utc_hour: int) -> bool:
        """Record the current hour. Returns True if the hour changed."""
        changed = utc_hour != self.last_bar_hour
        self.last_bar_hour = utc_hour
        return changed


class StatArbMonitor:
    def __init__(self):
        self.buffer = HourlyPriceBuffer()
        self.positions: dict[str, Position | None] = {f"{a}/{b}": None for a, b in PAIRS}
        self.cooldowns: dict[str, int] = {f"{a}/{b}": 0 for a, b in PAIRS}
        self.trades: list[PaperTrade] = []
        self.tick = 0
        self.hours_elapsed = 0       # counts hourly ticks for hold time
        self.last_signal_hour = -1    # last UTC hour we ran signal logic
        self.client = httpx.Client(timeout=10)
        self.signals: dict[str, dict] = {}
        self.start_time: str = datetime.now(timezone.utc).isoformat()

    def fetch_prices(self) -> dict[str, float]:
        """Fetch current mid prices for all coins."""
        prices = {}
        for coin in COINS:
            try:
                r = self.client.post(REST_URL, json={"type": "l2Book", "coin": coin})
                data = r.json()
                levels = data.get("levels", [])
                if len(levels) >= 2 and levels[0] and levels[1]:
                    bid = float(levels[0][0]["px"])
                    ask = float(levels[1][0]["px"])
                    prices[coin] = (bid + ask) / 2
            except Exception:
                pass
            time.sleep(0.3)
        return prices

    def compute_signals(self) -> dict[str, dict]:
        """Compute z-scores and correlations for all pairs.

        Uses the hourly buffer — each element is one hourly close,
        matching the backtest timescale exactly.
        """
        signals = {}
        for coin_a, coin_b in PAIRS:
            label = f"{coin_a}/{coin_b}"
            pa = list(self.buffer.prices[coin_a])
            pb = list(self.buffer.prices[coin_b])

            n = min(len(pa), len(pb))
            if n < LOOKBACK_HOURS + 1:
                signals[label] = {"z": None, "corr": None, "status": f"warming up ({n}/{LOOKBACK_HOURS}h)"}
                continue

            pa_arr = np.array(pa[-n:])
            pb_arr = np.array(pb[-n:])
            log_ratio = np.log(pa_arr / pb_arr)

            # Z-score: rolling window over the PREVIOUS lookback bars,
            # then score the current bar against that window.
            window = log_ratio[-(LOOKBACK_HOURS + 1):-1]
            mean = np.mean(window)
            std = np.std(window, ddof=1)
            z = (log_ratio[-1] - mean) / std if std > 1e-10 else 0

            # Correlation of hourly returns over trailing 7 days
            corr = None
            if n >= CORR_WINDOW_HOURS + 1:
                ret_a = np.diff(np.log(pa_arr[-(CORR_WINDOW_HOURS + 1):]))
                ret_b = np.diff(np.log(pb_arr[-(CORR_WINDOW_HOURS + 1):]))
                if len(ret_a) == len(ret_b) and len(ret_a) > 5:
                    corr = float(np.corrcoef(ret_a, ret_b)[0, 1])

            signals[label] = {
                "z": z,
                "corr": corr,
                "price_a": pa_arr[-1],
                "price_b": pb_arr[-1],
                "status": "ready",
                "n_bars": n,
            }

        self.signals = signals
        return signals

    def is_new_hour(self) -> bool:
        """Check if the UTC hour has changed since last signal run."""
        current = datetime.now(timezone.utc).hour
        return current != self.last_signal_hour

    def process_signals(self) -> list[str]:
        """Check for entry/exit signals and manage positions.

        Only runs entry/exit logic when the hour rolls over, matching
        the backtest's hourly decision cadence.  Positions' hours_held
        increments once per hour, not once per poll.
        """
        now_hour = datetime.now(timezone.utc).hour
        new_hour = now_hour != self.last_signal_hour
        if not new_hour:
            return []  # No action until the hour ticks

        self.last_signal_hour = now_hour

        actions = []
        notional = NOTIONAL_PER_LEG
        rt_cost = notional * 2 * COST_PER_SIDE_BPS / 10_000 * 2

        for (coin_a, coin_b) in PAIRS:
            label = f"{coin_a}/{coin_b}"
            sig = self.signals.get(label, {})
            z = sig.get("z")
            corr = sig.get("corr")
            if z is None:
                continue

            pos = self.positions[label]

            if pos is None:
                # Check cooldown
                if self.cooldowns[label] > 0:
                    self.cooldowns[label] -= 1
                    continue

                # Correlation gate
                if corr is None or corr < CORR_HIGH:
                    continue

                # Entry
                if z > ENTRY_Z:
                    pos = Position(
                        pair=label, direction=-1, entry_z=z,
                        entry_price_a=sig["price_a"], entry_price_b=sig["price_b"],
                        entry_time=datetime.now(timezone.utc).isoformat(),
                        entry_corr=corr,
                    )
                    self.positions[label] = pos
                    actions.append(f"ENTER {label} SHORT_RATIO z={z:+.2f} corr={corr:.3f}")

                elif z < -ENTRY_Z:
                    pos = Position(
                        pair=label, direction=1, entry_z=z,
                        entry_price_a=sig["price_a"], entry_price_b=sig["price_b"],
                        entry_time=datetime.now(timezone.utc).isoformat(),
                        entry_corr=corr,
                    )
                    self.positions[label] = pos
                    actions.append(f"ENTER {label} LONG_RATIO z={z:+.2f} corr={corr:.3f}")

            else:
                # Check exit
                pos.hours_held += 1
                exit_reason = ""

                if pos.direction == 1:
                    if z >= -EXIT_Z:
                        exit_reason = "mean_revert"
                    elif z > STOP_LOSS_Z:
                        exit_reason = "stop_loss"
                elif pos.direction == -1:
                    if z <= EXIT_Z:
                        exit_reason = "mean_revert"
                    elif z < -STOP_LOSS_Z:
                        exit_reason = "stop_loss"

                if pos.hours_held >= MAX_HOLD_HOURS:
                    exit_reason = "time_stop"

                if exit_reason:
                    xa = sig["price_a"]
                    xb = sig["price_b"]
                    ea = pos.entry_price_a
                    eb = pos.entry_price_b

                    if pos.direction == 1:
                        pnl_a = notional * (xa - ea) / ea
                        pnl_b = notional * (eb - xb) / eb
                    else:
                        pnl_a = notional * (ea - xa) / ea
                        pnl_b = notional * (xb - eb) / eb

                    gross = pnl_a + pnl_b
                    net = gross - rt_cost

                    trade = PaperTrade(
                        pair=label,
                        direction="long_ratio" if pos.direction == 1 else "short_ratio",
                        entry_time=pos.entry_time,
                        exit_time=datetime.now(timezone.utc).isoformat(),
                        entry_z=pos.entry_z, exit_z=z,
                        hours_held=pos.hours_held,
                        entry_price_a=ea, entry_price_b=eb,
                        exit_price_a=xa, exit_price_b=xb,
                        gross_pnl=round(gross, 2),
                        cost=round(rt_cost, 2),
                        net_pnl=round(net, 2),
                        entry_corr=pos.entry_corr,
                        exit_reason=exit_reason,
                    )
                    self.trades.append(trade)
                    self.save_trade(trade)

                    actions.append(
                        f"EXIT {label} {exit_reason} z={z:+.2f} "
                        f"held={pos.hours_held}h pnl=${net:+,.0f}"
                    )

                    self.positions[label] = None
                    self.cooldowns[label] = COOLDOWN_HOURS

        return actions

    def save_trade(self, trade: PaperTrade) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / "paper_trades.csv"
        exists = path.exists()
        d = asdict(trade)
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(d.keys()))
            if not exists:
                writer.writeheader()
            writer.writerow(d)

    def log_hourly_snapshot(self) -> None:
        """Write one row per pair to the hourly snapshot log.

        Records the full state at each hourly evaluation: z-scores,
        correlations, prices, position state, unrealized P&L, and
        why a signal was or wasn't acted on. This is the data needed
        to reconstruct everything when we analyze later.
        """
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / "hourly_snapshots.csv"
        exists = path.exists()

        SNAPSHOT_FIELDS = [
            "timestamp", "pair", "z_score", "correlation", "price_a", "price_b",
            "n_bars", "position", "hours_held", "unrealized_pnl",
            "cooldown_remaining", "signal_status", "action_taken",
        ]

        now = datetime.now(timezone.utc).isoformat()
        rows = []

        for coin_a, coin_b in PAIRS:
            label = f"{coin_a}/{coin_b}"
            sig = self.signals.get(label, {})
            z = sig.get("z")
            corr = sig.get("corr")
            pos = self.positions[label]
            cooldown = self.cooldowns.get(label, 0)

            # Determine signal status
            if z is None:
                status = "warming_up"
            elif pos is not None:
                status = "in_position"
            elif cooldown > 0:
                status = "cooldown"
            elif corr is not None and corr < CORR_HIGH:
                status = "corr_blocked"
            elif z is not None and abs(z) > ENTRY_Z:
                status = "signal_present"
            else:
                status = "no_signal"

            # Unrealized P&L
            upnl = 0.0
            if pos and "price_a" in sig and "price_b" in sig:
                xa, xb = sig["price_a"], sig["price_b"]
                ea, eb = pos.entry_price_a, pos.entry_price_b
                if pos.direction == 1:
                    upnl = NOTIONAL_PER_LEG * ((xa - ea) / ea + (eb - xb) / eb)
                else:
                    upnl = NOTIONAL_PER_LEG * ((ea - xa) / ea + (xb - eb) / eb)

            # What position are we in?
            pos_str = ""
            hold = 0
            if pos:
                pos_str = "long_ratio" if pos.direction == 1 else "short_ratio"
                hold = pos.hours_held

            rows.append({
                "timestamp": now,
                "pair": label,
                "z_score": round(z, 6) if z is not None else "",
                "correlation": round(corr, 6) if corr is not None else "",
                "price_a": sig.get("price_a", ""),
                "price_b": sig.get("price_b", ""),
                "n_bars": sig.get("n_bars", ""),
                "position": pos_str,
                "hours_held": hold,
                "unrealized_pnl": round(upnl, 2),
                "cooldown_remaining": cooldown,
                "signal_status": status,
                "action_taken": "",  # filled by process_signals caller
            })

        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=SNAPSHOT_FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerows(rows)

    def save_state(self) -> None:
        """Persist positions, cooldowns, and trade history to disk.

        Called after every hourly evaluation so we can resume after restart.
        """
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        state = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "start_time": self.start_time,
            "last_signal_hour": self.last_signal_hour,
            "positions": {},
            "cooldowns": dict(self.cooldowns),
            "completed_trades_count": len(self.trades),
        }
        for label, pos in self.positions.items():
            if pos is not None:
                state["positions"][label] = {
                    "pair": pos.pair,
                    "direction": pos.direction,
                    "entry_z": pos.entry_z,
                    "entry_price_a": pos.entry_price_a,
                    "entry_price_b": pos.entry_price_b,
                    "entry_time": pos.entry_time,
                    "entry_corr": pos.entry_corr,
                    "hours_held": pos.hours_held,
                }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)

    def load_state(self) -> bool:
        """Restore positions and cooldowns from disk. Returns True if loaded."""
        if not STATE_FILE.exists():
            return False
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)

            self.last_signal_hour = state.get("last_signal_hour", -1)
            self.start_time = state.get("start_time", self.start_time)
            self.cooldowns = {label: state.get("cooldowns", {}).get(label, 0) for label in self.cooldowns}

            for label, pos_data in state.get("positions", {}).items():
                self.positions[label] = Position(
                    pair=pos_data["pair"],
                    direction=pos_data["direction"],
                    entry_z=pos_data["entry_z"],
                    entry_price_a=pos_data["entry_price_a"],
                    entry_price_b=pos_data["entry_price_b"],
                    entry_time=pos_data["entry_time"],
                    entry_corr=pos_data["entry_corr"],
                    hours_held=pos_data["hours_held"],
                )

            # Reload completed trades from CSV
            trades_path = LOG_DIR / "paper_trades.csv"
            if trades_path.exists():
                with open(trades_path) as tf:
                    reader = csv.DictReader(tf)
                    for row in reader:
                        self.trades.append(PaperTrade(
                            pair=row["pair"],
                            direction=row["direction"],
                            entry_time=row["entry_time"],
                            exit_time=row["exit_time"],
                            entry_z=float(row["entry_z"]),
                            exit_z=float(row["exit_z"]),
                            hours_held=int(row["hours_held"]),
                            entry_price_a=float(row["entry_price_a"]),
                            entry_price_b=float(row["entry_price_b"]),
                            exit_price_a=float(row["exit_price_a"]),
                            exit_price_b=float(row["exit_price_b"]),
                            gross_pnl=float(row["gross_pnl"]),
                            cost=float(row["cost"]),
                            net_pnl=float(row["net_pnl"]),
                            entry_corr=float(row["entry_corr"]),
                            exit_reason=row["exit_reason"],
                        ))

            return True
        except Exception:
            return False

    def build_display(self) -> Group:
        """Build full dashboard: signals table + trade history + summary."""
        RichText = Text

        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        n_bars = 0
        for sig in self.signals.values():
            n_bars = sig.get("n_bars", 0)
            break

        # ── Signals table ────────────────────────────────────────────
        t = Table(
            show_header=True,
            header_style="bold cyan",
            expand=True,
        )
        t.add_column("Pair", style="bold")
        t.add_column("Z-Score", justify="right")
        t.add_column("Corr", justify="right")
        t.add_column("Position", justify="center")
        t.add_column("Hold", justify="right")
        t.add_column("Unreal P&L", justify="right")
        t.add_column("Signal", justify="center")

        total_unrealized = 0.0
        for coin_a, coin_b in PAIRS:
            label = f"{coin_a}/{coin_b}"
            sig = self.signals.get(label, {})
            z = sig.get("z")
            corr = sig.get("corr")
            pos = self.positions[label]

            z_str = f"{z:+.2f}" if z is not None else "—"
            corr_str = f"{corr:.3f}" if corr is not None else "warming"

            if z is not None:
                if abs(z) > ENTRY_Z:
                    z_str = f"[bold yellow]{z:+.2f}[/bold yellow]"
                elif abs(z) < EXIT_Z:
                    z_str = f"[dim]{z:+.2f}[/dim]"

            if corr is not None and corr < CORR_HIGH:
                corr_str = f"[red]{corr:.3f}[/red]"

            pos_str = "—"
            hold_str = "—"
            pnl_str = "—"
            signal_str = "—"

            if pos:
                pos_str = "[cyan]LONG[/cyan]" if pos.direction == 1 else "[magenta]SHORT[/magenta]"
                hold_str = f"{pos.hours_held}h"

                if "price_a" in sig and "price_b" in sig:
                    xa, xb = sig["price_a"], sig["price_b"]
                    ea, eb = pos.entry_price_a, pos.entry_price_b
                    if pos.direction == 1:
                        upnl = NOTIONAL_PER_LEG * ((xa - ea) / ea + (eb - xb) / eb)
                    else:
                        upnl = NOTIONAL_PER_LEG * ((ea - xa) / ea + (xb - eb) / eb)
                    total_unrealized += upnl
                    c = "green" if upnl > 0 else "red"
                    pnl_str = f"[{c}]${upnl:+,.0f}[/{c}]"
            else:
                if z is not None and corr is not None:
                    if corr >= CORR_HIGH:
                        if z > ENTRY_Z:
                            signal_str = "[yellow]SHORT?[/yellow]"
                        elif z < -ENTRY_Z:
                            signal_str = "[yellow]LONG?[/yellow]"
                        else:
                            signal_str = "[dim]flat[/dim]"
                    else:
                        signal_str = "[red]blocked[/red]"
                if self.cooldowns.get(label, 0) > 0:
                    signal_str = f"[dim]cool {self.cooldowns[label]}h[/dim]"

            t.add_row(label, z_str, corr_str, pos_str, hold_str, pnl_str, signal_str)

        # ── Trade history table ──────────────────────────────────────
        parts: list = [t, RichText("")]

        if self.trades:
            th = Table(
                title="Completed Trades",
                show_header=True,
                header_style="bold",
                expand=True,
            )
            th.add_column("Pair")
            th.add_column("Dir", justify="center")
            th.add_column("Entry", justify="right")
            th.add_column("Exit", justify="right")
            th.add_column("Hold", justify="right")
            th.add_column("Entry Z", justify="right")
            th.add_column("Net P&L", justify="right")
            th.add_column("Reason")

            for tr in self.trades[-10:]:  # last 10 trades
                entry_short = tr.entry_time[11:16] if len(tr.entry_time) > 16 else tr.entry_time
                exit_short = tr.exit_time[11:16] if len(tr.exit_time) > 16 else tr.exit_time
                d = "L" if tr.direction == "long_ratio" else "S"
                nc = "green" if tr.net_pnl > 0 else "red"
                th.add_row(
                    tr.pair,
                    d,
                    entry_short,
                    exit_short,
                    f"{tr.hours_held}h",
                    f"{tr.entry_z:+.2f}",
                    f"[{nc}]${tr.net_pnl:+,.0f}[/{nc}]",
                    tr.exit_reason,
                )

            parts.append(th)
            parts.append(RichText(""))

        # ── Summary lines ────────────────────────────────────────────
        total_realized = sum(tr.net_pnl for tr in self.trades)
        total_pnl = total_realized + total_unrealized
        n_trades = len(self.trades)
        wins = sum(1 for tr in self.trades if tr.net_pnl > 0)
        wr = f"{wins}/{n_trades} ({wins/n_trades*100:.0f}%)" if n_trades else "0/0"

        # Position sizing info
        n_open = sum(1 for p in self.positions.values() if p is not None)
        total_exposure = n_open * NOTIONAL_PER_LEG * 2  # both legs
        margin_at_lev = total_exposure / DEFAULT_LEVERAGE if DEFAULT_LEVERAGE > 0 else total_exposure
        max_exposure = len(PAIRS) * NOTIONAL_PER_LEG * 2
        max_margin = max_exposure / DEFAULT_LEVERAGE if DEFAULT_LEVERAGE > 0 else max_exposure

        rc = "green" if total_realized >= 0 else "red"
        uc = "green" if total_unrealized >= 0 else "red"
        tc = "green" if total_pnl >= 0 else "red"

        # Runtime and annualized metrics
        try:
            start_dt = datetime.fromisoformat(self.start_time)
            elapsed_hours = max(1, (datetime.now(timezone.utc) - start_dt).total_seconds() / 3600)
            elapsed_days = elapsed_hours / 24
            daily_rate = total_pnl / elapsed_days if elapsed_days > 0 else 0
            annual_proj = daily_rate * 365
            apr_on_margin = annual_proj / max_margin * 100 if max_margin > 0 else 0
            runtime_str = f"{elapsed_days:.1f}d" if elapsed_days >= 1 else f"{elapsed_hours:.0f}h"
        except Exception:
            runtime_str = "?"
            daily_rate = 0
            annual_proj = 0
            apr_on_margin = 0

        dc = "green" if daily_rate >= 0 else "red"
        ac = "green" if apr_on_margin >= 0 else "red"

        summary_lines = [
            f"Trades: {n_trades}  WR: {wr}  "
            f"Realized: [{rc}]${total_realized:+,.0f}[/{rc}]  "
            f"Unrealized: [{uc}]${total_unrealized:+,.0f}[/{uc}]  "
            f"Total: [{tc} bold]${total_pnl:+,.0f}[/{tc} bold]",
            f"Runtime: {runtime_str}  "
            f"Daily rate: [{dc}]${daily_rate:+,.0f}/day[/{dc}]  "
            f"Projected annual: [{dc}]${annual_proj:+,.0f}[/{dc}]  "
            f"APR ({DEFAULT_LEVERAGE}x): [{ac}]{apr_on_margin:+.0f}%[/{ac}]",
            f"Notional/leg: ${NOTIONAL_PER_LEG:,}  "
            f"Open: {n_open}/{len(PAIRS)} pairs  "
            f"Exposure: ${total_exposure:,} / ${max_exposure:,}  "
            f"Margin ({DEFAULT_LEVERAGE}x): ${margin_at_lev:,.0f} / ${max_margin:,.0f}",
            f"[dim]Bars: {n_bars} │ Next signal eval: top of next hour │ Polling every {POLL_INTERVAL_SEC}s │ State auto-saved[/dim]",
        ]

        parts.append(RichText.from_markup("\n".join(summary_lines)))

        return Panel(
            Group(*parts),
            title=f"[bold cyan]Stat Arb Paper Trading[/bold cyan]",
            subtitle=f"[dim]{now}[/dim]",
            border_style="cyan",
            expand=True,
        )


def seed_buffer(monitor: StatArbMonitor) -> None:
    """Seed the price buffer with recent hourly candle closes.

    Loads enough history so that z-scores and correlations are ready
    immediately (CORR_WINDOW + LOOKBACK + margin).  Uses the actual
    candle timestamp to align the buffer with wall-clock hours.
    """
    console.print("[cyan]Seeding price buffer with recent hourly candles...[/cyan]")
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (CORR_WINDOW_HOURS + LOOKBACK_HOURS + 10) * 3_600_000

    last_candle_hour = -1
    for coin in COINS:
        try:
            r = monitor.client.post(
                REST_URL,
                json={
                    "type": "candleSnapshot",
                    "req": {"coin": coin, "interval": "1h", "startTime": start_ms, "endTime": now_ms},
                },
                timeout=15,
            )
            candles = r.json()
            if isinstance(candles, list) and candles:
                closes = [float(c["c"]) for c in candles]
                # The candle 't' field is the bar OPEN time in epoch ms.
                last_open_ms = int(candles[-1]["t"])
                last_candle_hour = datetime.fromtimestamp(
                    last_open_ms / 1000, tz=timezone.utc
                ).hour
                monitor.buffer.seed(coin, closes, last_candle_hour)

                first_dt = datetime.fromtimestamp(int(candles[0]["t"]) / 1000, tz=timezone.utc)
                last_dt = datetime.fromtimestamp(last_open_ms / 1000, tz=timezone.utc)
                console.print(
                    f"  {coin}: {len(closes)} hourly bars "
                    f"({first_dt.strftime('%m/%d %H:00')} → {last_dt.strftime('%m/%d %H:00')})"
                )
        except Exception as e:
            console.print(f"  [red]{coin}: failed ({e})[/red]")
        time.sleep(0.7)

    # The buffer now knows which hour its last bar represents.
    # On the first live poll, if we're still in that same hour,
    # update_live will overwrite the last bar (the current incomplete
    # candle). If the hour has already rolled, it appends a new bar.
    # Either way, no historical bar is destroyed.

    # Don't run signal logic until the first full hour boundary,
    # so we don't act on a partially-formed bar.
    monitor.last_signal_hour = datetime.now(timezone.utc).hour

    console.print()


def main() -> None:
    import sys
    no_resume = "--fresh" in sys.argv

    monitor = StatArbMonitor()

    # Try to resume from saved state
    resumed = False
    if not no_resume and STATE_FILE.exists():
        resumed = monitor.load_state()
        if resumed:
            n_pos = sum(1 for p in monitor.positions.values() if p is not None)
            console.print(f"\n[cyan]Resumed from saved state:[/cyan]")
            console.print(f"  Completed trades: {len(monitor.trades)}")
            console.print(f"  Open positions: {n_pos}")
            console.print(f"  Cooldowns: {dict((k,v) for k,v in monitor.cooldowns.items() if v > 0) or 'none'}")
            for label, pos in monitor.positions.items():
                if pos:
                    d = "LONG" if pos.direction == 1 else "SHORT"
                    console.print(f"    {label}: {d} ratio, {pos.hours_held}h held, entry z={pos.entry_z:+.2f}")
            console.print()

    seed_buffer(monitor)

    if not resumed:
        console.print("[dim]  (Use --fresh to ignore saved state)[/dim]\n")

    console.print("[green]Starting paper trade monitor (Ctrl+C to stop)...[/green]\n")

    try:
        with Live(console=console, refresh_per_second=0.5) as live:
            while True:
                # Fetch current mid prices
                prices = monitor.fetch_prices()
                utc_hour = datetime.now(timezone.utc).hour

                # Update the hourly buffer:
                # - same hour as last bar → overwrites latest bar in-place
                # - new hour → appends a new bar (hour rolled)
                for coin, price in prices.items():
                    monitor.buffer.update_live(coin, price, utc_hour)
                hour_changed = monitor.buffer.advance_hour(utc_hour)

                # Compute signals (uses hourly bars, same as backtest)
                monitor.compute_signals()

                # Entry/exit logic only fires when the hour ticks over
                actions = monitor.process_signals()

                # Log full state snapshot and save state every hour
                if hour_changed:
                    monitor.log_hourly_snapshot()
                    monitor.save_state()

                monitor.tick += 1

                # Update display (always, so we see live z-scores)
                live.update(monitor.build_display())

                # Log actions to console
                for action in actions:
                    console.log(action)

                time.sleep(POLL_INTERVAL_SEC)

    except KeyboardInterrupt:
        console.print("\n[yellow]Stopping paper trade monitor.[/yellow]")
        monitor.save_state()
        console.print("[dim]State saved. Will resume on next start.[/dim]")

    # Final summary
    if monitor.trades:
        console.print("\n[bold cyan]═══ Paper Trading Summary ═══[/bold cyan]\n")
        total = sum(t.net_pnl for t in monitor.trades)
        wins = sum(1 for t in monitor.trades if t.net_pnl > 0)
        n = len(monitor.trades)
        console.print(f"  Trades: {n}")
        console.print(f"  Wins:   {wins} ({wins/n*100:.0f}%)")
        console.print(f"  Net:    ${total:+,.2f}")
        console.print(f"\n  Trade log: {LOG_DIR / 'paper_trades.csv'}")
        console.print(f"  State file: {STATE_FILE}")


if __name__ == "__main__":
    main()
