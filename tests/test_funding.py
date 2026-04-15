"""Tests for funding rate fetching and cost computation."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from hypemm.funding import (
    _save_csv,
    compute_funding_cost,
    fetch_coin_funding,
    fetch_funding_page,
    load_funding,
)
from hypemm.models import Direction


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
        base = 1_700_000_000_000
        a = self._series(base, [0.0010, 0.0020, 0.0030])  # sum = 0.006
        b = self._series(base, [0.0005, 0.0005, 0.0005])  # sum = 0.0015
        cost = compute_funding_cost(Direction.LONG_RATIO, 50_000, base, base + 3 * 3_600_000, a, b)
        assert cost == pytest.approx(50_000 * (0.006 - 0.0015))

    def test_short_ratio(self) -> None:
        base = 1_700_000_000_000
        a = self._series(base, [0.0010, 0.0020, 0.0030])
        b = self._series(base, [0.0005, 0.0005, 0.0005])
        cost = compute_funding_cost(
            Direction.SHORT_RATIO, 50_000, base, base + 3 * 3_600_000, a, b
        )
        assert cost == pytest.approx(50_000 * (0.0015 - 0.006))

    def test_zero_duration(self) -> None:
        base = 1_700_000_000_000
        a = self._series(base, [0.001])
        b = self._series(base, [0.001])
        assert compute_funding_cost(Direction.LONG_RATIO, 50_000, base, base, a, b) == 0.0

    def test_raises_on_gap(self) -> None:
        base = 1_700_000_000_000
        # Only 2 records but we ask for 3 hours
        a = self._series(base, [0.001, 0.001])
        b = self._series(base, [0.001, 0.001])
        with pytest.raises(ValueError, match="Funding data gap"):
            compute_funding_cost(Direction.LONG_RATIO, 50_000, base, base + 3 * 3_600_000, a, b)
