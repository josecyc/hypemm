#!/usr/bin/env python3
"""Step 0: Fetch hourly candles for all coins and save to CSV.

Paginates in 30-day chunks. Respects rate limits. Resumes from last
fetched timestamp if CSVs already exist.

Usage:
    python -m verification.fetch_data [--force]
"""
from __future__ import annotations

import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from rich.console import Console

from verification.config import (
    CANDLES_DIR,
    CHUNK_DAYS,
    COINS,
    RATE_LIMIT_SEC,
    REST_URL,
)

console = Console()

CANDLE_FIELDS = ["timestamp", "open", "high", "low", "close", "volume"]

# Try to go back 18 months — API will return what it has.
MAX_LOOKBACK_DAYS = 540


def fetch_candles_chunk(
    client: httpx.Client,
    coin: str,
    start_ms: int,
    end_ms: int,
) -> list[dict]:
    """Fetch one chunk of hourly candles."""
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": "1h",
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }
    for attempt in range(3):
        try:
            r = client.post(REST_URL, json=payload, timeout=15.0)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                return []
            rows = []
            for c in data:
                rows.append({
                    "timestamp": int(c["t"]),
                    "open": float(c["o"]),
                    "high": float(c["h"]),
                    "low": float(c["l"]),
                    "close": float(c["c"]),
                    "volume": float(c["v"]),
                })
            return rows
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                console.print(f"  [red]Failed after 3 attempts: {e}[/red]")
                return []


def existing_max_ts(path: Path) -> int | None:
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


def save_csv(path: Path, rows: list[dict]) -> None:
    """Write rows to CSV, deduplicating and sorting by timestamp."""
    seen = set()
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


def fetch_coin(client: httpx.Client, coin: str, force: bool) -> None:
    """Fetch all available hourly candles for one coin."""
    path = CANDLES_DIR / f"{coin}_1h.csv"
    now_ms = int(time.time() * 1000)

    if not force:
        existing = existing_max_ts(path)
        if existing and now_ms - existing < 3_600_000:
            console.print(f"  {coin}: already up-to-date, skipping (use --force to refetch)")
            return

    start_ms = now_ms - MAX_LOOKBACK_DAYS * 24 * 3600 * 1000
    chunk_ms = CHUNK_DAYS * 24 * 3600 * 1000

    all_rows: list[dict] = []
    chunk_start = start_ms

    while chunk_start < now_ms:
        chunk_end = min(chunk_start + chunk_ms, now_ms)
        time.sleep(RATE_LIMIT_SEC)
        rows = fetch_candles_chunk(client, coin, chunk_start, chunk_end)
        all_rows.extend(rows)

        if rows:
            first_dt = datetime.fromtimestamp(rows[0]["timestamp"] / 1000, tz=timezone.utc)
            last_dt = datetime.fromtimestamp(rows[-1]["timestamp"] / 1000, tz=timezone.utc)
            console.print(
                f"  {coin}: chunk {first_dt.strftime('%Y-%m-%d')}→"
                f"{last_dt.strftime('%Y-%m-%d')} ({len(rows)} candles)"
            )
        else:
            dt = datetime.fromtimestamp(chunk_start / 1000, tz=timezone.utc)
            console.print(f"  {coin}: chunk {dt.strftime('%Y-%m-%d')} — no data")

        chunk_start = chunk_end

    if not all_rows:
        console.print(f"  [yellow]{coin}: no data returned[/yellow]")
        return

    save_csv(path, all_rows)

    # Summary
    ts_sorted = sorted(set(r["timestamp"] for r in all_rows))
    first = datetime.fromtimestamp(ts_sorted[0] / 1000, tz=timezone.utc)
    last = datetime.fromtimestamp(ts_sorted[-1] / 1000, tz=timezone.utc)
    expected_hours = int((ts_sorted[-1] - ts_sorted[0]) / 3_600_000) + 1
    gaps = expected_hours - len(ts_sorted)

    console.print(
        f"  [green]{coin}: {len(ts_sorted)} candles, "
        f"{first.strftime('%Y-%m-%d')} → {last.strftime('%Y-%m-%d')}, "
        f"{gaps} gaps[/green]"
    )


def main() -> None:
    force = "--force" in sys.argv
    CANDLES_DIR.mkdir(parents=True, exist_ok=True)

    console.print("\n[bold cyan]═══ Fetching Hourly Candle Data ═══[/bold cyan]\n")

    with httpx.Client() as client:
        for coin in COINS:
            console.print(f"[cyan]{coin}:[/cyan]")
            fetch_coin(client, coin, force)
            console.print()

    console.print("[green]Data fetch complete.[/green]\n")


if __name__ == "__main__":
    main()
