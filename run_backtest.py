"""
run_backtest.py — run walk-forward backtest on current top-ranked signals.

Reads signals_current.xlsx for the current top tickers/timeframes/outfits,
loads candle data from the disk cache, runs walk-forward backtest on each,
and writes results to output/backtest_results.csv + prints a summary.

Usage:
    docker exec e47_engine python run_backtest.py
    docker exec e47_engine python run_backtest.py --method cpcv
    docker exec e47_engine python run_backtest.py --horizon 20 --top 50
"""

from __future__ import annotations

import os
import csv
import pickle
import gzip
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
import openpyxl

from backtest import backtest_top_signals, BacktestResult
from engine import OUTFITS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CACHE_DIR = "/cache/candle_cache"
OUTPUT_DIR = Path("./output")
XLSX_PATH = OUTPUT_DIR / "signals_current.xlsx"
RESULTS_PATH = OUTPUT_DIR / "backtest_results.csv"


def load_candle_cache(cache_dir: str = CACHE_DIR) -> dict:
    """Load candle cache from disk. Format: one {ticker}.pkl.gz per ticker,
    containing a dict of {timeframe: DataFrame}."""
    cache = {}
    cache_path = Path(cache_dir)
    if not cache_path.exists():
        logging.warning(f"Cache dir not found: {cache_dir}")
        return cache
    files = list(cache_path.glob("*.pkl.gz"))
    for f_path in files:
        ticker = f_path.name.replace(".pkl.gz", "")
        try:
            with gzip.open(f_path, "rb") as f:
                tf_data: dict = pickle.load(f)
            for tf, df in tf_data.items():
                cache[(ticker, tf)] = df
        except Exception as e:
            logging.debug(f"Could not load {ticker}: {e}")
    logging.info(f"Loaded {len(cache)} ticker/timeframe combos from cache")
    return cache


def read_top_signals(xlsx_path: Path, top_n: int = 100) -> list[dict]:
    """Read unique ticker/timeframe/outfit combos from signals_current.xlsx."""
    wb = openpyxl.load_workbook(xlsx_path)
    ws = wb.active

    entries = []
    seen = set()
    in_table = False

    for row in ws.iter_rows(values_only=True):
        if row[0] == "Rank":
            in_table = True
            continue
        if not in_table:
            continue
        if not isinstance(row[0], int):
            break

        rank, ticker, tf, outfit_str = row[0], row[1], row[2], row[3]
        if not ticker or not tf or not outfit_str:
            continue

        # Deduplicate by ticker+timeframe+outfit
        key = (ticker, tf, outfit_str)
        if key in seen:
            continue
        seen.add(key)

        # Match outfit string to outfit dict
        outfit = None
        for o in OUTFITS:
            periods_str = "/".join(str(p) for p in o["periods"])
            if periods_str == outfit_str:
                outfit = o
                break

        if outfit is None:
            continue

        entries.append({
            "rank": rank,
            "ticker": ticker,
            "timeframe": tf,
            "outfit": outfit,
            "outfit_str": outfit_str,
        })

        if len(entries) >= top_n:
            break

    logging.info(f"Read {len(entries)} unique signals from {xlsx_path.name}")
    return entries


class _FakeEntry:
    """Minimal stand-in for HashMapEntry so backtest_top_signals works."""
    def __init__(self, ticker, timeframe, outfit_id):
        self.ticker = ticker
        self.timeframe = timeframe
        self.outfit_id = outfit_id


def run(method: str = "walk_forward", horizon_bars: int = 10, top_n: int = 100):
    if not XLSX_PATH.exists():
        logging.error(f"signals_current.xlsx not found at {XLSX_PATH}")
        return

    signals = read_top_signals(XLSX_PATH, top_n=top_n)
    if not signals:
        logging.error("No signals found in output.")
        return

    candle_cache = load_candle_cache()
    if not candle_cache:
        logging.error("Candle cache is empty — run the engine first.")
        return

    outfits_by_id = {o["id"]: o for o in OUTFITS}

    # Build fake entries + subset candle cache for speed
    fake_entries = []
    subset_cache = {}
    for sig in signals:
        ticker = sig["ticker"]
        tf = sig["timeframe"]
        outfit = sig["outfit"]
        key = (ticker, tf)
        if key not in candle_cache:
            logging.debug(f"No candles for {ticker}/{tf} — skipping")
            continue
        fake_entries.append(_FakeEntry(ticker, tf, outfit["id"]))
        subset_cache[key] = candle_cache[key]

    logging.info(f"Running {method} backtest on {len(fake_entries)} signals (horizon={horizon_bars} bars)...")

    results = backtest_top_signals(
        candle_cache=subset_cache,
        top_entries=fake_entries,
        outfits_by_id=outfits_by_id,
        method=method,
        horizon_bars=horizon_bars,
        train_size=50,
        test_size=30,
    )

    if not results:
        logging.warning("No backtest results — check candle cache coverage.")
        return

    # Sort by Sharpe descending
    sorted_results = sorted(results.items(), key=lambda x: x[1].sharpe, reverse=True)

    # Print summary
    # Build outfit lookup for display
    outfit_str_by_id = {o["id"]: "/".join(str(p) for p in o["periods"]) for o in OUTFITS}

    print("\n" + "═" * 90)
    print(f"  BACKTEST SUMMARY  |  method={method}  horizon={horizon_bars} bars")
    print("═" * 90)
    print(f"  {'Ticker':<8} {'TF':<6} {'Outfit':<32} {'Trades':>6} {'WinRate':>8} {'AvgRet':>8} {'Sharpe':>8}")
    print("─" * 90)
    for key, res in sorted_results[:30]:
        if res.n_trades == 0:
            continue
        parts = key.split("|")
        ticker = parts[0] if len(parts) > 0 else ""
        tf = parts[1] if len(parts) > 1 else ""
        outfit_id = int(parts[2]) if len(parts) > 2 else 0
        outfit_str = outfit_str_by_id.get(outfit_id, str(outfit_id))
        print(f"  {ticker:<8} {tf:<6} {outfit_str:<32} {res.n_trades:>6} {res.win_rate:>8.1%} {res.avg_return:>+8.3%} {res.sharpe:>8.2f}")
    print("═" * 90)

    # Write CSV
    OUTPUT_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    results_path = OUTPUT_DIR / f"backtest_{ts}.csv"
    with open(results_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["key", "ticker", "timeframe", "outfit_id",
                         "n_trades", "win_rate", "avg_return", "total_return",
                         "sharpe", "max_drawdown", "method", "horizon_bars"])
        for key, res in sorted_results:
            parts = key.split("|")
            writer.writerow([
                key,
                parts[0] if len(parts) > 0 else "",
                parts[1] if len(parts) > 1 else "",
                parts[2] if len(parts) > 2 else "",
                res.n_trades,
                round(res.win_rate, 4),
                round(res.avg_return, 6),
                round(res.total_return, 6),
                round(res.sharpe, 4),
                round(res.max_drawdown, 4),
                method,
                horizon_bars,
            ])

    logging.info(f"Results saved to {results_path}")
    print(f"\n  Saved to: {results_path}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest current top signals")
    parser.add_argument("--method", choices=["walk_forward", "cpcv"], default="walk_forward")
    parser.add_argument("--horizon", type=int, default=10, help="Forward bars to hold")
    parser.add_argument("--top", type=int, default=100, help="How many top signals to test")
    args = parser.parse_args()
    run(method=args.method, horizon_bars=args.horizon, top_n=args.top)
