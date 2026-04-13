"""Live orderbook depth collection and analysis."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

import httpx

from hypemm.config import (
    DEPTH_BPS_LEVELS,
    GATE3_MIN_EASY_PAIRS,
    OB_COLLECTION_DURATION_SEC,
    OB_SNAPSHOT_INTERVAL_SEC,
    InfraConfig,
    StrategyConfig,
)

logger = logging.getLogger(__name__)


def fetch_book(client: httpx.Client, url: str, coin: str) -> dict[str, object] | None:
    """Fetch L2 book snapshot."""
    r = client.post(url, json={"type": "l2Book", "coin": coin}, timeout=10)
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def analyze_book(data: dict[str, object]) -> dict[str, float]:
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

    for bps in DEPTH_BPS_LEVELS:
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


def run_orderbook_analysis(
    config: StrategyConfig,
    infra: InfraConfig,
) -> str:
    """Collect orderbook snapshots and assess execution feasibility.

    Returns verdict string.
    """
    reports_dir = infra.reports_dir
    snapshots_dir = infra.snapshots_dir
    reports_dir.mkdir(parents=True, exist_ok=True)
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    coins = config.all_coins
    n_snapshots = OB_COLLECTION_DURATION_SEC // OB_SNAPSHOT_INTERVAL_SEC

    logger.info(
        "Collecting %d snapshots over %d minutes",
        n_snapshots,
        OB_COLLECTION_DURATION_SEC // 60,
    )

    all_snapshots: dict[str, list[dict[str, float]]] = {c: [] for c in coins}

    with httpx.Client() as client:
        for snap_i in range(n_snapshots):
            snap_time = datetime.now(timezone.utc)
            logger.info(
                "Snapshot %d/%d (%s)", snap_i + 1, n_snapshots, snap_time.strftime("%H:%M:%S")
            )

            for coin in coins:
                time.sleep(infra.rate_limit_sec)
                data = fetch_book(client, infra.rest_url, coin)
                if data is not None:
                    analysis = analyze_book(data)
                    if analysis:
                        all_snapshots[coin].append(analysis)

            if snap_i < n_snapshots - 1:
                elapsed = (datetime.now(timezone.utc) - snap_time).total_seconds()
                wait = max(0, OB_SNAPSHOT_INTERVAL_SEC - elapsed)
                if wait > 0:
                    time.sleep(wait)

    # Compute per-coin stats
    coin_stats: dict[str, dict[str, object]] = {}
    easy_pairs = 0

    for coin in coins:
        snaps = all_snapshots[coin]
        if not snaps:
            continue

        avg_spread = sum(s["spread_bps"] for s in snaps) / len(snaps)
        depths: dict[int, float] = {}
        for bps in DEPTH_BPS_LEVELS:
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

    # Pair viability
    pair_viability: dict[str, dict[str, str]] = {}
    for pair in config.pairs:
        ra = str(coin_stats.get(pair.coin_a, {}).get("rating", "Unknown"))
        rb = str(coin_stats.get(pair.coin_b, {}).get("rating", "Unknown"))
        if ra == "Easy" and rb == "Easy":
            pair_viability[pair.label] = {"viable": "YES", "rec_size": "$50K"}
            easy_pairs += 1
        elif "Difficult" in (ra, rb):
            pair_viability[pair.label] = {"viable": "NO", "rec_size": "$10K max"}
        else:
            pair_viability[pair.label] = {"viable": "MAYBE", "rec_size": "$25K"}

    verdict = "PASS" if easy_pairs >= GATE3_MIN_EASY_PAIRS else "FAIL"
    logger.info("Orderbook analysis verdict: %s (easy pairs: %d)", verdict, easy_pairs)

    # Serialize coin stats for JSON output
    serialized_stats: dict[str, object] = {}
    for k, v in coin_stats.items():
        depths_raw = v.get("depths", {})
        depths_dict = depths_raw if isinstance(depths_raw, dict) else {}
        stat_entry = {sk: sv for sk, sv in v.items() if sk != "depths"}
        stat_entry["depths"] = {str(dk): dv for dk, dv in depths_dict.items()}
        serialized_stats[k] = stat_entry

    analysis_result: dict[str, object] = {
        "coin_stats": serialized_stats,
        "pair_viability": pair_viability,
        "verdict": verdict,
    }
    with open(reports_dir / "orderbook_analysis.json", "w") as f:
        json.dump(analysis_result, f, indent=2, default=str)

    return verdict
