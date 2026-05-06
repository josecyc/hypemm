"""Tests for funding rate fetching and cost computation."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from hypemm.funding import (
    _save_csv,
    accrue_hourly_funding,
    compute_funding_cost,
    fetch_coin_funding,
    fetch_funding_page,
    load_funding,
)
from hypemm.models import Direction, OpenPosition, PairConfig


def _make_response(records: list[dict[str, Any]]) -> MagicMock:
    response = MagicMock()
    response.json.return_value = records
    response.raise_for_status = MagicMock()
    return response


def _funding_record(ts: int, rate: float, premium: float = 0.0) -> dict[str, Any]:
    return {"time": ts, "fundingRate": str(rate), "premium": str(premium)}


def _write_funding_csv(path: Path, coin: str, rows: list[tuple[int, float]]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    csv_path = path / f"{coin}_1h.csv"
    with open(csv_path, "w") as f:
        f.write("timestamp,funding_rate,premium\n")
        for ts, rate in rows:
            f.write(f"{ts},{rate},0.0\n")


class TestFetchFundingPage:
    def test_parses_response(self) -> None:
        client = MagicMock()
        client.post.return_value = _make_response(
            [_funding_record(1000, -0.0001), _funding_record(4600, 0.0002)]
        )
        rows = fetch_funding_page(client, "http://fake", "BTC", 0)
        assert rows == [
            {"timestamp": 1000, "funding_rate": -0.0001, "premium": 0.0},
            {"timestamp": 4600, "funding_rate": 0.0002, "premium": 0.0},
        ]

    def test_non_list_response_returns_empty(self) -> None:
        client = MagicMock()
        client.post.return_value = _make_response({"error": "bad"})  # type: ignore[arg-type]
        assert fetch_funding_page(client, "http://fake", "BTC", 0) == []


class TestFetchCoinFundingPagination:
    def test_paginates_when_page_full(self, tmp_path: Path) -> None:
        page1 = [_funding_record(i * 3_600_000, 0.0001) for i in range(500)]
        page2 = [_funding_record(500 * 3_600_000 + i * 3_600_000, 0.0002) for i in range(200)]
        client = MagicMock()
        client.post.side_effect = [_make_response(page1), _make_response(page2)]

        fetch_coin_funding(client, "http://fake", "BTC", tmp_path, rate_limit_sec=0)

        assert client.post.call_count == 2
        # Page 2 must start from page 1's last timestamp + 1
        second_call_start = client.post.call_args_list[1].kwargs["json"]["startTime"]
        assert second_call_start == 499 * 3_600_000 + 1

        csv_path = tmp_path / "BTC_1h.csv"
        assert csv_path.exists()
        df = pd.read_csv(csv_path)
        assert len(df) == 700

    def test_stops_when_page_short(self, tmp_path: Path) -> None:
        """A page returning fewer than 500 records means we're caught up."""
        page = [_funding_record(i * 3_600_000, 0.0001) for i in range(10)]
        client = MagicMock()
        client.post.return_value = _make_response(page)

        fetch_coin_funding(client, "http://fake", "BTC", tmp_path, rate_limit_sec=0)

        assert client.post.call_count == 1

    def test_skips_if_up_to_date(self, tmp_path: Path) -> None:
        import time

        now_ms = int(time.time() * 1000)
        recent_ts = now_ms - 1000
        _write_funding_csv(tmp_path, "BTC", [(recent_ts, 0.0001)])

        client = MagicMock()
        fetch_coin_funding(client, "http://fake", "BTC", tmp_path, rate_limit_sec=0)
        assert client.post.call_count == 0

    def test_resumes_from_last_saved(self, tmp_path: Path) -> None:
        """When existing CSV is stale, resume from last_ts + 1."""
        old_ts = 1_700_000_000_000
        _write_funding_csv(tmp_path, "BTC", [(old_ts, 0.0001)])

        client = MagicMock()
        client.post.return_value = _make_response([])
        fetch_coin_funding(client, "http://fake", "BTC", tmp_path, rate_limit_sec=0)

        start = client.post.call_args.kwargs["json"]["startTime"]
        assert start == old_ts + 1


class TestSaveCsv:
    def test_dedupes_and_sorts(self, tmp_path: Path) -> None:
        rows: list[dict[str, float | int]] = [
            {"timestamp": 2000, "funding_rate": 0.2, "premium": 0.0},
            {"timestamp": 1000, "funding_rate": 0.1, "premium": 0.0},
            {"timestamp": 2000, "funding_rate": 0.25, "premium": 0.0},
        ]
        path = tmp_path / "X_1h.csv"
        n = _save_csv(path, rows)
        assert n == 2
        df = pd.read_csv(path)
        assert list(df["timestamp"]) == [1000, 2000]


class TestLoadFunding:
    def test_loads_wide_frame(self, tmp_path: Path) -> None:
        base = 1_700_000_000_000
        _write_funding_csv(tmp_path, "BTC", [(base + i * 3_600_000, 0.0001 * i) for i in range(5)])
        _write_funding_csv(tmp_path, "ETH", [(base + i * 3_600_000, 0.0002 * i) for i in range(5)])

        df = load_funding(tmp_path, ["BTC", "ETH"])
        assert list(df.columns) == ["BTC", "ETH"]
        assert len(df) == 5

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Missing"):
            load_funding(tmp_path, ["NONEXISTENT"])


class TestComputeFundingCost:
    @staticmethod
    def _series(base_ts: int, rates: list[float]) -> "pd.Series[float]":
        idx = pd.to_datetime(
            [base_ts + i * 3_600_000 for i in range(len(rates))], unit="ms", utc=True
        )
        return pd.Series(rates, index=idx)

    def test_long_ratio(self) -> None:
        # Per-leg model: signed_a × Σ(mark_a × rate_a) + signed_b × Σ(mark_b × rate_b)
        # For constant marks and signed_a=+size_a, signed_b=-size_b this equals
        # size_a×mark_a×Σrate_a − size_b×mark_b×Σrate_b. With mark_a=size_b=mark_b=size_a=1,
        # it collapses to Σrate_a − Σrate_b — the legacy formula.
        base = 1_700_000_000_000
        a_rates = self._series(base, [0.0010, 0.0020, 0.0030])  # sum = 0.006
        b_rates = self._series(base, [0.0005, 0.0005, 0.0005])  # sum = 0.0015
        a_marks = self._series(base, [100.0, 100.0, 100.0])
        b_marks = self._series(base, [10.0, 10.0, 10.0])
        cost = compute_funding_cost(
            Direction.LONG_RATIO,
            500.0,  # size_a
            5000.0,  # size_b
            base,
            base + 3 * 3_600_000,
            a_rates,
            b_rates,
            a_marks,
            b_marks,
        )
        # +500 × 100 × 0.006 + (-5000) × 10 × 0.0015 = 300 − 75 = 225
        assert cost == pytest.approx(225.0)

    def test_short_ratio(self) -> None:
        base = 1_700_000_000_000
        a_rates = self._series(base, [0.0010, 0.0020, 0.0030])
        b_rates = self._series(base, [0.0005, 0.0005, 0.0005])
        a_marks = self._series(base, [100.0, 100.0, 100.0])
        b_marks = self._series(base, [10.0, 10.0, 10.0])
        cost = compute_funding_cost(
            Direction.SHORT_RATIO,
            500.0,
            5000.0,
            base,
            base + 3 * 3_600_000,
            a_rates,
            b_rates,
            a_marks,
            b_marks,
        )
        # SHORT inverts both signs: −500×100×0.006 + 5000×10×0.0015 = −300 + 75 = −225
        assert cost == pytest.approx(-225.0)

    def test_zero_duration(self) -> None:
        base = 1_700_000_000_000
        a = self._series(base, [0.001])
        b = self._series(base, [0.001])
        marks = self._series(base, [100.0])
        assert (
            compute_funding_cost(
                Direction.LONG_RATIO, 500.0, 5000.0, base, base, a, b, marks, marks
            )
            == 0.0
        )

    def test_raises_on_gap(self) -> None:
        base = 1_700_000_000_000
        # Only 2 records but we ask for 3 hours
        a = self._series(base, [0.001, 0.001])
        b = self._series(base, [0.001, 0.001])
        marks = self._series(base, [100.0, 100.0])
        with pytest.raises(ValueError, match="Funding data gap"):
            compute_funding_cost(
                Direction.LONG_RATIO,
                500.0,
                5000.0,
                base,
                base + 3 * 3_600_000,
                a,
                b,
                marks,
                marks,
            )


class TestAccrueHourlyFunding:
    @staticmethod
    def _make_position(direction: Direction) -> OpenPosition:
        return OpenPosition(
            pair=PairConfig("BTC", "ETH"),
            direction=direction,
            entry_z=-2.5,
            entry_price_a=100.0,
            entry_price_b=10.0,
            entry_time_ms=0,
            entry_correlation=0.9,
            filled_size_a=0.5,  # 0.5 BTC
            filled_size_b=5.0,  # 5 ETH
        )

    # signed sizes:
    #   LONG_RATIO  → (+0.5 BTC, -5 ETH)
    #   SHORT_RATIO → (-0.5 BTC, +5 ETH)
    # leg charge per hour = signed_size × mark × rate
    # marks = {BTC: 100, ETH: 10}, rates = {BTC: 0.0002, ETH: 0.0001}
    #   long  charge = 0.5*100*0.0002 + (-5)*10*0.0001 = 0.01 − 0.005 = 0.005
    #   short charge = -0.5*100*0.0002 + 5*10*0.0001  = -0.01 + 0.005 = -0.005

    def test_long_ratio_accrues_per_leg(self) -> None:
        pos = self._make_position(Direction.LONG_RATIO)
        positions: dict[str, OpenPosition | None] = {pos.pair.label: pos}
        accrue_hourly_funding(
            positions,
            {"BTC": 0.0002, "ETH": 0.0001},
            marks={"BTC": 100.0, "ETH": 10.0},
        )
        assert pos.funding_paid == pytest.approx(0.005)

    def test_short_ratio_accrues_per_leg(self) -> None:
        pos = self._make_position(Direction.SHORT_RATIO)
        positions: dict[str, OpenPosition | None] = {pos.pair.label: pos}
        accrue_hourly_funding(
            positions,
            {"BTC": 0.0002, "ETH": 0.0001},
            marks={"BTC": 100.0, "ETH": 10.0},
        )
        assert pos.funding_paid == pytest.approx(-0.005)

    def test_accrues_additively_across_calls(self) -> None:
        pos = self._make_position(Direction.LONG_RATIO)
        positions: dict[str, OpenPosition | None] = {pos.pair.label: pos}
        for _ in range(3):
            accrue_hourly_funding(
                positions,
                {"BTC": 0.0002, "ETH": 0.0001},
                marks={"BTC": 100.0, "ETH": 10.0},
            )
        assert pos.funding_paid == pytest.approx(3 * 0.005)

    def test_skips_position_with_missing_rate(self) -> None:
        pos = self._make_position(Direction.LONG_RATIO)
        positions: dict[str, OpenPosition | None] = {pos.pair.label: pos}
        accrue_hourly_funding(
            positions, {"BTC": 0.0002}, marks={"BTC": 100.0, "ETH": 10.0}
        )  # ETH rate missing
        assert pos.funding_paid == 0.0

    def test_skips_position_with_missing_mark(self) -> None:
        pos = self._make_position(Direction.LONG_RATIO)
        positions: dict[str, OpenPosition | None] = {pos.pair.label: pos}
        accrue_hourly_funding(
            positions, {"BTC": 0.0002, "ETH": 0.0001}, marks={"BTC": 100.0}
        )  # ETH mark missing
        assert pos.funding_paid == 0.0

    def test_skips_none_positions(self) -> None:
        positions: dict[str, OpenPosition | None] = {"BTC/ETH": None}
        accrue_hourly_funding(
            positions,
            {"BTC": 0.0002, "ETH": 0.0001},
            marks={"BTC": 100.0, "ETH": 10.0},
        )
        assert positions["BTC/ETH"] is None


class TestAccrueActualFunding:
    """Per-leg attribution of HL userFunding deltas. Linearity makes this exact
    even when cross-pair coin overlap nets to zero on the exchange side."""

    @staticmethod
    def _make_position(
        coin_a: str, coin_b: str, direction: Direction, size_a: float, size_b: float
    ) -> OpenPosition:
        return OpenPosition(
            pair=PairConfig(coin_a, coin_b),
            direction=direction,
            entry_z=0.0,
            entry_price_a=100.0,
            entry_price_b=10.0,
            entry_time_ms=0,
            entry_correlation=0.9,
            filled_size_a=size_a,
            filled_size_b=size_b,
        )

    def test_distributes_to_single_leg(self) -> None:
        from hypemm.funding import accrue_actual_funding

        pos = self._make_position("BTC", "ETH", Direction.LONG_RATIO, 0.5, 5.0)
        positions: dict[str, OpenPosition | None] = {pos.pair.label: pos}
        # BTC funding event: HL charged us $0.01 (matches modeled long BTC × rate × mark)
        events = [
            {
                "time": 1_700_000_000_000,
                "delta": {"type": "funding", "coin": "BTC", "usdc": "0.01"},
            }
        ]
        accrue_actual_funding(positions, events)
        assert pos.funding_paid_actual == pytest.approx(0.01)

    def test_cross_pair_overlap_cancels_to_match_hl(self) -> None:
        """Two pairs holding SOL with opposite signs net to $0 at HL.

        Pair 1: long SOL 0.3.  Pair 2: short SOL 0.3.  Net = 0 → HL bills $0.
        Each pair individually owes/earns funding; allocation must split the
        $0 equally and produce funding_paid_actual = 0 on each.
        """
        from hypemm.funding import accrue_actual_funding

        pos1 = self._make_position("LINK", "SOL", Direction.LONG_RATIO, 1.0, 0.3)
        pos2 = self._make_position("SOL", "AVAX", Direction.LONG_RATIO, 0.3, 0.5)
        positions: dict[str, OpenPosition | None] = {
            pos1.pair.label: pos1,
            pos2.pair.label: pos2,
        }
        # signed SOL sizes: pos1 leg_b = -0.3 (short SOL), pos2 leg_a = +0.3 (long).
        # Net = 0 → HL bills $0.
        events = [
            {
                "time": 1_700_000_000_000,
                "delta": {"type": "funding", "coin": "SOL", "usdc": "0.0"},
            }
        ]
        accrue_actual_funding(positions, events)
        assert pos1.funding_paid_actual == 0.0
        assert pos2.funding_paid_actual == 0.0

    def test_asymmetric_legs_split_proportionally_to_signed_size(self) -> None:
        """Two legs holding SOL with magnitudes that don't cancel:
        leg1 is +10 SOL (long), leg2 is -2 SOL (short). Net = +8.
        HL charges net × mark × rate. Each leg's actual_funding must equal
        signed × mark × rate (i.e., what they'd pay in isolation), summing
        back to the HL total."""
        from hypemm.funding import accrue_actual_funding

        pos1 = self._make_position("LINK", "SOL", Direction.SHORT_RATIO, 1.0, 10.0)
        # SHORT_RATIO LINK/SOL → -1 LINK, +10 SOL  (long SOL)
        pos2 = self._make_position("SOL", "AVAX", Direction.SHORT_RATIO, 2.0, 0.5)
        # SHORT_RATIO SOL/AVAX → -2 SOL, +0.5 AVAX  (short SOL)
        positions: dict[str, OpenPosition | None] = {
            pos1.pair.label: pos1,
            pos2.pair.label: pos2,
        }
        # Net SOL = +10 + (-2) = +8. HL bill (e.g., $0.80 = 8 × $100 × 0.001).
        events = [
            {
                "time": 1_700_000_000_000,
                "delta": {"type": "funding", "coin": "SOL", "usdc": "0.80"},
            }
        ]
        accrue_actual_funding(positions, events)
        # Linearity: leg_actual = total × (signed / net_signed)
        # pos1 (+10): 0.80 × (10 / 8) = 1.00
        # pos2 (-2):  0.80 × (-2 / 8) = -0.20
        # Sum = 0.80 ✓
        assert pos1.funding_paid_actual == pytest.approx(1.00)
        assert pos2.funding_paid_actual == pytest.approx(-0.20)

    def test_drops_event_for_unheld_coin(self) -> None:
        """If a userFunding event arrives for a coin no open position holds,
        we drop it (event arrived after the position closed)."""
        from hypemm.funding import accrue_actual_funding

        pos = self._make_position("BTC", "ETH", Direction.LONG_RATIO, 0.5, 5.0)
        positions: dict[str, OpenPosition | None] = {pos.pair.label: pos}
        events = [
            {
                "time": 1_700_000_000_000,
                "delta": {"type": "funding", "coin": "DOGE", "usdc": "0.05"},
            }
        ]
        accrue_actual_funding(positions, events)
        assert pos.funding_paid_actual == 0.0
