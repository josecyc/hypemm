"""Tests for the correlation gate check."""

from __future__ import annotations

from hypemm.config import GateConfig
from hypemm.correlation import check_correlation_gate


def test_correlation_gate_pass() -> None:
    regimes: list[dict[str, object]] = [{"pair": "A/B", "high_pct": 80.0}]
    breakdowns: dict[str, list[dict[str, object]]] = {
        "A/B": [{"duration_hours": 10}],
    }
    gate = check_correlation_gate(regimes, breakdowns, GateConfig())
    assert gate.passed is True
    assert gate.gate == "correlation"


def test_correlation_gate_fail_low_corr() -> None:
    regimes: list[dict[str, object]] = [{"pair": "A/B", "high_pct": 50.0}]
    breakdowns: dict[str, list[dict[str, object]]] = {"A/B": []}
    gate = check_correlation_gate(regimes, breakdowns, GateConfig())
    assert gate.passed is False


def test_correlation_gate_fail_long_breakdown() -> None:
    regimes: list[dict[str, object]] = [{"pair": "A/B", "high_pct": 80.0}]
    breakdowns: dict[str, list[dict[str, object]]] = {
        "A/B": [{"duration_hours": 500}],
    }
    gate = check_correlation_gate(regimes, breakdowns, GateConfig())
    assert gate.passed is False
