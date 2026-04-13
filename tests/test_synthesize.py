"""Tests for go/no-go synthesis."""

from __future__ import annotations

import json
from pathlib import Path

from hypemm.analysis.synthesize import load_json, run_synthesis
from hypemm.config import StrategyConfig


def _write_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


class TestLoadJson:
    def test_missing_file(self, tmp_path: Path) -> None:
        result = load_json(tmp_path / "nope.json")
        assert result == {}

    def test_valid_file(self, tmp_path: Path) -> None:
        p = tmp_path / "test.json"
        _write_json(p, {"verdict": "PASS"})
        result = load_json(p)
        assert result["verdict"] == "PASS"


class TestRunSynthesis:
    def _setup_reports(
        self,
        tmp_path: Path,
        bt: str = "PASS",
        corr: str = "PASS",
        ob: str = "PASS",
    ) -> Path:
        _write_json(tmp_path / "backtest_summary.json", {"verdict": bt})
        _write_json(tmp_path / "correlation_analysis.json", {"verdict": corr})
        _write_json(tmp_path / "orderbook_analysis.json", {"verdict": ob})
        return tmp_path

    def test_all_pass_returns_go(self, tmp_path: Path) -> None:
        reports = self._setup_reports(tmp_path)
        assert run_synthesis(reports, StrategyConfig()) == "GO"

    def test_two_fail_returns_no_go(self, tmp_path: Path) -> None:
        reports = self._setup_reports(tmp_path, bt="FAIL", corr="FAIL")
        assert run_synthesis(reports, StrategyConfig()) == "NO-GO"

    def test_one_fail_returns_conditional(self, tmp_path: Path) -> None:
        reports = self._setup_reports(tmp_path, ob="FAIL")
        assert run_synthesis(reports, StrategyConfig()) == "CONDITIONAL"

    def test_missing_files_returns_incomplete(self, tmp_path: Path) -> None:
        assert run_synthesis(tmp_path, StrategyConfig()) == "INCOMPLETE"

    def test_partial_missing_returns_incomplete(self, tmp_path: Path) -> None:
        _write_json(tmp_path / "backtest_summary.json", {"verdict": "PASS"})
        assert run_synthesis(tmp_path, StrategyConfig()) == "INCOMPLETE"
