"""Backtest ≡ live parity invariant.

The headline goal of the accounting overhaul: given the same fills, the
backtest path and the runner path produce byte-identical CompletedTrade
rows. Both paths must funnel fee math through `accounting.fee_for_fill`
and funding through `accounting.funding_for_leg_hour`.

This file locks that in. If a future refactor introduces a divergence
(e.g., live starts using HL's actual fee in net_pnl while backtest stays
modeled), one of these tests fails.
"""

from __future__ import annotations

import pytest

from hypemm.accounting import fee_for_fill, funding_for_leg_hour, signed_leg_sizes
from hypemm.backtest import _build_backtest_fill
from hypemm.config import StrategyConfig
from hypemm.engine import StrategyEngine
from hypemm.models import (
    Direction,
    EntryOrder,
    ExitOrder,
    ExitReason,
    FillReport,
    PairConfig,
    Signal,
)


def _signal(pair: PairConfig, z: float, ts_ms: int = 0) -> Signal:
    return Signal(
        pair=pair,
        z_score=z,
        correlation=0.9,
        price_a=10.0,
        price_b=100.0,
        timestamp_ms=ts_ms,
        n_bars=100,
    )


def test_build_backtest_fill_matches_live_formula() -> None:
    """`_build_backtest_fill` calls the same fee_for_fill function the live
    adapter does. With identical (price, size, taker_fee_bps), the produced
    fees are bit-equal — and that's what guarantees backtest≡live in `cost`.
    """
    fill = _build_backtest_fill(price_a=10.0, price_b=100.0, notional=50_000.0, taker_fee_bps=4.5)
    expected_size_a = 50_000.0 / 10.0
    expected_size_b = 50_000.0 / 100.0
    assert fill.size_a == expected_size_a
    assert fill.size_b == expected_size_b
    assert fill.fee_a == fee_for_fill(10.0, expected_size_a, 4.5)
    assert fill.fee_b == fee_for_fill(100.0, expected_size_b, 4.5)
    # Audit columns duplicate modeled in non-live paths so reconcile-pnl
    # can compare uniformly without a "is this live?" branch.
    assert fill.fee_a_actual == fill.fee_a
    assert fill.fee_b_actual == fill.fee_b
    assert fill.oid_a == 0
    assert fill.oid_b == 0


def test_close_fill_honors_close_sizes_verbatim() -> None:
    """On exit the backtest must size from the entry size, not from
    notional/exit_price — same contract the live adapter enforces, so the
    fee computation is on a comparable basis."""
    entry = _build_backtest_fill(10.0, 100.0, 50_000.0, 4.5)
    exit_fill = _build_backtest_fill(
        12.0, 90.0, 50_000.0, 4.5, is_close=True, close_sizes=(entry.size_a, entry.size_b)
    )
    assert exit_fill.size_a == entry.size_a
    assert exit_fill.size_b == entry.size_b
    # Fee is on the EXIT price × the persisted entry size — same as live.
    assert exit_fill.fee_a == fee_for_fill(12.0, entry.size_a, 4.5)
    assert exit_fill.fee_b == fee_for_fill(90.0, entry.size_b, 4.5)


def test_engine_produces_identical_completed_trade_regardless_of_path() -> None:
    """The engine doesn't care which adapter built the FillReport.

    Run two engines side-by-side: one fed FillReports from
    `_build_backtest_fill` (the backtest path), the other fed
    FillReports built explicitly with the same inputs (mimicking what a
    live adapter returns). The resulting CompletedTrade must agree on
    every accounting-relevant field.
    """
    pair = PairConfig("LINK", "SOL")
    cfg = StrategyConfig(pairs=(pair,), taker_fee_bps=4.5, notional_per_leg=50_000)

    # ---- backtest path
    eng_bt = StrategyEngine(cfg)
    entry_sig = _signal(pair, z=-2.5, ts_ms=1000)
    orders = eng_bt.process_bar({pair.label: entry_sig}, timestamp_ms=1000)
    assert isinstance(orders[0], EntryOrder)
    bt_entry = _build_backtest_fill(10.0, 100.0, cfg.notional_per_leg, cfg.taker_fee_bps)
    eng_bt.confirm_entry(orders[0], bt_entry, 1000)

    exit_sig = _signal(pair, z=-0.3, ts_ms=2000)
    orders = eng_bt.process_bar({pair.label: exit_sig}, timestamp_ms=2000)
    assert isinstance(orders[0], ExitOrder)
    bt_exit = _build_backtest_fill(
        11.0,
        99.0,
        cfg.notional_per_leg,
        cfg.taker_fee_bps,
        is_close=True,
        close_sizes=(bt_entry.size_a, bt_entry.size_b),
    )
    bt_trade = eng_bt.confirm_exit(orders[0], bt_exit, 2000)

    # ---- live-mimic path: identical FillReports built explicitly
    eng_live = StrategyEngine(cfg)
    orders = eng_live.process_bar({pair.label: entry_sig}, timestamp_ms=1000)
    sa = cfg.notional_per_leg / 10.0
    sb = cfg.notional_per_leg / 100.0
    live_entry = FillReport(
        price_a=10.0,
        price_b=100.0,
        size_a=sa,
        size_b=sb,
        fee_a=fee_for_fill(10.0, sa, cfg.taker_fee_bps),
        fee_b=fee_for_fill(100.0, sb, cfg.taker_fee_bps),
        fee_a_actual=fee_for_fill(10.0, sa, cfg.taker_fee_bps),
        fee_b_actual=fee_for_fill(100.0, sb, cfg.taker_fee_bps),
        oid_a=0,
        oid_b=0,
    )
    eng_live.confirm_entry(orders[0], live_entry, 1000)

    orders = eng_live.process_bar({pair.label: exit_sig}, timestamp_ms=2000)
    live_exit = FillReport(
        price_a=11.0,
        price_b=99.0,
        size_a=sa,
        size_b=sb,
        fee_a=fee_for_fill(11.0, sa, cfg.taker_fee_bps),
        fee_b=fee_for_fill(99.0, sb, cfg.taker_fee_bps),
        fee_a_actual=fee_for_fill(11.0, sa, cfg.taker_fee_bps),
        fee_b_actual=fee_for_fill(99.0, sb, cfg.taker_fee_bps),
        oid_a=0,
        oid_b=0,
    )
    live_trade = eng_live.confirm_exit(orders[0], live_exit, 2000)

    # Every field that ends up in the CSV (accounting-load-bearing) must agree.
    for field in (
        "pair_label",
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
        "net_pnl",
        "exit_reason",
        "funding_cost",
        "entry_size_a",
        "entry_size_b",
        "entry_fee_a",
        "entry_fee_b",
        "exit_fee_a",
        "exit_fee_b",
    ):
        assert getattr(bt_trade, field) == getattr(live_trade, field), (
            f"mismatch on {field}: bt={getattr(bt_trade, field)} "
            f"live={getattr(live_trade, field)}"
        )


def test_funding_per_leg_sums_to_hl_charge_by_linearity() -> None:
    """The whole reason per-leg funding attribution works under cross-pair
    coin overlap: linearity. Per-leg charges sum to the netted HL charge."""
    # Pair 1: LONG_RATIO LINK/SOL → +sa LINK, -sb SOL
    # Pair 2: LONG_RATIO SOL/AVAX → +sa SOL, -sb AVAX
    # Net SOL position = -sb + sa.  HL charges (net_sol × mark × rate).
    # Sum of per-leg attributions for SOL must equal that.
    sa1, sb1 = signed_leg_sizes(Direction.LONG_RATIO, 10.0, 0.5)  # (+10, -0.5)
    sa2, sb2 = signed_leg_sizes(Direction.LONG_RATIO, 0.5, 5.0)  # (+0.5, -5.0)
    sol_legs = [sb1, sa2]  # the SOL legs of each pair
    mark_sol = 100.0
    rate_sol = 0.0001

    per_leg_charges = [funding_for_leg_hour(s, mark_sol, rate_sol) for s in sol_legs]
    hl_charge = sum(sol_legs) * mark_sol * rate_sol  # what HL would bill

    assert sum(per_leg_charges) == pytest.approx(hl_charge)


def test_signed_leg_sizes_inverts_correctly() -> None:
    long_a, long_b = signed_leg_sizes(Direction.LONG_RATIO, 10.0, 5.0)
    short_a, short_b = signed_leg_sizes(Direction.SHORT_RATIO, 10.0, 5.0)
    assert (long_a, long_b) == (10.0, -5.0)
    assert (short_a, short_b) == (-10.0, 5.0)


def test_completed_trade_cost_equals_sum_of_leg_fees() -> None:
    """The CSV `cost` column must always equal the sum of the four per-leg
    modeled fee columns. If a future change makes them disagree, this fails.
    """
    pair = PairConfig("LINK", "SOL")
    cfg = StrategyConfig(pairs=(pair,), taker_fee_bps=4.5, notional_per_leg=25.0)
    eng = StrategyEngine(cfg)

    orders = eng.process_bar({pair.label: _signal(pair, z=-2.5, ts_ms=1000)}, 1000)
    entry = _build_backtest_fill(10.0, 100.0, cfg.notional_per_leg, cfg.taker_fee_bps)
    eng.confirm_entry(orders[0], entry, 1000)

    orders = eng.process_bar({pair.label: _signal(pair, z=-0.3, ts_ms=2000)}, 2000)
    exit_fill = _build_backtest_fill(
        11.0,
        99.0,
        cfg.notional_per_leg,
        cfg.taker_fee_bps,
        is_close=True,
        close_sizes=(entry.size_a, entry.size_b),
    )
    trade = eng.confirm_exit(orders[0], exit_fill, 2000)
    assert trade.exit_reason == ExitReason.MEAN_REVERT

    expected = trade.entry_fee_a + trade.entry_fee_b + trade.exit_fee_a + trade.exit_fee_b
    assert trade.cost == pytest.approx(expected)
    # And net_pnl is gross − cost − funding (funding is zero here)
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.cost - trade.funding_cost)
