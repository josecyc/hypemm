"""Fetch hourly funding rates from the Hyperliquid API and compute per-trade funding cost."""

from __future__ import annotations

import csv
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd

from hypemm.config import InfraConfig
from hypemm.models import DataFetchError, Direction, OpenPosition

logger = logging.getLogger(__name__)

FUNDING_FIELDS = ["timestamp", "funding_rate", "premium"]
MAX_LOOKBACK_DAYS = 540
PAGE_SIZE = 500  # Hyperliquid's hard cap per fundingHistory response


def fetch_funding_page(
    client: httpx.Client,
    url: str,
    coin: str,
    start_ms: int,
    end_ms: int | None = None,
) -> list[dict[str, float | int]]:
    """Fetch one page of hourly funding records with retries. Up to 500 records."""
    for attempt in range(3):
        try:
            payload: dict[str, object] = {
                "type": "fundingHistory",
                "coin": coin,
                "startTime": start_ms,
            }
            if end_ms is not None:
                payload["endTime"] = end_ms
            r = client.post(url, json=payload, timeout=15.0)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                return []
            return [
                {
                    "timestamp": int(row["time"]),
                    "funding_rate": float(row["fundingRate"]),
                    "premium": float(row["premium"]),
                }
                for row in data
            ]
        except (httpx.HTTPError, httpx.TimeoutException, KeyError, ValueError) as e:
            if attempt < 2:
                time.sleep(2)
            else:
                raise DataFetchError(f"Failed to fetch {coin} funding after 3 attempts: {e}")
    return []


def _existing_max_ts(path: Path) -> int | None:
    """Return the latest timestamp in an existing funding CSV, or None."""
    if not path.exists():
        return None
    max_ts = None
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = int(row["timestamp"])
            if max_ts is None or ts > max_ts:
                max_ts = ts
    return max_ts


def _save_csv(path: Path, rows: list[dict[str, float | int]]) -> int:
    """Write rows to CSV, deduplicating and sorting by timestamp. Returns unique count."""
    seen: set[int | float] = set()
    unique = []
    for r in rows:
        if r["timestamp"] not in seen:
            seen.add(r["timestamp"])
            unique.append(r)
    unique.sort(key=lambda x: x["timestamp"])

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FUNDING_FIELDS)
        writer.writeheader()
        writer.writerows(unique)

    return len(unique)


def fetch_coin_funding(
    client: httpx.Client,
    url: str,
    coin: str,
    funding_dir: Path,
    rate_limit_sec: float,
    force: bool = False,
) -> None:
    """Fetch all available hourly funding records for one coin, paginated."""
    path = funding_dir / f"{coin}_1h.csv"
    now_ms = int(time.time() * 1000)

    existing_max = _existing_max_ts(path) if not force else None
    if existing_max is not None and now_ms - existing_max < 3_600_000:
        logger.info("%s funding: already up-to-date, skipping", coin)
        return

    if existing_max is not None:
        # Incremental: resume from last saved timestamp + 1ms
        start_ms = existing_max + 1
        existing_rows = _read_existing(path)
    else:
        start_ms = now_ms - MAX_LOOKBACK_DAYS * 24 * 3600 * 1000
        existing_rows = []

    all_rows: list[dict[str, float | int]] = list(existing_rows)
    cursor = start_ms

    while cursor < now_ms:
        time.sleep(rate_limit_sec)
        page = fetch_funding_page(client, url, coin, cursor, now_ms)
        if not page:
            break

        all_rows.extend(page)
        last_ts = int(page[-1]["timestamp"])
        first_dt = datetime.fromtimestamp(int(page[0]["timestamp"]) / 1000, tz=timezone.utc)
        last_dt = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc)
        logger.info(
            "%s funding: %s -> %s (%d records)",
            coin,
            first_dt.strftime("%Y-%m-%d %H:%M"),
            last_dt.strftime("%Y-%m-%d %H:%M"),
            len(page),
        )

        if len(page) < PAGE_SIZE:
            break
        cursor = last_ts + 1

    if not all_rows:
        logger.warning("%s funding: no data returned", coin)
        return

    n_unique = _save_csv(path, all_rows)
    logger.info("%s funding: %d unique records saved", coin, n_unique)


def _read_existing(path: Path) -> list[dict[str, float | int]]:
    """Read an existing funding CSV into a list of rows."""
    rows: list[dict[str, float | int]] = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(
                {
                    "timestamp": int(row["timestamp"]),
                    "funding_rate": float(row["funding_rate"]),
                    "premium": float(row["premium"]),
                }
            )
    return rows


def fetch_all_funding(
    coins: list[str],
    infra: InfraConfig,
    force: bool = False,
) -> None:
    """Fetch funding rates for all coins."""
    infra.funding_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Fetching hourly funding data for %s", ", ".join(coins))

    with httpx.Client() as client:
        for coin in coins:
            fetch_coin_funding(
                client,
                infra.rest_url,
                coin,
                infra.funding_dir,
                infra.rate_limit_sec,
                force,
            )

    logger.info("Funding fetch complete")


def load_funding(funding_dir: Path, coins: list[str]) -> pd.DataFrame:
    """Load funding CSVs into a DataFrame with columns = coin funding rates.

    Raises FileNotFoundError if any coin's CSV is missing.
    """
    frames: dict[str, "pd.Series[float]"] = {}

    for coin in coins:
        path = funding_dir / f"{coin}_1h.csv"
        if not path.exists():
            raise FileNotFoundError(f"Missing {path}. Run `hypemm fetch` first.")

        df = pd.read_csv(path)
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("datetime").sort_index()
        df = df[~df.index.duplicated(keep="first")]
        frames[coin] = df["funding_rate"]

    combined = pd.DataFrame(frames)
    combined = combined.ffill()
    combined = combined.dropna()
    return combined


def compute_funding_cost(
    direction: Direction,
    notional: float,
    entry_ts_ms: int,
    exit_ts_ms: int,
    funding_a: "pd.Series[float]",
    funding_b: "pd.Series[float]",
) -> float:
    """Total funding paid (positive = cost to us) over [entry_ts, exit_ts).

    Each hourly funding event charges notional * rate per leg.
    LONG_RATIO (long A, short B):  net rate = rate_A - rate_B
    SHORT_RATIO (short A, long B): net rate = rate_B - rate_A
    """
    if exit_ts_ms <= entry_ts_ms:
        return 0.0

    entry_ts = pd.Timestamp(entry_ts_ms, unit="ms", tz="UTC")
    exit_ts = pd.Timestamp(exit_ts_ms, unit="ms", tz="UTC")

    mask_a = (funding_a.index >= entry_ts) & (funding_a.index < exit_ts)
    mask_b = (funding_b.index >= entry_ts) & (funding_b.index < exit_ts)
    rates_a = funding_a[mask_a]
    rates_b = funding_b[mask_b]

    expected_hours = (exit_ts_ms - entry_ts_ms) // 3_600_000
    if len(rates_a) != expected_hours or len(rates_b) != expected_hours:
        raise ValueError(
            f"Funding data gap: expected {expected_hours} hourly records between "
            f"{entry_ts} and {exit_ts}, got {len(rates_a)} for A and {len(rates_b)} for B"
        )

    sum_a = float(rates_a.sum())
    sum_b = float(rates_b.sum())
    if direction == Direction.LONG_RATIO:
        return notional * (sum_a - sum_b)
    return notional * (sum_b - sum_a)


def fetch_latest_funding_rates(
    client: httpx.Client,
    url: str,
    coins: list[str],
) -> dict[str, float]:
    """Fetch the most recent funding rate for each coin. Returns {coin: rate}.

    Used by the live runner to accrue funding at each hourly boundary.
    Skips coins with no recent record and logs a warning — callers must tolerate
    missing keys. Records are looked up in a 3-hour window to survive brief gaps.
    """
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 3 * 3_600_000
    rates: dict[str, float] = {}
    for coin in coins:
        page = fetch_funding_page(client, url, coin, start_ms)
        if not page:
            logger.warning("No recent funding record for %s", coin)
            continue
        rates[coin] = float(page[-1]["funding_rate"])
    return rates


def accrue_hourly_funding(
    positions: dict[str, OpenPosition | None],
    rates: dict[str, float],
    notional: float,
) -> None:
    """Add one hour of funding to each open position's funding_paid."""
    for pos in positions.values():
        if pos is None:
            continue
        rate_a = rates.get(pos.pair.coin_a)
        rate_b = rates.get(pos.pair.coin_b)
        if rate_a is None or rate_b is None:
            logger.warning(
                "Skipping funding accrual for %s: missing rate for %s or %s",
                pos.pair.label,
                pos.pair.coin_a,
                pos.pair.coin_b,
            )
            continue
        if pos.direction == Direction.LONG_RATIO:
            pos.funding_paid += notional * (rate_a - rate_b)
        else:
            pos.funding_paid += notional * (rate_b - rate_a)
