"""Domain models for the stat arb system."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum

try:
    from enum import StrEnum
except ImportError:
    class StrEnum(str, Enum):
        """Python 3.10-compatible fallback for enum.StrEnum."""


class Direction(IntEnum):
    """Trade direction relative to the price ratio A/B."""

    LONG_RATIO = 1
    SHORT_RATIO = -1

    @property
    def label(self) -> str:
        return "long_ratio" if self == Direction.LONG_RATIO else "short_ratio"


class ExitReason(StrEnum):
    """Why a position was exited."""

    MEAN_REVERT = "mean_revert"
    STOP_LOSS = "stop_loss"
    TIME_STOP = "time_stop"


@dataclass(frozen=True)
class PairConfig:
    """A tradeable pair of coins."""

    coin_a: str
    coin_b: str

    @property
    def label(self) -> str:
        return f"{self.coin_a}/{self.coin_b}"

    @property
    def coins(self) -> tuple[str, str]:
        return (self.coin_a, self.coin_b)


@dataclass(frozen=True)
class Signal:
    """Output of signal computation for one pair at one point in time."""

    pair: PairConfig
    z_score: float
    correlation: float | None
    price_a: float
    price_b: float
    timestamp_ms: int
    n_bars: int
    hurst: float | None = None
    adf_stat: float | None = None


@dataclass
class OpenPosition:
    """An active position in a pair."""

    pair: PairConfig
    direction: Direction
    entry_z: float
    entry_price_a: float
    entry_price_b: float
    entry_time_ms: int
    entry_correlation: float
    hours_held: int = 0
    funding_paid: float = 0.0

    @property
    def direction_str(self) -> str:
        return self.direction.label


@dataclass(frozen=True)
class CompletedTrade:
    """A closed trade with realized P&L."""

    pair_label: str
    direction: Direction
    entry_ts: int
    exit_ts: int
    entry_z: float
    exit_z: float
    hours_held: int
    entry_price_a: float
    entry_price_b: float
    exit_price_a: float
    exit_price_b: float
    pnl_leg_a: float
    pnl_leg_b: float
    gross_pnl: float
    cost: float
    net_pnl: float
    exit_reason: ExitReason
    entry_correlation: float
    funding_cost: float = 0.0
    max_adverse_excursion: float = 0.0


@dataclass(frozen=True)
class EntryOrder:
    """Decision to enter a position. Pending execution confirmation."""

    pair: PairConfig
    direction: Direction
    signal: Signal


@dataclass(frozen=True)
class ExitOrder:
    """Decision to exit a position. Pending execution confirmation."""

    pair: PairConfig
    position: OpenPosition
    reason: ExitReason
    signal: Signal


# -- Exceptions --


class HypeMMError(Exception):
    """Base exception for hypemm."""


class DataFetchError(HypeMMError):
    """Failed to fetch data from exchange."""


class InsufficientDataError(HypeMMError):
    """Not enough data to compute signals."""


class StateCorruptionError(HypeMMError):
    """Saved state is corrupt or incompatible."""


class ConfigurationError(HypeMMError):
    """Invalid configuration."""


# -- Result types --


@dataclass(frozen=True)
class BacktestResult:
    """Summary of a backtest run."""

    trades: list[CompletedTrade]
    total_net: float
    win_rate: float
    sharpe: float
    max_drawdown: float
    monthly: list[dict[str, object]]


@dataclass(frozen=True)
class SweepRow:
    """One row of a parameter sweep result."""

    lookback: int
    entry_z: float
    trades: int
    win_rate: float
    net: float
    daily: float
    max_dd: float
    sharpe: float


@dataclass(frozen=True)
class GateResult:
    """Outcome of a single validation gate."""

    gate: str
    passed: bool
    detail: str

    @property
    def verdict(self) -> str:
        return "PASS" if self.passed else "FAIL"
