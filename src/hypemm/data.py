"""Fetch hourly candles from the Hyperliquid API and load from CSV."""

from __future__ import annotations

import csv
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pandas as pd

from hypemm.config import InfraConfig
from hypemm.models import DataFetchError

if TYPE_CHECKING:
    from hypemm.config import StrategyConfig
    from hypemm.price_buffer import HourlyPriceBuffer

logger = logging.getLogger(__name__)

CANDLE_FIELDS = ["timestamp", "open", "high", "low", "close", "volume"]
CHUNK_DAYS = 30
BINANCE_KLINES_LIMIT = 1500


def fetch_candles_chunk(
    client: httpx.Client,
    url: str,
    coin: str,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, float | int]]:
    """Fetch one chunk of hourly candles with retries."""
    for attempt in range(3):
        try:
            payload = {
                "type": "candleSnapshot",
                "req": {"coin": coin, "interval": "1h", "startTime": start_ms, "endTime": end_ms},
            }
            r = client.post(url, json=payload, timeout=15.0)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                return []
            return [
                {
                    "timestamp": int(c["t"]),
                    "open": float(c["o"]),
                    "high": float(c["h"]),
                    "low": float(c["l"]),
                    "close": float(c["c"]),
                    "volume": float(c["v"]),
                }
                for c in data
            ]
        except (httpx.HTTPError, httpx.TimeoutException, KeyError, ValueError) as e:
            if attempt < 2:
                time.sleep(2)
            else:
                raise DataFetchError(f"Failed to fetch {coin} candles after 3 attempts: {e}")
    return []


def _existing_max_ts(path: Path) -> int | None:
    """Return the latest timestamp in an existing CSV, or None."""
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
        writer = csv.DictWriter(f, fieldnames=CANDLE_FIELDS)
        writer.writeheader()
        writer.writerows(unique)

    return len(unique)


def fetch_coin_candles(
    client: httpx.Client,
    url: str,
    coin: str,
    candles_dir: Path,
    rate_limit_sec: float,
    force: bool = False,
) -> None:
    """Fetch all available hourly candles for one coin."""
    path = candles_dir / f"{coin}_1h.csv"
    now_ms = int(time.time() * 1000)

    if not force:
        existing = _existing_max_ts(path)
        if existing and now_ms - existing < 3_600_000:
            logger.info("%s: already up-to-date, skipping", coin)
            return

    start_ms = now_ms - 540 * 24 * 3600 * 1000
    chunk_ms = CHUNK_DAYS * 24 * 3600 * 1000

    all_rows: list[dict[str, float | int]] = []
    chunk_start = start_ms

    while chunk_start < now_ms:
        chunk_end = min(chunk_start + chunk_ms, now_ms)
        time.sleep(rate_limit_sec)
        rows = fetch_candles_chunk(client, url, coin, chunk_start, chunk_end)
        all_rows.extend(rows)

        if rows:
            first_dt = datetime.fromtimestamp(int(rows[0]["timestamp"]) / 1000, tz=timezone.utc)
            last_dt = datetime.fromtimestamp(int(rows[-1]["timestamp"]) / 1000, tz=timezone.utc)
            logger.info(
                "%s: %s -> %s (%d candles)",
                coin,
                first_dt.strftime("%Y-%m-%d"),
                last_dt.strftime("%Y-%m-%d"),
                len(rows),
            )

        chunk_start = chunk_end

    if not all_rows:
        logger.warning("%s: no data returned", coin)
        return

    n_unique = _save_csv(path, all_rows)
    logger.info("%s: %d unique candles saved", coin, n_unique)


def _binance_symbol(coin: str) -> str:
    return f"{coin}USDT"


def fetch_binance_coin_candles(
    client: httpx.Client,
    base_url: str,
    coin: str,
    candles_dir: Path,
    lookback_days: int,
    rate_limit_sec: float,
    force: bool = False,
) -> None:
    """Fetch hourly candles from Binance USD-M futures."""
    path = candles_dir / f"{coin}_1h.csv"
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    now_ms = int(now.timestamp() * 1000)

    if not force:
        existing = _existing_max_ts(path)
        if existing and now_ms - existing < 3_600_000:
            logger.info("%s: already up-to-date, skipping", coin)
            return

    start_dt = now - timedelta(days=lookback_days)
    cursor_ms = int(start_dt.timestamp() * 1000)
    symbol = _binance_symbol(coin)
    all_rows: list[dict[str, float | int]] = []

    while cursor_ms < now_ms:
        params: dict[str, str | int] = {
            "symbol": symbol,
            "interval": "1h",
            "startTime": cursor_ms,
            "limit": BINANCE_KLINES_LIMIT,
        }
        r = client.get(f"{base_url}/fapi/v1/klines", params=params, timeout=20.0)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or not data:
            break

        rows: list[dict[str, float | int]] = []
        for c in data:
            rows.append(
                {
                    "timestamp": int(c[0]),
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5]),
                }
            )
        all_rows.extend(rows)
        first_dt = datetime.fromtimestamp(int(rows[0]["timestamp"]) / 1000, tz=timezone.utc)
        last_dt = datetime.fromtimestamp(int(rows[-1]["timestamp"]) / 1000, tz=timezone.utc)
        logger.info(
            "%s (binance): %s -> %s (%d candles)",
            coin,
            first_dt.strftime("%Y-%m-%d"),
            last_dt.strftime("%Y-%m-%d"),
            len(rows),
        )
        if len(rows) < BINANCE_KLINES_LIMIT:
            break
        cursor_ms = int(rows[-1]["timestamp"]) + 3_600_000
        time.sleep(rate_limit_sec)

    if not all_rows:
        logger.warning("%s: no Binance candle data returned", coin)
        return

    n_unique = _save_csv(path, all_rows)
    logger.info("%s (binance): %d unique candles saved", coin, n_unique)


def fetch_all_candles(
    coins: list[str],
    infra: InfraConfig,
    force: bool = False,
) -> None:
    """Fetch candles for all coins."""
    infra.candles_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Fetching hourly candle data for %s", ", ".join(coins))

    with httpx.Client() as client:
        for coin in coins:
            if infra.market_data_provider == "binance_futures":
                fetch_binance_coin_candles(
                    client,
                    infra.binance_futures_url,
                    coin,
                    infra.candles_dir,
                    infra.lookback_days,
                    infra.rate_limit_sec,
                    force,
                )
            else:
                fetch_coin_candles(
                    client,
                    infra.rest_url,
                    coin,
                    infra.candles_dir,
                    infra.rate_limit_sec,
                    force,
                )

    logger.info("Data fetch complete")


def seed_price_buffer(
    buffer: "HourlyPriceBuffer",
    config: "StrategyConfig",
    infra: InfraConfig,
) -> None:
    """Seed a HourlyPriceBuffer with recent hourly candle closes from the API."""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (config.corr_window_hours + config.lookback_hours + 10) * 3_600_000

    with httpx.Client(timeout=15) as client:
        for coin in config.all_coins:
            try:
                r = client.post(
                    infra.rest_url,
                    json={
                        "type": "candleSnapshot",
                        "req": {
                            "coin": coin,
                            "interval": "1h",
                            "startTime": start_ms,
                            "endTime": now_ms,
                        },
                    },
                )
                candles = r.json()
                if isinstance(candles, list) and candles:
                    closes = [float(c["c"]) for c in candles]
                    last_open_ms = int(candles[-1]["t"])
                    last_epoch_hour = last_open_ms // 3_600_000
                    buffer.seed(coin, closes, last_epoch_hour)
                    logger.info("%s: %d hourly bars seeded", coin, len(closes))
            except (httpx.HTTPError, httpx.TimeoutException, KeyError, ValueError) as e:
                raise DataFetchError(f"Failed to seed {coin}: {e}")
            time.sleep(infra.rate_limit_sec)


def load_candles(candles_dir: Path, coins: list[str]) -> pd.DataFrame:
    """Load candle CSVs into a DataFrame with columns = coin close prices.

    Raises FileNotFoundError if any coin's CSV is missing.
    """
    frames: dict[str, "pd.Series[float]"] = {}

    for coin in coins:
        path = candles_dir / f"{coin}_1h.csv"
        if not path.exists():
            raise FileNotFoundError(f"Missing {path}. Run `hypemm fetch` first.")

        df = pd.read_csv(path)
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("datetime").sort_index()
        df = df[~df.index.duplicated(keep="first")]
        frames[coin] = df["close"]

    combined = pd.DataFrame(frames)
    combined = combined.ffill()
    combined = combined.dropna()
    return combined
