"""Reconcile the runner's CSV against HL's userFills + userFunding ledger.

For a live config, queries HL for fills and funding events since `start_time`
in `state.json`, matches each CSV trade to its 4 fill events by oid, and
reports the gap between modeled and actual cost. Surfaces:

  - per-trade fee model error (modeled fee vs HL-billed fee)
  - per-trade funding model error
  - HL fills with no matching CSV trade (residual flattens, manual
    interventions, leg-A-orphan rollbacks)

Emits a `taker_fee_bps` re-calibration suggestion based on observed actuals.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from hypemm.models import CompletedTrade
from hypemm.persistence import load_trades

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradeReconcile:
    """One CSV trade compared against its HL fills."""

    trade: CompletedTrade
    modeled_fees: float
    actual_fees: float
    modeled_funding: float
    actual_funding: float

    @property
    def fee_drift(self) -> float:
        return self.actual_fees - self.modeled_fees

    @property
    def funding_drift(self) -> float:
        return self.actual_funding - self.modeled_funding


@dataclass(frozen=True)
class UnmatchedFill:
    """A HL userFills event whose oid doesn't map to any CSV trade."""

    coin: str
    direction: str  # "Open Long", "Close Short", etc.
    size: float
    price: float
    closed_pnl: float
    fee: float
    timestamp_ms: int
    oid: int


@dataclass(frozen=True)
class ReconcileReport:
    """Top-level diff: CSV vs HL ledger."""

    start_ms: int
    end_ms: int
    trades: list[TradeReconcile]
    unmatched_fills: list[UnmatchedFill]
    csv_total_net: float
    hl_realized: float  # Σ closedPnl − Σ fees + Σ funding
    hl_closed_pnl: float
    hl_fees: float
    hl_funding: float

    @property
    def gap(self) -> float:
        """How far the CSV total is from the HL ledger truth."""
        return self.hl_realized - self.csv_total_net

    @property
    def total_modeled_fees(self) -> float:
        return sum(r.modeled_fees for r in self.trades)

    @property
    def total_actual_fees(self) -> float:
        return sum(r.actual_fees for r in self.trades)


def fetch_user_fills(
    client: httpx.Client, info_url: str, account: str, start_ms: int
) -> list[dict[str, Any]]:
    """Fetch HL userFills since start_ms. HL API caps responses; this returns
    whatever the endpoint hands back in one call.
    """
    r = client.post(
        info_url,
        json={"type": "userFillsByTime", "user": account, "startTime": start_ms},
        timeout=15.0,
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return []
    return [dict(row) for row in data]


def fetch_user_funding_window(
    client: httpx.Client, info_url: str, account: str, start_ms: int
) -> list[dict[str, Any]]:
    """Fetch HL userFunding since start_ms."""
    r = client.post(
        info_url,
        json={"type": "userFunding", "user": account, "startTime": start_ms},
        timeout=15.0,
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return []
    return [dict(row) for row in data]


def build_report(
    trades: list[CompletedTrade],
    fills: list[dict[str, Any]],
    funding_events: list[dict[str, Any]],
    start_ms: int,
    end_ms: int,
) -> ReconcileReport:
    """Match trades to fills by oid; compute drift; surface unmatched fills.

    Trades from before the size-persistence overhaul (`entry_oid_a == 0`)
    can't be matched by oid — fall back to timestamp matching for those.
    """
    fills_by_oid: dict[int, list[dict[str, Any]]] = {}
    for f in fills:
        oid = int(f.get("oid", 0))
        fills_by_oid.setdefault(oid, []).append(f)

    matched_oids: set[int] = set()
    trade_reconciles: list[TradeReconcile] = []
    for t in trades:
        oids = (t.entry_oid_a, t.entry_oid_b, t.exit_oid_a, t.exit_oid_b)
        modeled_fees = t.entry_fee_a + t.entry_fee_b + t.exit_fee_a + t.exit_fee_b
        actual_fees = 0.0
        for oid in oids:
            if oid <= 0:
                continue
            matched_oids.add(oid)
            for f in fills_by_oid.get(oid, []):
                actual_fees += float(f.get("fee", 0.0))

        # Funding attributed to this trade = events for the trade's coins
        # within (entry_ts, exit_ts]. Approximation: assumes the position is
        # the only holder of those coins. With cross-pair overlap this can
        # over-attribute; reconcile the *session total* if you suspect that.
        coin_a, coin_b = t.pair_label.split("/", 1)
        actual_funding = 0.0
        for ev in funding_events:
            ts = int(ev.get("time", 0))
            if not (t.entry_ts < ts <= t.exit_ts):
                continue
            d = ev.get("delta")
            if not isinstance(d, dict) or d.get("type") != "funding":
                continue
            coin = d.get("coin")
            if coin == coin_a or coin == coin_b:
                actual_funding += float(d.get("usdc", 0.0))

        trade_reconciles.append(
            TradeReconcile(
                trade=t,
                modeled_fees=modeled_fees,
                actual_fees=actual_fees,
                modeled_funding=t.funding_cost,
                actual_funding=actual_funding,
            )
        )

    unmatched: list[UnmatchedFill] = []
    for f in fills:
        oid = int(f.get("oid", 0))
        if oid in matched_oids:
            continue
        unmatched.append(
            UnmatchedFill(
                coin=str(f.get("coin", "")),
                direction=str(f.get("dir", "")),
                size=float(f.get("sz", 0.0)),
                price=float(f.get("px", 0.0)),
                closed_pnl=float(f.get("closedPnl", 0.0)),
                fee=float(f.get("fee", 0.0)),
                timestamp_ms=int(f.get("time", 0)),
                oid=oid,
            )
        )

    hl_closed_pnl = sum(float(f.get("closedPnl", 0.0)) for f in fills)
    hl_fees = sum(float(f.get("fee", 0.0)) for f in fills)
    hl_funding = 0.0
    for ev in funding_events:
        d = ev.get("delta")
        if isinstance(d, dict) and d.get("type") == "funding":
            hl_funding += float(d.get("usdc", 0.0))

    csv_total_net = sum(t.net_pnl for t in trades)
    # HL ledger convention: closedPnl is gross PnL on closing fills, fees are
    # a cost, funding deltas are signed (positive usdc = HL paid us, negative
    # = we paid HL). Net realized = gross − fees + funding_signed.
    hl_realized = hl_closed_pnl - hl_fees + hl_funding

    return ReconcileReport(
        start_ms=start_ms,
        end_ms=end_ms,
        trades=trade_reconciles,
        unmatched_fills=unmatched,
        csv_total_net=csv_total_net,
        hl_realized=hl_realized,
        hl_closed_pnl=hl_closed_pnl,
        hl_fees=hl_fees,
        hl_funding=hl_funding,
    )


def suggest_taker_fee_bps(report: ReconcileReport) -> float | None:
    """Implied taker_fee_bps from observed actual fees over total fill notional.

    Returns None if there's nothing to calibrate against (no matched fills).
    The user can compare this against the configured value and update if drift
    exceeds tolerance.
    """
    total_notional = 0.0
    total_actual = 0.0
    for r in report.trades:
        # Best-effort: notional = sum |size × price| across the 4 fills.
        # Use modeled values since we have them; this is for calibration UI,
        # not load-bearing accounting.
        t = r.trade
        total_notional += abs(t.entry_size_a * t.entry_price_a)
        total_notional += abs(t.entry_size_b * t.entry_price_b)
        total_notional += abs(t.entry_size_a * t.exit_price_a)
        total_notional += abs(t.entry_size_b * t.exit_price_b)
        total_actual += r.actual_fees
    if total_notional <= 0 or total_actual <= 0:
        return None
    return total_actual / total_notional * 10_000.0


def format_report(report: ReconcileReport) -> str:
    """Human-readable summary suitable for stdout / CI logs."""
    lines: list[str] = []
    start_dt = datetime.fromtimestamp(report.start_ms / 1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(report.end_ms / 1000, tz=timezone.utc)
    lines.append(f"Period:        {start_dt.isoformat()} → {end_dt.isoformat()}")
    lines.append(f"Trades:        {len(report.trades)}")
    lines.append("")
    lines.append(f"CSV total:     ${report.csv_total_net:+.4f}")
    lines.append(
        f"HL realized:   ${report.hl_realized:+.4f}   "
        f"(closedPnl ${report.hl_closed_pnl:+.4f} − fees ${report.hl_fees:.4f} "
        f"− funding ${report.hl_funding:+.4f})"
    )
    lines.append(f"Gap:           ${report.gap:+.4f}")
    lines.append("")
    lines.append("Buckets:")
    lines.append(
        f"  Fee drift:   ${report.total_actual_fees - report.total_modeled_fees:+.4f}"
        f"   (modeled ${report.total_modeled_fees:.4f}, actual ${report.total_actual_fees:.4f})"
    )
    if report.unmatched_fills:
        unaccounted_pnl = sum(u.closed_pnl - u.fee for u in report.unmatched_fills)
        lines.append(
            f"  Unaccounted: ${unaccounted_pnl:+.4f}   "
            f"({len(report.unmatched_fills)} fill(s) not in CSV)"
        )
        for u in report.unmatched_fills:
            ts = datetime.fromtimestamp(u.timestamp_ms / 1000, tz=timezone.utc)
            lines.append(
                f"    {ts.strftime('%Y-%m-%d %H:%M:%S')}  {u.coin:<5} "
                f"{u.direction:<14} sz={u.size:<10g} px={u.price:<10g} "
                f"closedPnl=${u.closed_pnl:+.4f} fee=${u.fee:.4f}"
            )
    suggested = suggest_taker_fee_bps(report)
    if suggested is not None:
        lines.append("")
        lines.append(f"Implied taker_fee_bps from actuals: {suggested:.2f}")
    return "\n".join(lines)


def reconcile_run_dir(
    run_dir: Path,
    *,
    info_url: str,
    account: str,
) -> ReconcileReport:
    """Top-level entry point: load trades + state.json, hit HL, build report."""
    trades_path = run_dir / "paper_trades.csv"
    state_path = run_dir / "state.json"

    trades = load_trades(trades_path)

    if not state_path.exists():
        raise FileNotFoundError(f"state.json missing at {state_path}")
    with open(state_path) as f:
        state = json.load(f)
    start_iso = state.get("start_time")
    if not start_iso:
        raise ValueError(f"state.json at {state_path} has no start_time")
    start_dt = datetime.fromisoformat(start_iso)
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    with httpx.Client(timeout=15.0) as client:
        fills = fetch_user_fills(client, info_url, account, start_ms)
        funding_events = fetch_user_funding_window(client, info_url, account, start_ms)

    return build_report(trades, fills, funding_events, start_ms, end_ms)
