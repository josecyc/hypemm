"""Fetch hourly funding rates from the Hyperliquid API and compute per-trade funding cost."""

from __future__ import annotations

import csv
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pandas as pd

from hypemm.accounting import funding_for_leg_hour, signed_leg_sizes
from hypemm.config import InfraConfig
from hypemm.models import DataFetchError, Direction, OpenPosition

logger = logging.getLogger(__name__)

FUNDING_FIELDS = ["timestamp", "funding_rate", "premium"]
PAGE_SIZE = 500  # Hyperliquid's hard cap per fundingHistory response
BINANCE_FUNDING_LIMIT = 1000


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
        start_ms = now_ms - 540 * 24 * 3600 * 1000
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


def _binance_symbol(coin: str) -> str:
    return f"{coin}USDT"


def fetch_binance_coin_funding(
    client: httpx.Client,
    base_url: str,
    coin: str,
    funding_dir: Path,
    lookback_days: int,
    rate_limit_sec: float,
    force: bool = False,
) -> None:
    """Fetch Binance funding history and expand to hourly rows with zeros between events."""
    path = funding_dir / f"{coin}_1h.csv"
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end_dt = now - timedelta(hours=1)
    start_dt = now - timedelta(days=lookback_days)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    if not force:
        existing = _existing_max_ts(path)
        if existing and end_ms - existing < 3_600_000:
            logger.info("%s funding: already up-to-date, skipping", coin)
            return

    symbol = _binance_symbol(coin)
    cursor_ms = start_ms
    event_rates: dict[int, float] = {}

    while cursor_ms <= end_ms:
        params: dict[str, str | int] = {
            "symbol": symbol,
            "startTime": cursor_ms,
            "endTime": end_ms,
            "limit": BINANCE_FUNDING_LIMIT,
        }
        r = client.get(f"{base_url}/fapi/v1/fundingRate", params=params, timeout=20.0)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or not data:
            break

        for row in data:
            event_rates[int(row["fundingTime"])] = float(row["fundingRate"])

        last_ts = int(data[-1]["fundingTime"])
        logger.info(
            "%s funding (binance): %s -> %s (%d records)",
            coin,
            datetime.fromtimestamp(int(data[0]["fundingTime"]) / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M"
            ),
            datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            len(data),
        )
        if len(data) < BINANCE_FUNDING_LIMIT:
            break
        cursor_ms = last_ts + 1
        time.sleep(rate_limit_sec)

    rows: list[dict[str, float | int]] = []
    hour = start_dt
    while hour <= end_dt:
        ts = int(hour.timestamp() * 1000)
        rows.append(
            {
                "timestamp": ts,
                "funding_rate": event_rates.get(ts, 0.0),
                "premium": 0.0,
            }
        )
        hour += timedelta(hours=1)

    n_unique = _save_csv(path, rows)
    logger.info("%s funding (binance): %d hourly rows saved", coin, n_unique)


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
            if infra.market_data_provider == "binance_futures":
                fetch_binance_coin_funding(
                    client,
                    infra.binance_futures_url,
                    coin,
                    infra.funding_dir,
                    infra.lookback_days,
                    infra.rate_limit_sec,
                    force,
                )
            else:
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
    size_a: float,
    size_b: float,
    entry_ts_ms: int,
    exit_ts_ms: int,
    funding_a: "pd.Series[float]",
    funding_b: "pd.Series[float]",
    prices_a: "pd.Series[float]",
    prices_b: "pd.Series[float]",
) -> float:
    """Total funding paid (positive = cost to us) over [entry_ts, exit_ts).

    Per-leg model (see hypemm.accounting.funding_for_leg_hour): each leg
    accrues `signed_size × mark × hourly_rate` per hour. By linearity the
    sum across legs of one coin equals HL's per-coin charge, even with
    cross-pair coin overlap.

    `prices_a`/`prices_b` carry the hourly mark prices indexed identically
    to `funding_a`/`funding_b`.
    """
    if exit_ts_ms <= entry_ts_ms:
        return 0.0

    entry_ts = pd.Timestamp(entry_ts_ms, unit="ms", tz="UTC")
    exit_ts = pd.Timestamp(exit_ts_ms, unit="ms", tz="UTC")

    mask_a = (funding_a.index >= entry_ts) & (funding_a.index < exit_ts)
    mask_b = (funding_b.index >= entry_ts) & (funding_b.index < exit_ts)
    rates_a = funding_a[mask_a]
    rates_b = funding_b[mask_b]
    marks_a = prices_a.reindex(rates_a.index)
    marks_b = prices_b.reindex(rates_b.index)

    expected_hours = (exit_ts_ms - entry_ts_ms) // 3_600_000
    if len(rates_a) != expected_hours or len(rates_b) != expected_hours:
        raise ValueError(
            f"Funding data gap: expected {expected_hours} hourly records between "
            f"{entry_ts} and {exit_ts}, got {len(rates_a)} for A and {len(rates_b)} for B"
        )
    if marks_a.isna().any() or marks_b.isna().any():
        raise ValueError(
            f"Mark-price gap: missing prices at funding hours between {entry_ts} " f"and {exit_ts}"
        )

    signed_a, signed_b = signed_leg_sizes(direction, size_a, size_b)
    leg_a = float((marks_a * rates_a).sum()) * signed_a
    leg_b = float((marks_b * rates_b).sum()) * signed_b
    return leg_a + leg_b


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
    marks: dict[str, float],
) -> None:
    """Accrue one hour of MODELED funding to each open position.

    Per-leg formula: signed_size × mark × hourly_rate. Summed into
    `pos.funding_paid`. By linearity, summing across all open legs holding
    a coin equals the HL-billed funding for that coin in the same hour.

    Skips a position if either coin's rate or mark is missing — the runner
    will retry on the next hour boundary; better to underaccrue one hour
    than to feed in stale or fabricated data.
    """
    for pos in positions.values():
        if pos is None:
            continue
        rate_a = rates.get(pos.pair.coin_a)
        rate_b = rates.get(pos.pair.coin_b)
        mark_a = marks.get(pos.pair.coin_a)
        mark_b = marks.get(pos.pair.coin_b)
        if rate_a is None or rate_b is None or mark_a is None or mark_b is None:
            logger.warning(
                "Skipping funding accrual for %s: missing rate or mark for %s/%s",
                pos.pair.label,
                pos.pair.coin_a,
                pos.pair.coin_b,
            )
            continue
        signed_a, signed_b = signed_leg_sizes(pos.direction, pos.filled_size_a, pos.filled_size_b)
        pos.funding_paid += funding_for_leg_hour(signed_a, mark_a, rate_a)
        pos.funding_paid += funding_for_leg_hour(signed_b, mark_b, rate_b)


def accrue_actual_funding(
    positions: dict[str, OpenPosition | None],
    events: list[dict[str, object]],
) -> None:
    """Distribute HL `userFunding` events to open-position legs by signed size.

    Each event has shape `{"time": ms, "delta": {"type": "funding",
    "coin": str, "usdc": str, ...}}`. We sum per-coin deltas and then
    apportion to all legs holding that coin in proportion to their signed
    size — which equals each leg's own funding charge by linearity. If no
    leg currently holds the coin (event arrived after the position closed)
    the delta is dropped silently; reconcile catches such cases.
    """
    by_coin: dict[str, float] = {}
    for ev in events:
        delta = ev.get("delta")
        if not isinstance(delta, dict):
            continue
        if delta.get("type") != "funding":
            continue
        coin = delta.get("coin")
        usdc = delta.get("usdc")
        if not isinstance(coin, str) or usdc is None:
            continue
        by_coin[coin] = by_coin.get(coin, 0.0) + float(usdc)

    for coin, total in by_coin.items():
        legs: list[tuple[OpenPosition, float]] = []
        for pos in positions.values():
            if pos is None:
                continue
            if pos.pair.coin_a == coin:
                signed_a, _ = signed_leg_sizes(pos.direction, pos.filled_size_a, pos.filled_size_b)
                legs.append((pos, signed_a))
            if pos.pair.coin_b == coin:
                _, signed_b = signed_leg_sizes(pos.direction, pos.filled_size_a, pos.filled_size_b)
                legs.append((pos, signed_b))

        if not legs:
            logger.debug("Skipping userFunding for %s: no open leg holds it", coin)
            continue
        # Linearity gives leg_share = total × (signed / net_signed). This
        # equals each leg's own modeled funding (signed × mark × rate) when
        # everything is internally consistent. When net_signed = 0 (cross-pair
        # netting), HL billed 0 and we don't attribute anything — each pair's
        # modeled funding will diverge from actual by exactly its own
        # contribution, which reconcile surfaces as drift.
        net_signed = sum(s for _, s in legs)
        if abs(net_signed) < 1e-12:
            continue
        for pos, signed in legs:
            pos.funding_paid_actual += total * (signed / net_signed)


def fetch_user_funding(
    client: httpx.Client,
    rest_url: str,
    account: str,
    start_ms: int,
    end_ms: int | None = None,
) -> list[dict[str, object]]:
    """Fetch HL userFunding ledger entries for the account since start_ms."""
    payload: dict[str, object] = {
        "type": "userFunding",
        "user": account,
        "startTime": start_ms,
    }
    if end_ms is not None:
        payload["endTime"] = end_ms
    r = client.post(rest_url, json=payload, timeout=15.0)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return []
    return [dict(row) for row in data]
