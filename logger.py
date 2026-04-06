"""CSV logging for historical edge analysis."""

from __future__ import annotations

import csv
import os
from datetime import datetime, timezone

FIELDS = [
    "timestamp",
    "market",
    "hl_spread_bps",
    "ref_spread_bps",
    "spread_ratio",
    "edge_bps",
    "trades_1m",
    "trades_5m",
    "trades_60m",
    "avg_trade_size_usd",
    "depth_5bps",
    "depth_10bps",
    "depth_25bps",
    "depth_50bps",
    "volume_imbalance_pct",
    "funding_rate",
    "oracle_mid_div_bps",
    "verdict",
]


class CSVLogger:
    def __init__(self):
        date = datetime.now(timezone.utc).strftime("%Y%m%d")
        self.path = f"edge_log_{date}.csv"
        if not os.path.exists(self.path):
            with open(self.path, "w", newline="") as f:
                csv.writer(f).writerow(FIELDS)

    def log(self, market: str, metrics: dict) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        row = [ts, market] + [metrics.get(f) for f in FIELDS[2:]]
        with open(self.path, "a", newline="") as f:
            csv.writer(f).writerow(row)
