"""Core strategy engine: entry/exit logic for the stat arb system.

The engine is agnostic to whether it's running a backtest or live.
It processes signals one bar at a time and returns orders (decisions).
The caller executes orders and confirms fills back to the engine.
"""

from __future__ import annotations

from hypemm.config import StrategyConfig
from hypemm.math import compute_leg_pnl
from hypemm.models import (
    CompletedTrade,
    Direction,
    EntryOrder,
    ExitOrder,
    ExitReason,
    FillReport,
    OpenPosition,
    PairConfig,
    Signal,
)


class StrategyEngine:
    """Stateful strategy engine.

    Feed signals via process_bar(), receive orders back.
    Confirm fills via confirm_entry() / confirm_exit().
    """

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self.positions: dict[str, OpenPosition | None] = {
            pair.label: None for pair in config.pairs
        }
        self.cooldowns: dict[str, int] = {pair.label: 0 for pair in config.pairs}
        # When True, _check_entry returns None regardless of signal. Set by
        # the runner from the RiskMonitor when a kill switch is active.
        # Existing positions continue to be managed by the exit logic.
        self.halt_entries: bool = False
        # When True, process_bar returns an empty list — both entries AND
        # exits are suppressed. Set by the runtime reconcile loop when engine
        # state diverges from HL: we can't safely trade until an operator
        # investigates because closing might compound the divergence.
        self.halt_trading: bool = False

    def signed_coin_exposure(self) -> dict[str, float]:
        """Sum of signed leg sizes per coin across all open positions.

        Equals what HL's per-coin szi should read if engine and exchange
        agree. The runtime reconcile loop compares this against
        clearinghouseState; mismatch > tolerance trips halt_trading.

        LONG_RATIO contributes +size_a on coin_a and -size_b on coin_b.
        SHORT_RATIO contributes the opposite. Same-direction pairs on a
        shared coin add; opposite-direction pairs cancel.
        """
        out: dict[str, float] = {}
        for pos in self.positions.values():
            if pos is None:
                continue
            sign_a = 1 if pos.direction == Direction.LONG_RATIO else -1
            out[pos.pair.coin_a] = out.get(pos.pair.coin_a, 0.0) + sign_a * pos.filled_size_a
            out[pos.pair.coin_b] = out.get(pos.pair.coin_b, 0.0) - sign_a * pos.filled_size_b
        return out

    def process_bar(
        self,
        signals: dict[str, Signal],
        timestamp_ms: int,
    ) -> list[EntryOrder | ExitOrder]:
        """Process one bar of signals. Returns entry/exit orders to execute."""
        if self.halt_trading:
            return []
        orders: list[EntryOrder | ExitOrder] = []

        for pair in self.config.pairs:
            label = pair.label
            signal = signals.get(label)
            if signal is None:
                continue

            pos = self.positions[label]

            if pos is None:
                entry = self._check_entry(pair, signal)
                if entry is not None:
                    orders.append(entry)
            else:
                pos.hours_held += 1
                exit_order = self._check_exit(pair, pos, signal)
                if exit_order is not None:
                    orders.append(exit_order)

        return orders

    def _check_entry(self, pair: PairConfig, signal: Signal) -> EntryOrder | None:
        """Check if we should enter a position."""
        label = pair.label

        # Portfolio-level kill switch from the risk monitor.
        if self.halt_entries:
            return None

        if self.cooldowns[label] > 0:
            self.cooldowns[label] -= 1
            return None

        # Correlation gate. Negative threshold disables the gate entirely.
        if self.config.corr_threshold >= 0:
            if signal.correlation is None or signal.correlation < self.config.corr_threshold:
                return None

        # Stationarity gate: Hurst exponent (halt when trending)
        if self.config.hurst_threshold >= 0:
            if signal.hurst is None or signal.hurst > self.config.hurst_threshold:
                return None

        # Stationarity gate: ADF test (halt when non-stationary)
        if self.config.adf_threshold < 0:
            if signal.adf_stat is None or signal.adf_stat > self.config.adf_threshold:
                return None

        z = signal.z_score
        if z > self.config.entry_z:
            return EntryOrder(pair=pair, direction=Direction.SHORT_RATIO, signal=signal)
        if z < -self.config.entry_z:
            return EntryOrder(pair=pair, direction=Direction.LONG_RATIO, signal=signal)

        return None

    def _check_exit(
        self,
        pair: PairConfig,
        pos: OpenPosition,
        signal: Signal,
    ) -> ExitOrder | None:
        """Check if an open position should be exited."""
        z = signal.z_score
        reason = _determine_exit_reason(
            z,
            pos.direction,
            pos.hours_held,
            self.config.exit_z,
            self.config.stop_loss_z,
            self.config.max_hold_hours,
        )

        # Progress-exit: if z hasn't improved enough after N hours, cut
        if reason is None and self.config.progress_exit_hours > 0:
            if pos.hours_held >= self.config.progress_exit_hours:
                required = abs(pos.entry_z) * (1.0 - self.config.progress_exit_pct)
                if abs(z) > required:
                    reason = ExitReason.TIME_STOP

        if reason is None:
            return None

        return ExitOrder(pair=pair, position=pos, reason=reason, signal=signal)

    def confirm_entry(
        self,
        order: EntryOrder,
        fill: FillReport,
        timestamp_ms: int,
    ) -> OpenPosition:
        """Confirm an entry fill. Registers the position internally."""
        if self.config.corr_threshold >= 0 and order.signal.correlation is None:
            raise ValueError("Entry confirmed with None correlation — should have been blocked")
        corr = order.signal.correlation if order.signal.correlation is not None else float("nan")
        pos = OpenPosition(
            pair=order.pair,
            direction=order.direction,
            entry_z=order.signal.z_score,
            entry_price_a=fill.price_a,
            entry_price_b=fill.price_b,
            entry_time_ms=timestamp_ms,
            entry_correlation=corr,
            filled_size_a=fill.size_a,
            filled_size_b=fill.size_b,
            entry_fee_a=fill.fee_a,
            entry_fee_b=fill.fee_b,
            entry_fee_a_actual=fill.fee_a_actual,
            entry_fee_b_actual=fill.fee_b_actual,
            entry_oid_a=fill.oid_a,
            entry_oid_b=fill.oid_b,
        )
        self.positions[order.pair.label] = pos
        return pos

    def confirm_exit(
        self,
        order: ExitOrder,
        fill: FillReport,
        timestamp_ms: int,
    ) -> CompletedTrade:
        """Confirm an exit fill. Closes position and returns completed trade.

        `cost` is computed from the per-leg modeled fees recorded at entry +
        the modeled fees on this fill. `funding_cost` is the modeled total
        accumulated on the position. The *_actual fields are pass-throughs
        for the reconcile report; they don't enter net_pnl.
        """
        pos = order.position
        pnl_a, pnl_b = compute_leg_pnl(
            pos.direction,
            self.config.notional_per_leg,
            pos.entry_price_a,
            pos.entry_price_b,
            fill.price_a,
            fill.price_b,
        )
        gross = pnl_a + pnl_b
        cost = pos.entry_fee_a + pos.entry_fee_b + fill.fee_a + fill.fee_b
        net = gross - cost - pos.funding_paid

        trade = CompletedTrade(
            pair_label=pos.pair.label,
            direction=pos.direction,
            entry_ts=pos.entry_time_ms,
            exit_ts=timestamp_ms,
            entry_z=pos.entry_z,
            exit_z=order.signal.z_score,
            hours_held=pos.hours_held,
            entry_price_a=pos.entry_price_a,
            entry_price_b=pos.entry_price_b,
            exit_price_a=fill.price_a,
            exit_price_b=fill.price_b,
            pnl_leg_a=pnl_a,
            pnl_leg_b=pnl_b,
            gross_pnl=gross,
            cost=cost,
            net_pnl=net,
            exit_reason=order.reason,
            entry_correlation=pos.entry_correlation,
            funding_cost=pos.funding_paid,
            entry_size_a=pos.filled_size_a,
            entry_size_b=pos.filled_size_b,
            entry_fee_a=pos.entry_fee_a,
            entry_fee_b=pos.entry_fee_b,
            exit_fee_a=fill.fee_a,
            exit_fee_b=fill.fee_b,
            entry_fee_a_actual=pos.entry_fee_a_actual,
            entry_fee_b_actual=pos.entry_fee_b_actual,
            exit_fee_a_actual=fill.fee_a_actual,
            exit_fee_b_actual=fill.fee_b_actual,
            funding_actual=pos.funding_paid_actual,
            entry_oid_a=pos.entry_oid_a,
            entry_oid_b=pos.entry_oid_b,
            exit_oid_a=fill.oid_a,
            exit_oid_b=fill.oid_b,
        )

        self.positions[pos.pair.label] = None
        self.cooldowns[pos.pair.label] = self.config.cooldown_hours
        return trade

    def get_state(self) -> dict[str, object]:
        """Serialize engine state for persistence."""
        positions_data: dict[str, dict[str, object] | None] = {}
        for label, pos in self.positions.items():
            if pos is not None:
                positions_data[label] = {
                    "coin_a": pos.pair.coin_a,
                    "coin_b": pos.pair.coin_b,
                    "direction": int(pos.direction),
                    "entry_z": pos.entry_z,
                    "entry_price_a": pos.entry_price_a,
                    "entry_price_b": pos.entry_price_b,
                    "entry_time_ms": pos.entry_time_ms,
                    "entry_correlation": pos.entry_correlation,
                    "filled_size_a": pos.filled_size_a,
                    "filled_size_b": pos.filled_size_b,
                    "hours_held": pos.hours_held,
                    "funding_paid": pos.funding_paid,
                    "funding_paid_actual": pos.funding_paid_actual,
                    "entry_fee_a": pos.entry_fee_a,
                    "entry_fee_b": pos.entry_fee_b,
                    "entry_fee_a_actual": pos.entry_fee_a_actual,
                    "entry_fee_b_actual": pos.entry_fee_b_actual,
                    "entry_oid_a": pos.entry_oid_a,
                    "entry_oid_b": pos.entry_oid_b,
                }
            else:
                positions_data[label] = None

        return {
            "positions": positions_data,
            "cooldowns": dict(self.cooldowns),
        }

    def load_state(self, state: dict[str, object]) -> None:
        """Restore engine state from persisted data.

        Missing new-schema fields default to 0.0 / 0 so a state.json written
        before the accounting overhaul still loads cleanly. Such positions
        will close with zero entry-side fees recorded — accept the gap on the
        first close after deploy rather than fabricate values.
        """
        positions_data = state.get("positions", {})
        if not isinstance(positions_data, dict):
            return

        for label, pos_data in positions_data.items():
            if label not in self.positions:
                continue
            if pos_data is None:
                self.positions[label] = None
                continue
            if not isinstance(pos_data, dict):
                continue

            self.positions[label] = OpenPosition(
                pair=PairConfig(str(pos_data["coin_a"]), str(pos_data["coin_b"])),
                direction=Direction(int(pos_data["direction"])),
                entry_z=float(pos_data["entry_z"]),
                entry_price_a=float(pos_data["entry_price_a"]),
                entry_price_b=float(pos_data["entry_price_b"]),
                entry_time_ms=int(pos_data["entry_time_ms"]),
                entry_correlation=float(pos_data["entry_correlation"]),
                filled_size_a=float(pos_data.get("filled_size_a", 0.0)),
                filled_size_b=float(pos_data.get("filled_size_b", 0.0)),
                hours_held=int(pos_data["hours_held"]),
                funding_paid=float(pos_data.get("funding_paid", 0.0)),
                funding_paid_actual=float(pos_data.get("funding_paid_actual", 0.0)),
                entry_fee_a=float(pos_data.get("entry_fee_a", 0.0)),
                entry_fee_b=float(pos_data.get("entry_fee_b", 0.0)),
                entry_fee_a_actual=float(pos_data.get("entry_fee_a_actual", 0.0)),
                entry_fee_b_actual=float(pos_data.get("entry_fee_b_actual", 0.0)),
                entry_oid_a=int(pos_data.get("entry_oid_a", 0)),
                entry_oid_b=int(pos_data.get("entry_oid_b", 0)),
            )

        cooldowns_data = state.get("cooldowns", {})
        if isinstance(cooldowns_data, dict):
            for label, val in cooldowns_data.items():
                if label in self.cooldowns:
                    self.cooldowns[label] = int(val)


def _determine_exit_reason(
    z: float,
    direction: Direction,
    hours_held: int,
    exit_z: float,
    stop_loss_z: float,
    max_hold_hours: int,
) -> ExitReason | None:
    """Determine if/why a position should be exited."""
    if direction == Direction.LONG_RATIO:
        # Entered at z < -entry_z. Exit when z rises back to >= -exit_z
        if z >= -exit_z:
            return ExitReason.MEAN_REVERT
        if z > stop_loss_z:
            return ExitReason.STOP_LOSS
    else:
        # Entered at z > entry_z. Exit when z falls back to <= exit_z
        if z <= exit_z:
            return ExitReason.MEAN_REVERT
        if z < -stop_loss_z:
            return ExitReason.STOP_LOSS

    if hours_held >= max_hold_hours:
        return ExitReason.TIME_STOP

    return None
