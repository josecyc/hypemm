#!/usr/bin/env python3
"""Fetch all historical data needed to run the Jupyter notebooks.

Downloads hourly candles and funding rates from Binance Futures for all coins
used across the strategy configs and research notebooks.

Usage:
    uv run python scripts/fetch_data.py          # fetch everything
    uv run python scripts/fetch_data.py --quick   # only 2yr core pairs (faster)

After running, you can open the notebooks:
    cd verification && jupyter notebook
"""

from __future__ import annotations

import argparse
import csv
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

BINANCE_URL = "https://fapi.binance.com"

# Core coins (used by the strategy configs)
CORE_COINS = ["BTC", "ETH", "SOL", "DOGE", "LINK", "AVAX", "ADA"]

# Extended coins (used by the expanded universe notebook analysis)
EXTENDED_COINS = [
    "AAVE",
    "NEAR",
    "FIL",
    "ATOM",
    "UNI",
    "LTC",
    "DOT",
    "SUI",
    "ARB",
    "OP",
    "APT",
    "WLD",
    "ZEC",
    "INJ",
    "FET",
    "SEI",
    "XRP",
]

# Dataset definitions: each writes under data/market/binance_futures/<window>/.
DATASETS = {
    "2y": {
        "coins": ["BTC", "SOL", "AVAX", "LINK", "DOGE"],
        "lookback_days": 730,
        "description": "2-year core pairs (risk_analysis_reservoir.ipynb)",
    },
    "6y": {
        "coins": ["BTC", "SOL", "AVAX", "LINK", "DOGE"],
        "lookback_days": 2190,
        "description": "6-year core pairs (walkforward_analysis.ipynb)",
    },
    "expanded": {
        "coins": CORE_COINS + EXTENDED_COINS,
        "lookback_days": 2190,
        "description": "24-coin expanded universe (pair scanning)",
    },
}

MARKET_ROOT = Path("data/market/binance_futures")


def fetch_candles(coin: str, out_dir: Path, lookback_days: int, force: bool = False) -> bool:
    """Fetch hourly candles from Binance Futures."""
    out_path = out_dir / f"{coin}_1h.csv"

    if out_path.exists() and not force:
        size = out_path.stat().st_size
        if size > 100_000:
            print(f"    {coin} candles: exists ({size:,} bytes), skipping")
            return True

    symbol = f"{coin}USDT"
    now = datetime.now(timezone.utc)
    start_ms = int((now.timestamp() - lookback_days * 86400) * 1000)
    end_ms = int(now.timestamp() * 1000)

    all_candles: list[list] = []
    cursor = start_ms

    while cursor < end_ms:
        params = {
            "symbol": symbol,
            "interval": "1h",
            "startTime": cursor,
            "limit": 1500,
        }
        try:
            resp = httpx.get(f"{BINANCE_URL}/fapi/v1/klines", params=params, timeout=15)
            if resp.status_code == 400:
                print(f"    {coin} candles: not available on Binance Futures")
                return False
            resp.raise_for_status()
            candles = resp.json()
            if not candles:
                break
            all_candles.extend(candles)
            cursor = candles[-1][0] + 1
            time.sleep(0.15)
        except httpx.HTTPError as e:
            print(f"    {coin} candles: error {e}")
            return False

    if not all_candles:
        print(f"    {coin} candles: no data returned")
        return False

    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for c in all_candles:
            writer.writerow([c[0], c[1], c[2], c[3], c[4], c[5]])

    first = datetime.fromtimestamp(all_candles[0][0] / 1000, tz=timezone.utc)
    last = datetime.fromtimestamp(all_candles[-1][0] / 1000, tz=timezone.utc)
    print(f"    {coin} candles: {len(all_candles):,} bars " f"({first.date()} → {last.date()})")
    return True


def fetch_funding(coin: str, out_dir: Path, lookback_days: int, force: bool = False) -> bool:
    """Fetch funding rate history from Binance Futures."""
    out_path = out_dir / f"{coin}_1h.csv"

    if out_path.exists() and not force:
        size = out_path.stat().st_size
        if size > 10_000:
            print(f"    {coin} funding: exists ({size:,} bytes), skipping")
            return True

    symbol = f"{coin}USDT"
    now = datetime.now(timezone.utc)
    start_ms = int((now.timestamp() - lookback_days * 86400) * 1000)
    end_ms = int(now.timestamp() * 1000)

    all_rates: list[dict] = []
    cursor = start_ms

    while cursor < end_ms:
        params = {"symbol": symbol, "startTime": cursor, "limit": 1000}
        try:
            resp = httpx.get(f"{BINANCE_URL}/fapi/v1/fundingRate", params=params, timeout=15)
            if resp.status_code != 200:
                break
            rates = resp.json()
            if not rates:
                break
            all_rates.extend(rates)
            cursor = rates[-1]["fundingTime"] + 1
            time.sleep(0.15)
        except httpx.HTTPError as e:
            print(f"    {coin} funding: error {e}")
            return False

    if not all_rates:
        print(f"    {coin} funding: no data")
        return False

    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "funding_rate"])
        for r in all_rates:
            writer.writerow([r["fundingTime"], r["fundingRate"]])

    print(f"    {coin} funding: {len(all_rates):,} rates")
    return True


def fetch_dataset(name: str, force: bool = False) -> None:
    """Fetch a complete dataset (candles + funding) into data/market/."""
    ds = DATASETS[name]
    candle_dir = MARKET_ROOT / name / "candles"
    fund_dir = MARKET_ROOT / name / "funding"
    candle_dir.mkdir(parents=True, exist_ok=True)
    fund_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"Dataset: {name}")
    print(f"  {ds['description']}")
    print(f"  Coins: {ds['coins']}")
    print(f"  Lookback: {ds['lookback_days']} days")
    print(f"{'='*60}")

    print("\n  Candles:")
    for coin in ds["coins"]:
        fetch_candles(coin, candle_dir, ds["lookback_days"], force=force)

    print("\n  Funding:")
    for coin in ds["coins"]:
        fetch_funding(coin, fund_dir, ds["lookback_days"], force=force)


def run_backtests() -> None:
    """Run backtests to generate the report files notebooks depend on."""
    print(f"\n{'='*60}")
    print("Running backtests to generate report files...")
    print(f"{'='*60}")

    import subprocess

    configs = [
        ("configs/backtest/original_4pair_2y.toml", "original_4pair_2y"),
        ("configs/backtest/original_4pair_6y.toml", "original_4pair_6y"),
    ]

    for cfg_path, stem in configs:
        run_dir = Path("data/runs/backtest") / stem
        summary = run_dir / "backtest_summary.json"
        if summary.exists():
            print(f"\n  {stem}: reports exist, skipping")
            continue

        print(f"\n  {stem}: running backtest...")
        result = subprocess.run(
            ["uv", "run", "hypemm", "backtest", "--config", cfg_path],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"  {stem}: done")
        else:
            print(f"  {stem}: failed")
            print(f"    {result.stderr[:200]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch historical data for notebooks")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Only fetch 2yr core data (faster, enough for risk_analysis)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch even if data already exists",
    )
    parser.add_argument(
        "--no-backtest",
        action="store_true",
        help="Skip running backtests after fetching",
    )
    args = parser.parse_args()

    if args.quick:
        datasets = ["2y"]
    else:
        datasets = ["2y", "6y", "expanded"]

    for ds in datasets:
        fetch_dataset(ds, force=args.force)

    if not args.no_backtest:
        run_backtests()

    print(f"\n{'='*60}")
    print("Done! You can now run the notebooks:")
    print("  uv run jupyter lab notebooks")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
