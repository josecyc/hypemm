"""Validation pipeline: correlation stability, orderbook depth, and go/no-go synthesis."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from hypemm.config import GateConfig, InfraConfig, StrategyConfig
from hypemm.math import rolling_correlation
from hypemm.models import BacktestResult

logger = logging.getLogger(__name__)

CORR_LOW = 0.5


# -- Result types --


@dataclass(frozen=True)
class GateResult:
    """Outcome of a single validation gate."""

    gate: str
    passed: bool
    detail: str


# -- Correlation analysis (Gate 2) --


def compute_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """Compute hourly log returns."""
    result: pd.DataFrame = np.log(prices / prices.shift(1)).dropna()
    return result


def correlation_regimes(
    corr_series: np.ndarray,
    high_threshold: float,
) -> dict[str, float]:
    """Classify correlation into HIGH/MEDIUM/LOW regimes."""
    valid = corr_series[~np.isnan(corr_series)]
    total = len(valid)
    if total == 0:
        return {"high_pct": 0, "med_pct": 0, "low_pct": 0, "mean": 0}

    high = int(np.sum(valid > high_threshold))
    low = int(np.sum(valid < CORR_LOW))
    med = total - high - low

    return {
        "high_pct": high / total * 100,
        "med_pct": med / total * 100,
        "low_pct": low / total * 100,
        "mean": float(np.nanmean(valid)),
        "std": float(np.nanstd(valid)),
        "min": float(np.nanmin(valid)),
    }


def find_breakdowns(
    corr_series: np.ndarray,
    timestamps: pd.DatetimeIndex,
    threshold: float = CORR_LOW,
) -> list[dict[str, object]]:
    """Find continuous periods where correlation is below threshold."""
    below = corr_series < threshold
    breakdowns: list[dict[str, object]] = []
    in_breakdown = False
    start_idx = 0

    for i in range(len(corr_series)):
        if np.isnan(corr_series[i]):
            continue
        if below[i] and not in_breakdown:
            in_breakdown = True
            start_idx = i
        elif not below[i] and in_breakdown:
            in_breakdown = False
            duration_hours = i - start_idx
            min_corr = float(np.nanmin(corr_series[start_idx:i]))
            breakdowns.append(
                {
                    "start": str(timestamps[start_idx]),
                    "end": str(timestamps[i]),
                    "duration_hours": duration_hours,
                    "min_corr": min_corr,
                }
            )

    if in_breakdown:
        duration_hours = len(corr_series) - start_idx
        min_corr = float(np.nanmin(corr_series[start_idx:]))
        breakdowns.append(
            {
                "start": str(timestamps[start_idx]),
                "end": str(timestamps[-1]),
                "duration_hours": duration_hours,
                "min_corr": min_corr,
            }
        )

    return breakdowns


def compute_correlation_stability(
    prices: pd.DataFrame,
    config: StrategyConfig,
) -> tuple[list[dict[str, object]], dict[str, list[dict[str, object]]]]:
    """Compute correlation regimes and breakdowns for all pairs.

    Returns (regime_results, all_breakdowns).
    """
    returns = compute_returns(prices)

    regime_results: list[dict[str, object]] = []
    all_breakdowns: dict[str, list[dict[str, object]]] = {}

    for pair in config.pairs:
        ret_a = returns[pair.coin_a].values
        ret_b = returns[pair.coin_b].values
        corr = rolling_correlation(ret_a, ret_b, config.corr_window_hours)
        regimes = correlation_regimes(corr, config.corr_threshold)
        regime_results.append({"pair": pair.label, **regimes})

        bds = find_breakdowns(corr, returns.index, CORR_LOW)
        all_breakdowns[pair.label] = bds
        logger.info(
            "%s: mean=%.3f, HIGH=%.0f%%, breakdowns=%d",
            pair.label,
            regimes["mean"],
            regimes["high_pct"],
            len(bds),
        )

    return regime_results, all_breakdowns


def check_correlation_gate(
    regimes: list[dict[str, object]],
    breakdowns: dict[str, list[dict[str, object]]],
    gate_config: GateConfig,
) -> GateResult:
    """Check whether correlation stability passes the gate."""
    all_high = all(float(str(r["high_pct"])) >= gate_config.min_high_corr_pct for r in regimes)
    max_breakdown = 0
    for bds_list in breakdowns.values():
        for bd in bds_list:
            dur = int(str(bd["duration_hours"]))
            if dur > max_breakdown:
                max_breakdown = dur

    passed = all_high and max_breakdown <= gate_config.max_breakdown_hours
    detail = f"all_high={all_high}, max_breakdown={max_breakdown}h"
    logger.info("Correlation gate: %s (%s)", "PASS" if passed else "FAIL", detail)
    return GateResult(gate="correlation", passed=passed, detail=detail)


# -- Orderbook analysis (Gate 3) --


def fetch_book(client: httpx.Client, url: str, coin: str) -> dict[str, object] | None:
    """Fetch L2 book snapshot."""
    r = client.post(url, json={"type": "l2Book", "coin": coin}, timeout=10)
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def analyze_book(
    data: dict[str, object], depth_bps_levels: tuple[int, ...] = (2, 5, 10, 25, 50)
) -> dict[str, float]:
    """Analyze a single L2 book snapshot."""
    levels = data.get("levels", [])
    if not isinstance(levels, list) or len(levels) < 2:
        return {}

    bids_raw = levels[0]
    asks_raw = levels[1]
    if not bids_raw or not asks_raw:
        return {}

    bids = [(float(lv["px"]), float(lv["sz"])) for lv in bids_raw]
    asks = [(float(lv["px"]), float(lv["sz"])) for lv in asks_raw]

    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2
    if mid <= 0:
        return {}

    spread_bps = (best_ask - best_bid) / mid * 10_000
    result: dict[str, float] = {"mid": mid, "spread_bps": spread_bps}

    for bps in depth_bps_levels:
        threshold = mid * bps / 10_000
        bid_depth = sum(px * sz for px, sz in bids if mid - px <= threshold)
        ask_depth = sum(px * sz for px, sz in asks if px - mid <= threshold)
        result[f"depth_{bps}bps"] = bid_depth + ask_depth

    return result


def fill_rating(avg_depth_5bps: float, avg_depth_10bps: float, target: float) -> str:
    """Assess fill feasibility for target notional."""
    if avg_depth_5bps > target * 2:
        return "Easy"
    if avg_depth_5bps > target:
        return "Likely"
    if avg_depth_10bps > target:
        return "Tight"
    return "Difficult"


def collect_orderbook_data(
    config: StrategyConfig,
    infra: InfraConfig,
    gate_config: GateConfig,
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, str]]]:
    """Collect orderbook snapshots and compute stats.

    Returns (coin_stats, pair_viability).
    """
    coins = config.all_coins
    n_snapshots = gate_config.ob_collection_duration_sec // gate_config.ob_snapshot_interval_sec

    logger.info(
        "Collecting %d snapshots over %d minutes",
        n_snapshots,
        gate_config.ob_collection_duration_sec // 60,
    )

    all_snapshots: dict[str, list[dict[str, float]]] = {c: [] for c in coins}

    with httpx.Client() as client:
        for snap_i in range(n_snapshots):
            snap_time = datetime.now(timezone.utc)
            logger.info(
                "Snapshot %d/%d (%s)",
                snap_i + 1,
                n_snapshots,
                snap_time.strftime("%H:%M:%S"),
            )

            for coin in coins:
                time.sleep(infra.rate_limit_sec)
                data = fetch_book(client, infra.rest_url, coin)
                if data is not None:
                    analysis = analyze_book(data, gate_config.depth_bps_levels)
                    if analysis:
                        all_snapshots[coin].append(analysis)

            if snap_i < n_snapshots - 1:
                elapsed = (datetime.now(timezone.utc) - snap_time).total_seconds()
                wait = max(0, gate_config.ob_snapshot_interval_sec - elapsed)
                if wait > 0:
                    time.sleep(wait)

    coin_stats: dict[str, dict[str, object]] = {}
    for coin in coins:
        snaps = all_snapshots[coin]
        if not snaps:
            continue

        avg_spread = sum(s["spread_bps"] for s in snaps) / len(snaps)
        depths: dict[int, float] = {}
        for bps in gate_config.depth_bps_levels:
            key = f"depth_{bps}bps"
            vals = [s[key] for s in snaps if key in s]
            depths[bps] = sum(vals) / len(vals) if vals else 0

        rating = fill_rating(depths.get(5, 0), depths.get(10, 0), config.notional_per_leg)
        coin_stats[coin] = {
            "avg_spread_bps": avg_spread,
            "depths": depths,
            "rating": rating,
            "n_snapshots": len(snaps),
        }
        logger.info(
            "%s: spread=%.1f bps, depth@10bps=$%,.0f, rating=%s",
            coin,
            avg_spread,
            depths.get(10, 0),
            rating,
        )

    pair_viability: dict[str, dict[str, str]] = {}
    for pair in config.pairs:
        ra = str(coin_stats.get(pair.coin_a, {}).get("rating", "Unknown"))
        rb = str(coin_stats.get(pair.coin_b, {}).get("rating", "Unknown"))
        if ra == "Easy" and rb == "Easy":
            pair_viability[pair.label] = {"viable": "YES", "rec_size": "$50K"}
        elif "Difficult" in (ra, rb):
            pair_viability[pair.label] = {"viable": "NO", "rec_size": "$10K max"}
        else:
            pair_viability[pair.label] = {"viable": "MAYBE", "rec_size": "$25K"}

    return coin_stats, pair_viability


def check_orderbook_gate(
    coin_stats: dict[str, dict[str, object]],
    pair_viability: dict[str, dict[str, str]],
    gate_config: GateConfig,
) -> GateResult:
    """Check whether orderbook depth passes the gate."""
    easy_pairs = sum(1 for pv in pair_viability.values() if pv.get("viable") == "YES")
    passed = easy_pairs >= gate_config.min_easy_pairs
    detail = f"easy_pairs={easy_pairs}, required={gate_config.min_easy_pairs}"
    logger.info("Orderbook gate: %s (%s)", "PASS" if passed else "FAIL", detail)
    return GateResult(gate="orderbook", passed=passed, detail=detail)


# -- Backtest gate (Gate 1) --


def check_backtest_gate(result: BacktestResult, gate_config: GateConfig) -> GateResult:
    """Check whether backtest results pass the gate."""
    passed = result.sharpe >= gate_config.min_sharpe
    detail = f"sharpe={result.sharpe:.2f}, required={gate_config.min_sharpe}"
    logger.info("Backtest gate: %s (%s)", "PASS" if passed else "FAIL", detail)
    return GateResult(gate="backtest", passed=passed, detail=detail)


# -- Synthesis --


def load_json(path: Path) -> dict[str, object]:
    """Load a JSON file or return empty dict if missing."""
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)  # type: ignore[no-any-return]


def run_synthesis(reports_dir: Path) -> str:
    """Combine analysis results from JSON files and produce overall verdict.

    Returns "GO", "NO-GO", "CONDITIONAL", or "INCOMPLETE".
    """
    bt = load_json(reports_dir / "backtest_summary.json")
    corr = load_json(reports_dir / "correlation_analysis.json")
    ob = load_json(reports_dir / "orderbook_analysis.json")

    missing = []
    if not bt:
        missing.append("backtest_summary.json")
    if not corr:
        missing.append("correlation_analysis.json")
    if not ob:
        missing.append("orderbook_analysis.json")

    if missing:
        logger.warning("Missing data files: %s", ", ".join(missing))
        return "INCOMPLETE"

    v1 = str(bt.get("verdict", "UNKNOWN"))
    v2 = str(corr.get("verdict", "UNKNOWN"))
    v3 = str(ob.get("verdict", "UNKNOWN"))

    logger.info("Step 1 (Backtest):    %s", v1)
    logger.info("Step 2 (Correlation): %s", v2)
    logger.info("Step 3 (Orderbook):   %s", v3)

    verdicts = [v1, v2, v3]
    n_pass = sum(1 for v in verdicts if v == "PASS")
    n_fail = sum(1 for v in verdicts if v == "FAIL")

    if n_pass == 3:
        overall = "GO"
    elif n_fail >= 2:
        overall = "NO-GO"
    else:
        overall = "CONDITIONAL"

    logger.info("Overall verdict: %s", overall)
    return overall


def run_validation(
    prices: pd.DataFrame,
    config: StrategyConfig,
    infra: InfraConfig,
    gate_config: GateConfig,
) -> list[GateResult]:
    """Run the full validation pipeline: backtest, correlation, orderbook.

    Returns list of GateResult. Stops early if a gate fails.
    """
    from hypemm.backtest import run_backtest_all_pairs, summarize_backtest

    results: list[GateResult] = []

    # Gate 1: Backtest
    logger.info("=== Gate 1: Backtest ===")
    trades = run_backtest_all_pairs(prices, config)
    bt_result = summarize_backtest(trades, prices)
    gate1 = check_backtest_gate(bt_result, gate_config)
    results.append(gate1)
    if not gate1.passed:
        logger.info("Backtest gate failed — stopping early")
        return results

    # Gate 2: Correlation
    logger.info("=== Gate 2: Correlation ===")
    regimes, breakdowns = compute_correlation_stability(prices, config)
    gate2 = check_correlation_gate(regimes, breakdowns, gate_config)
    results.append(gate2)
    if not gate2.passed:
        logger.info("Correlation gate failed — stopping early")
        return results

    # Gate 3: Orderbook
    logger.info("=== Gate 3: Orderbook ===")
    coin_stats, pair_viability = collect_orderbook_data(config, infra, gate_config)
    gate3 = check_orderbook_gate(coin_stats, pair_viability, gate_config)
    results.append(gate3)

    n_pass = sum(1 for g in results if g.passed)
    if n_pass == 3:
        logger.info("Final verdict: GO")
    else:
        logger.info("Final verdict: CONDITIONAL")

    return results
