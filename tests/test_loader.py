"""Tests for candle CSV loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from hypemm.data.loader import load_candles


def _write_csv(path: Path, coin: str, rows: list[tuple[int, float]]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    csv_path = path / f"{coin}_1h.csv"
    with open(csv_path, "w") as f:
        f.write("timestamp,open,high,low,close,volume\n")
        for ts, close in rows:
            f.write(f"{ts},{close},{close},{close},{close},1000\n")


class TestLoadCandles:
    def test_loads_and_combines(self, tmp_path: Path) -> None:
        base_ts = 1_704_067_200_000
        rows_a = [(base_ts + i * 3_600_000, 15.0 + i * 0.1) for i in range(10)]
        rows_b = [(base_ts + i * 3_600_000, 150.0 + i) for i in range(10)]
        _write_csv(tmp_path, "LINK", rows_a)
        _write_csv(tmp_path, "SOL", rows_b)

        df = load_candles(tmp_path, ["LINK", "SOL"])
        assert "LINK" in df.columns
        assert "SOL" in df.columns
        assert len(df) == 10

    def test_deduplicates(self, tmp_path: Path) -> None:
        base_ts = 1_704_067_200_000
        rows = [(base_ts, 15.0), (base_ts, 15.5), (base_ts + 3_600_000, 16.0)]
        _write_csv(tmp_path, "LINK", rows)
        _write_csv(tmp_path, "SOL", [(base_ts, 150.0), (base_ts + 3_600_000, 151.0)])

        df = load_candles(tmp_path, ["LINK", "SOL"])
        assert len(df) == 2

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Missing"):
            load_candles(tmp_path, ["NONEXISTENT"])

    def test_forward_fills_gaps(self, tmp_path: Path) -> None:
        base_ts = 1_704_067_200_000
        rows_a = [(base_ts + i * 3_600_000, 15.0) for i in range(5)]
        rows_b = [(base_ts, 150.0), (base_ts + 3_600_000 * 4, 155.0)]
        _write_csv(tmp_path, "LINK", rows_a)
        _write_csv(tmp_path, "SOL", rows_b)

        df = load_candles(tmp_path, ["LINK", "SOL"])
        assert not df["SOL"].isna().any()
