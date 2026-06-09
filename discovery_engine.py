"""
discovery_engine.py — First-touch SMA Discovery Engine

Scans the existing candle cache for tickers where price has just touched
a significant SMA for the first time in N bars. This surfaces early-stage
setups BEFORE they accumulate enough hits to rank in the main engine.

The canonical example: LAC touching the MA420 at $3.96 — a structural
low that the main engine only surfaced weeks later after many confirmations.

Usage:
    docker exec e47_engine python /app/discovery_engine.py
    docker exec e47_engine python /app/discovery_engine.py --absence 100
    docker exec e47_engine python /app/discovery_engine.py --absence 50 --min-period 200
    docker exec e47_engine python /app/discovery_engine.py --timeframes 1d,1w

Does NOT modify any existing engine files or output.
Results saved to: output/discovery/discovery_YYYY-MM-DD_HH-MM-SS.csv
"""

from __future__ import annotations

import os
import csv
import gzip
import pickle
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from engine import OUTFITS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CACHE_DIR   = "/cache/candle_cache"
OUTPUT_DIR  = Path("./output/discovery")

# Webull timeframe label → minutes (for display/filtering)
TF_MINUTES: dict[str, int] = {
    "1s": 0, "5s": 0, "15s": 0, "30s": 0,
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240,
    "1d": 1440, "1w": 10080, "1mo": 43200,
}


# ─── Candle cache loader ──────────────────────────────────────────────────────

def load_candle_cache(cache_dir: str = CACHE_DIR) -> dict:
    """Load candle cache. Format: {ticker}.pkl.gz → {tf: DataFrame}."""
    cache = {}
    cache_path = Path(cache_dir)
    if not cache_path.exists():
        logging.warning(f"Cache dir not found: {cache_dir}")
        return cache
    for f_path in cache_path.glob("*.pkl.gz"):
        ticker = f_path.stem.replace(".pkl", "")
        try:
            with gzip.open(f_path, "rb") as f:
                tf_data: dict = pickle.load(f)
            for tf, df in tf_data.items():
                if isinstance(df, pd.DataFrame) and len(df) > 0:
                    cache[(ticker, tf)] = df
        except Exception as e:
            logging.debug(f"Could not load {ticker}: {e}")
    logging.info(f"Loaded {len(cache)} ticker/timeframe combos from cache")
    return cache


# ─── SMA calculation ─────────────────────────────────────────────────────────

def compute_sma(series: pd.Series, period: int) -> pd.Series:
    """Compute simple moving average."""
    return series.rolling(window=period, min_periods=period).mean()


# ─── First-touch detection ───────────────────────────────────────────────────

def find_first_touches(
    df: pd.DataFrame,
    periods: list[int],
    absence_bars: int = 100,
    tolerance_pct: float = 0.002,  # 0.2% tolerance for "touch"
) -> list[dict]:
    """
    For each SMA period, check if the most recent candle touches the SMA
    after at least `absence_bars` bars of no contact.

    A "touch" means price (high-low range) crosses or reaches within
    tolerance_pct of the SMA value.

    Returns list of hit dicts for this ticker/timeframe.
    """
    if len(df) < 2:
        return []

    close  = df["close"]
    high   = df["high"]
    low    = df["low"]

    hits = []

    for period in periods:
        if len(df) < period + absence_bars:
            continue  # not enough history

        sma = compute_sma(close, period)
        if sma.isna().all():
            continue

        # Check the most recent candle
        last_idx = len(df) - 1
        last_sma = sma.iloc[last_idx]
        last_high = high.iloc[last_idx]
        last_low  = low.iloc[last_idx]
        last_close = close.iloc[last_idx]

        if pd.isna(last_sma) or last_sma == 0:
            continue

        # Is the latest candle touching the SMA?
        tolerance = last_sma * tolerance_pct
        touched_now = (last_low - tolerance) <= last_sma <= (last_high + tolerance)
        if not touched_now:
            continue

        # Check absence: was this SMA NOT touched in the prior `absence_bars` candles?
        window_start = max(0, last_idx - absence_bars)
        window_end   = last_idx  # exclusive of current bar

        prior_highs = high.iloc[window_start:window_end]
        prior_lows  = low.iloc[window_start:window_end]
        prior_smas  = sma.iloc[window_start:window_end]

        # Count prior touches in the absence window
        prior_touches = 0
        for i in range(len(prior_smas)):
            s = prior_smas.iloc[i]
            h = prior_highs.iloc[i]
            l = prior_lows.iloc[i]
            if pd.isna(s) or s == 0:
                continue
            tol = s * tolerance_pct
            if (l - tol) <= s <= (h + tol):
                prior_touches += 1

        if prior_touches > 0:
            continue  # not a first touch — was touched recently

        # Determine direction: price approaching from above or below
        # Look at where close was N bars ago vs SMA
        ref_idx = max(0, last_idx - 5)
        ref_close = close.iloc[ref_idx]
        ref_sma   = sma.iloc[ref_idx] if not pd.isna(sma.iloc[ref_idx]) else last_sma

        if ref_close > ref_sma:
            direction = "from_above"  # price fell to SMA = potential support
        else:
            direction = "from_below"  # price rose to SMA = potential resistance

        hits.append({
            "sma_period": period,
            "sma_value":  round(float(last_sma), 4),
            "close":      round(float(last_close), 4),
            "high":       round(float(last_high), 4),
            "low":        round(float(last_low), 4),
            "direction":  direction,
            "prior_touches_in_window": prior_touches,
        })

    return hits


# ─── Main scan ───────────────────────────────────────────────────────────────

def run(
    absence_bars: int = 100,
    min_period: int = 0,
    timeframes: list[str] | None = None,
    tolerance_pct: float = 0.002,
):
    candle_cache = load_candle_cache()
    if not candle_cache:
        logging.error("Candle cache empty — run the main engine first.")
        return

    # Collect all SMA periods from all outfits
    all_periods_set: set[int] = set()
    for outfit in OUTFITS:
        for p in outfit["periods"]:
            if p >= min_period:
                all_periods_set.add(p)
    all_periods = sorted(all_periods_set)
    logging.info(f"Scanning {len(all_periods)} unique SMA periods: {all_periods}")

    results = []
    total   = len(candle_cache)

    for i, ((ticker, tf), df) in enumerate(candle_cache.items()):
        if timeframes and tf not in timeframes:
            continue
        if i % 500 == 0:
            logging.info(f"Scanning {i}/{total}...")

        hits = find_first_touches(
            df,
            periods=all_periods,
            absence_bars=absence_bars,
            tolerance_pct=tolerance_pct,
        )

        for hit in hits:
            results.append({
                "ticker":      ticker,
                "timeframe":   tf,
                "sma_period":  hit["sma_period"],
                "sma_value":   hit["sma_value"],
                "close":       hit["close"],
                "high":        hit["high"],
                "low":         hit["low"],
                "direction":   hit["direction"],
                "absence_bars": absence_bars,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            })

    if not results:
        logging.warning("No first-touch signals found.")
        print("\n  No discovery signals found this cycle.\n")
        return

    # Sort: longer SMA period first (structural), then by timeframe weight
    tf_weight = {tf: i for i, tf in enumerate(
        ["1mo", "1w", "1d", "4h", "2h", "1h", "30m", "15m", "5m", "1m"]
    )}
    results.sort(key=lambda r: (
        -r["sma_period"],
        tf_weight.get(r["timeframe"], 99),
    ))

    # Print summary
    print("\n" + "═" * 85)
    print(f"  DISCOVERY ENGINE  |  absence={absence_bars} bars  |  {len(results)} signals found")
    print("═" * 85)
    print(f"  {'Ticker':<8} {'TF':<6} {'SMA':>6} {'SMA Val':>9} {'Close':>9} {'Direction':<14}")
    print("─" * 85)
    for r in results[:50]:
        direction_label = "↓ support tap" if r["direction"] == "from_above" else "↑ resistance tap"
        print(
            f"  {r['ticker']:<8} {r['timeframe']:<6} {r['sma_period']:>6} "
            f"{r['sma_value']:>9.3f} {r['close']:>9.3f}  {direction_label}"
        )
    if len(results) > 50:
        print(f"  ... and {len(results) - 50} more")
    print("═" * 85)

    # Save CSV
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    out_path = OUTPUT_DIR / f"discovery_{ts}.csv"
    fieldnames = [
        "ticker", "timeframe", "sma_period", "sma_value", "close",
        "high", "low", "direction", "absence_bars", "timestamp_utc"
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    logging.info(f"Discovery results saved to {out_path}")
    print(f"\n  Saved: {out_path}\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="First-touch SMA Discovery Engine")
    parser.add_argument(
        "--absence", type=int, default=100,
        help="Minimum bars of no SMA contact before flagging (default: 100)"
    )
    parser.add_argument(
        "--min-period", type=int, default=0,
        help="Only scan SMAs >= this period (e.g. 200 for long-term only)"
    )
    parser.add_argument(
        "--timeframes", type=str, default=None,
        help="Comma-separated timeframes to scan (e.g. 1d,1w). Default: all."
    )
    parser.add_argument(
        "--tolerance", type=float, default=0.002,
        help="Touch tolerance as fraction of SMA value (default: 0.002 = 0.2%%)"
    )
    args = parser.parse_args()

    tf_list = [t.strip() for t in args.timeframes.split(",")] if args.timeframes else None

    run(
        absence_bars=args.absence,
        min_period=args.min_period,
        timeframes=tf_list,
        tolerance_pct=args.tolerance,
    )
