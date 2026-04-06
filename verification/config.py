"""Shared configuration for the stat arb verification pipeline."""
from __future__ import annotations

from pathlib import Path

# ── API ──────────────────────────────────────────────────────────────────
REST_URL = "https://api.hyperliquid.xyz/info"
RATE_LIMIT_SEC = 0.7
CHUNK_DAYS = 30

# ── Assets & Pairs ──────────────────────────────────────────────────────
COINS = ["ETH", "SOL", "BTC", "AVAX", "DOGE", "LINK"]
PAIRS = [
    ("ETH", "SOL"),
    ("ETH", "BTC"),
    ("SOL", "AVAX"),
    ("DOGE", "AVAX"),
    ("LINK", "SOL"),
    ("BTC", "SOL"),
]

# ── Strategy parameters (base case) ────────────────────────────────────
LOOKBACK_HOURS = 48
ENTRY_Z = 2.0
EXIT_Z = 0.5
MAX_HOLD_HOURS = 48
STOP_LOSS_Z = 4.0
NOTIONAL_PER_LEG = 50_000
COST_PER_SIDE_BPS = 2          # 0.02% per side, maker
COOLDOWN_HOURS = 2             # skip 2h after exit before re-entry

# ── Parameter sweep grid ────────────────────────────────────────────────
SWEEP_LOOKBACKS = [24, 48, 72]
SWEEP_ENTRY_Z = [1.5, 2.0, 2.5]

# ── Correlation analysis ────────────────────────────────────────────────
CORR_WINDOW_HOURS = 168        # 7-day rolling correlation
CORR_HIGH = 0.7
CORR_LOW = 0.5

# ── Orderbook depth ────────────────────────────────────────────────────
DEPTH_BPS_LEVELS = [2, 5, 10, 25, 50]
OB_SNAPSHOT_INTERVAL_SEC = 300  # 5 minutes
OB_COLLECTION_DURATION_SEC = 7200  # 2 hours

# ── Gate thresholds ─────────────────────────────────────────────────────
GATE1_MIN_PROFITABLE_MONTHS = 4   # out of 6
GATE1_MIN_PROFITABLE_PARAMS = 7   # out of 9
GATE1_MIN_SHARPE = 1.0
GATE1_MAX_MONTH_DD = 15_000

GATE2_MIN_HIGH_CORR_PCT = 65     # % of time correlation > 0.7
GATE2_MIN_HIGH_CORR_WR = 75      # win rate % when correlation > 0.7
GATE2_MAX_BREAKDOWN_HOURS = 336   # 2 weeks

GATE3_MIN_EASY_PAIRS = 3
GATE3_MIN_DEPTH_10BPS = 25_000

# ── Paths ───────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
CANDLES_DIR = DATA_DIR / "candles"
REPORTS_DIR = DATA_DIR / "reports"
SNAPSHOTS_DIR = DATA_DIR / "orderbook_snapshots"
