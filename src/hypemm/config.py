"""Strategy and infrastructure configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from hypemm.models import PairConfig

DEFAULT_PAIRS: tuple[PairConfig, ...] = (
    PairConfig("LINK", "SOL"),
    PairConfig("DOGE", "AVAX"),
    PairConfig("SOL", "AVAX"),
    PairConfig("BTC", "SOL"),
)


def _default_pairs() -> tuple[PairConfig, ...]:
    return DEFAULT_PAIRS


@dataclass(frozen=True)
class StrategyConfig:
    """All tunable strategy parameters. Immutable so parameter sweeps create new instances."""

    lookback_hours: int = 48
    entry_z: float = 2.0
    exit_z: float = 0.5
    max_hold_hours: int = 48
    stop_loss_z: float = 4.0
    notional_per_leg: float = 50_000
    cost_per_side_bps: float = 2.0
    cooldown_hours: int = 2
    corr_window_hours: int = 168
    corr_threshold: float = 0.7
    pairs: tuple[PairConfig, ...] = field(default_factory=_default_pairs)

    @property
    def round_trip_cost(self) -> float:
        """Total cost for a round-trip trade (enter + exit both legs)."""
        return self.notional_per_leg * 2 * self.cost_per_side_bps / 10_000 * 2

    @property
    def all_coins(self) -> list[str]:
        """Deduplicated list of all coins across all pairs."""
        coins: set[str] = set()
        for pair in self.pairs:
            coins.add(pair.coin_a)
            coins.add(pair.coin_b)
        return sorted(coins)


@dataclass(frozen=True)
class InfraConfig:
    """Infrastructure / deployment configuration."""

    rest_url: str = "https://api.hyperliquid.xyz/info"
    rate_limit_sec: float = 0.7
    poll_interval_sec: int = 60
    data_dir: Path = field(default_factory=lambda: Path("data"))

    @property
    def candles_dir(self) -> Path:
        return self.data_dir / "candles"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def paper_trades_dir(self) -> Path:
        return self.data_dir / "paper_trades"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "orderbook_snapshots"


# -- Gate thresholds for the analysis pipeline --

GATE1_MIN_PROFITABLE_MONTHS = 4
GATE1_MIN_PROFITABLE_PARAMS = 7
GATE1_MIN_SHARPE = 1.0
GATE1_MAX_MONTH_DD = 15_000

GATE2_MIN_HIGH_CORR_PCT = 65
GATE2_MIN_HIGH_CORR_WR = 75
GATE2_MAX_BREAKDOWN_HOURS = 336

GATE3_MIN_EASY_PAIRS = 3
GATE3_MIN_DEPTH_10BPS = 25_000

SWEEP_LOOKBACKS = [24, 48, 72]
SWEEP_ENTRY_Z = [1.5, 2.0, 2.5]

DEPTH_BPS_LEVELS = [2, 5, 10, 25, 50]
OB_SNAPSHOT_INTERVAL_SEC = 300
OB_COLLECTION_DURATION_SEC = 7200
