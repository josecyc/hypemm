"""Tests for orderbook analysis (pure functions, no network calls)."""

from __future__ import annotations

from hypemm.analysis.orderbook import analyze_book, fill_rating


class TestAnalyzeBook:
    def test_valid_book(self) -> None:
        data: dict[str, object] = {
            "levels": [
                [{"px": "99.0", "sz": "100"}, {"px": "98.0", "sz": "200"}],
                [{"px": "101.0", "sz": "150"}, {"px": "102.0", "sz": "250"}],
            ]
        }
        result = analyze_book(data)
        assert result["mid"] == 100.0
        assert result["spread_bps"] == 200.0

    def test_empty_levels(self) -> None:
        assert analyze_book({"levels": []}) == {}

    def test_missing_levels(self) -> None:
        assert analyze_book({}) == {}

    def test_empty_bids(self) -> None:
        data: dict[str, object] = {"levels": [[], [{"px": "100", "sz": "10"}]]}
        assert analyze_book(data) == {}

    def test_depth_computed(self) -> None:
        data: dict[str, object] = {
            "levels": [
                [{"px": "100.0", "sz": "500"}],
                [{"px": "100.02", "sz": "500"}],
            ]
        }
        result = analyze_book(data)
        assert "depth_2bps" in result
        assert "depth_50bps" in result


class TestFillRating:
    def test_easy(self) -> None:
        assert fill_rating(150_000, 200_000, 50_000) == "Easy"

    def test_likely(self) -> None:
        assert fill_rating(60_000, 100_000, 50_000) == "Likely"

    def test_tight(self) -> None:
        assert fill_rating(30_000, 60_000, 50_000) == "Tight"

    def test_difficult(self) -> None:
        assert fill_rating(10_000, 20_000, 50_000) == "Difficult"
