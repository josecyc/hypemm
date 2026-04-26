"""Strategy and infrastructure configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from hypemm.models import PairConfig

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        import toml as tomllib  # type: ignore[no-redef]


@dataclass(frozen=True)
class StrategyConfig:
    """All tunable strategy parameters. Immutable so parameter sweeps create new instances."""

    pairs: tuple[PairConfig, ...]
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
    # Stationarity gate: halt pair when Hurst > threshold or ADF > threshold
    hurst_window_hours: int = 168
    hurst_threshold: float = -1.0  # negative = disabled; 0.5 = halt when trending
    adf_threshold: float = 0.0  # 0 = disabled; -2.86 = halt when ADF > -2.86 (5% level)
    # Progress-exit: exit early if z hasn't improved after N hours
    progress_exit_hours: int = 0  # 0 = disabled
    progress_exit_pct: float = 0.10  # require 10% improvement in |z|

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
    market_data_provider: str = "hyperliquid"
    lookback_days: int = 540
    binance_futures_url: str = "https://fapi.binance.com"
    rate_limit_sec: float = 0.7
    poll_interval_sec: int = 60
    data_dir: Path = field(default_factory=lambda: Path("data"))
    # Live trading
    leverage: int = 5
    is_cross_margin: bool = True
    max_slippage_bps: float = 5.0  # abort fill if VWAP > N bps from signal mid
    ioc_aggression_bps: float = 10.0  # crossing limit price = mid +/- this many bps
    fill_poll_seconds: float = 0.5
    fill_timeout_seconds: float = 30.0

    @property
    def candles_dir(self) -> Path:
        return self.data_dir / "candles"

    @property
    def funding_dir(self) -> Path:
        return self.data_dir / "funding"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def paper_trades_dir(self) -> Path:
        return self.data_dir / "paper_trades"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "orderbook_snapshots"


@dataclass(frozen=True)
class RiskConfig:
    """Portfolio-level kill switch and drift detection thresholds.

    Calibrated from THESIS section 5.3.8 against the 7-month backtest's
    -$19,657 max simultaneous unrealized loss.
    """

    # Concurrent unrealized P&L (mark-to-market across all open positions)
    unrealized_warn: float = -10_000.0
    unrealized_halt: float = -15_000.0

    # 24-hour realized P&L
    daily_loss_halt: float = -5_000.0

    # Strategy drift: rolling win rate
    win_rate_window: int = 30
    win_rate_warn: float = 0.55
    win_rate_min_trades: int = 10

    # Strategy drift: rolling time-stop ratio
    time_stop_window: int = 20
    time_stop_warn_pct: float = 0.30
    time_stop_min_trades: int = 10

    # Correlation breakdown on an active pair
    corr_warn_threshold: float = 0.65


@dataclass(frozen=True)
class GateConfig:
    """Thresholds for the validation gates."""

    min_sharpe: float = 1.0
    min_high_corr_pct: float = 65
    max_breakdown_hours: int = 336
    min_easy_pairs: int = 3
    depth_bps_levels: tuple[int, ...] = (2, 5, 10, 25, 50)
    ob_snapshot_interval_sec: int = 300
    ob_collection_duration_sec: int = 7200


@dataclass(frozen=True)
class SweepConfig:
    """Parameter sweep grid definition."""

    lookbacks: tuple[int, ...] = (24, 48, 72)
    entry_zs: tuple[float, ...] = (1.5, 2.0, 2.5)


@dataclass(frozen=True)
class AppConfig:
    """Top-level application config loaded from TOML."""

    strategy: StrategyConfig
    infra: InfraConfig
    gates: GateConfig
    sweep: SweepConfig
    risk: RiskConfig


def load_config(path: Path) -> AppConfig:
    """Load application config from a TOML file."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    strategy_raw = dict(raw.get("strategy", {}))
    pairs_raw = strategy_raw.pop("pairs", [])
    pairs = tuple(PairConfig(p["coin_a"], p["coin_b"]) for p in pairs_raw)
    strategy = StrategyConfig(pairs=pairs, **strategy_raw)

    infra_raw = dict(raw.get("infra", {}))
    if "data_dir" in infra_raw:
        infra_raw["data_dir"] = Path(infra_raw["data_dir"])
    infra = InfraConfig(**infra_raw)

    gates_raw = dict(raw.get("gates", {}))
    if "depth_bps_levels" in gates_raw:
        gates_raw["depth_bps_levels"] = tuple(gates_raw["depth_bps_levels"])
    gates = GateConfig(**gates_raw)

    sweep_raw = dict(raw.get("sweep", {}))
    if "lookbacks" in sweep_raw:
        sweep_raw["lookbacks"] = tuple(sweep_raw["lookbacks"])
    if "entry_zs" in sweep_raw:
        sweep_raw["entry_zs"] = tuple(sweep_raw["entry_zs"])
    sweep = SweepConfig(**sweep_raw)

    risk_raw = dict(raw.get("risk", {}))
    risk = RiskConfig(**risk_raw)

    return AppConfig(strategy=strategy, infra=infra, gates=gates, sweep=sweep, risk=risk)
