"""
trade_engine.py — Paper Trade Suggestion Engine

Reads all existing output sources and generates ranked, actionable trade
suggestions with entry, stop, target, and R/R ratio.

Scoring logic:
  - Confluence score (1-3) from snapshot + discovery + backtest
  - Backtest Sharpe and win rate
  - Discovery SMA period (longer = more structural)
  - Discovery direction (from_above = support, from_below = breakout)
  - Convergence from main ranker
  - ATR-based stop from candle cache

Confidence:
  HIGH   — confluence 3/3 + Sharpe > 5
  MEDIUM — confluence 2/3 OR Sharpe 2-5
  LOW    — confluence 1/3, no backtest

Does NOT modify any existing files. Read-only.

Usage:
    docker exec e47_engine python /app/trade_engine.py
    docker exec e47_engine python /app/trade_engine.py --min-confidence MEDIUM
    docker exec e47_engine python /app/trade_engine.py --discovery-tf 1d,1w,1mo --min-rr 2.0
"""

from __future__ import annotations

import csv
import gzip
import pickle
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

OUTPUT_DIR    = Path("./output")
SNAPSHOT_DIR  = OUTPUT_DIR / "snapshots"
DISCOVERY_DIR = OUTPUT_DIR / "discovery"
CACHE_DIR     = Path("/cache/candle_cache")
TRADES_DIR    = OUTPUT_DIR / "trades"

CONFIDENCE_LEVELS = ["HIGH", "MEDIUM", "LOW"]


# ─── Loaders ─────────────────────────────────────────────────────────────────

def load_latest_snapshot() -> dict[str, dict]:
    files = sorted(SNAPSHOT_DIR.glob("snapshot_*.csv"))
    if not files:
        return {}
    latest = files[-1]
    logging.info(f"Snapshot: {latest.name}")
    results = {}
    with open(latest, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ticker = row["ticker"]
            if ticker not in results:
                results[ticker] = row
    return results


def load_latest_discovery(timeframes: list[str] | None = None) -> dict[str, list[dict]]:
    files = sorted(DISCOVERY_DIR.glob("discovery_*.csv"))
    if not files:
        return {}
    latest = files[-1]
    logging.info(f"Discovery: {latest.name}")
    results: dict[str, list[dict]] = {}
    with open(latest, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if timeframes and row["timeframe"] not in timeframes:
                continue
            ticker = row["ticker"]
            results.setdefault(ticker, []).append(row)
    return results


def load_latest_backtest(min_sharpe: float = 2.0) -> dict[str, list[dict]]:
    files = sorted(OUTPUT_DIR.glob("backtest_*.csv"))
    if not files:
        return {}
    latest = files[-1]
    logging.info(f"Backtest: {latest.name}")
    results: dict[str, list[dict]] = {}
    with open(latest, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                sharpe = float(row["sharpe"])
            except (ValueError, KeyError):
                continue
            if sharpe < min_sharpe:
                continue
            ticker = row["ticker"]
            results.setdefault(ticker, []).append(row)
    return results


def load_candle_cache() -> dict:
    cache = {}
    if not CACHE_DIR.exists():
        return cache
    for f_path in CACHE_DIR.glob("*.pkl.gz"):
        ticker = f_path.stem.replace(".pkl", "")
        try:
            with gzip.open(f_path, "rb") as f:
                tf_data = pickle.load(f)
            for tf, df in tf_data.items():
                if isinstance(df, pd.DataFrame) and len(df) > 0:
                    cache[(ticker, tf)] = df
        except Exception:
            pass
    logging.info(f"Loaded {len(cache)} candle cache entries")
    return cache


# ─── ATR calculation ─────────────────────────────────────────────────────────

def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Compute Average True Range for stop sizing."""
    try:
        high = df["high"].astype(float)
        low  = df["low"].astype(float)
        close = df["close"].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(period).mean().iloc[-1]
        return float(atr) if not np.isnan(atr) else 0.0
    except Exception:
        return 0.0


def get_sma_levels(df: pd.DataFrame, periods: list[int]) -> dict[int, float]:
    """Get current SMA values for a list of periods."""
    close = df["close"].astype(float)
    levels = {}
    for p in periods:
        if len(df) >= p:
            val = close.rolling(p).mean().iloc[-1]
            if not np.isnan(val):
                levels[p] = round(float(val), 4)
    return levels


# ─── Trade construction ───────────────────────────────────────────────────────

def build_trade(
    ticker: str,
    snapshot: dict[str, dict],
    discovery: dict[str, list[dict]],
    backtest: dict[str, list[dict]],
    candle_cache: dict,
) -> dict | None:

    in_snapshot  = ticker in snapshot
    in_discovery = ticker in discovery
    in_backtest  = ticker in backtest

    confluence = sum([in_snapshot, in_discovery, in_backtest])
    if confluence == 0:
        return None

    # ── Best backtest ──
    best_sharpe   = None
    best_win_rate = None
    if in_backtest:
        best_bt = max(backtest[ticker], key=lambda r: float(r["sharpe"]))
        best_sharpe   = round(float(best_bt["sharpe"]), 2)
        best_win_rate = round(float(best_bt["win_rate"]), 2)

    # ── Best discovery signal ──
    disc_direction = None
    disc_sma       = None
    disc_tf        = None
    disc_close     = None
    if in_discovery:
        # Prefer daily/weekly/monthly, longest SMA
        preferred = [r for r in discovery[ticker] if r["timeframe"] in ("1d","1w","1mo")]
        pool = preferred if preferred else discovery[ticker]
        best_disc  = max(pool, key=lambda r: int(r["sma_period"]))
        disc_direction = best_disc["direction"]
        disc_sma       = int(best_disc["sma_period"])
        disc_tf        = best_disc["timeframe"]
        disc_close     = float(best_disc["close"])

    # ── Snapshot data ──
    snap_tf         = snapshot[ticker]["timeframe"] if in_snapshot else None
    snap_convergence = snapshot[ticker].get("convergence", "") if in_snapshot else ""
    snap_hits       = int(snapshot[ticker]["hits"]) if in_snapshot else 0
    snap_outfit     = snapshot[ticker].get("outfit", "") if in_snapshot else ""

    # ── Price from candle cache ──
    # Use discovery close if available, else try cache
    close_price = disc_close

    tf_lookup = disc_tf or snap_tf or "1d"
    df = candle_cache.get((ticker, tf_lookup))
    if df is None:
        df = candle_cache.get((ticker, "1d"))
    if df is None:
        # Try any timeframe
        for key in candle_cache:
            if key[0] == ticker:
                df = candle_cache[key]
                break

    if df is None or len(df) < 10:
        if close_price is None:
            return None
        atr = close_price * 0.02  # fallback 2% ATR
        sma_levels = {}
    else:
        if close_price is None:
            close_price = float(df["close"].iloc[-1])
        atr = compute_atr(df)
        if atr == 0:
            atr = close_price * 0.02

        # Get SMA levels for stop/target
        all_periods = list(range(50, 1000, 50))
        sma_levels = get_sma_levels(df, all_periods)

    # ── Entry / Stop / Target ──
    entry = round(close_price, 4)

    # Stop: 1.5x ATR below entry (support tap) or above entry (breakout)
    if disc_direction == "from_above":
        # Support tap — stop below
        stop = round(entry - (1.5 * atr), 4)
        side = "BUY"
        # Target: next SMA above entry
        smas_above = {p: v for p, v in sma_levels.items() if v > entry * 1.01}
        target = round(min(smas_above.values()), 4) if smas_above else round(entry * 1.10, 4)
    elif disc_direction == "from_below":
        # Breakout — stop below breakout level
        stop = round(entry - (2.0 * atr), 4)
        side = "BUY"
        # Target: next SMA above
        smas_above = {p: v for p, v in sma_levels.items() if v > entry * 1.005}
        target = round(min(smas_above.values()), 4) if smas_above else round(entry * 1.15, 4)
    else:
        # Snapshot only, no direction — default support play
        stop = round(entry - (1.5 * atr), 4)
        side = "BUY"
        smas_above = {p: v for p, v in sma_levels.items() if v > entry * 1.01}
        target = round(min(smas_above.values()), 4) if smas_above else round(entry * 1.10, 4)

    # ── R/R ──
    risk   = entry - stop
    reward = target - entry
    rr = round(reward / risk, 2) if risk > 0 else 0.0

    # ── Confidence ──
    if confluence == 3 and (best_sharpe or 0) >= 5:
        confidence = "HIGH"
    elif confluence == 3 or (confluence == 2 and (best_sharpe or 0) >= 2):
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    # ── Reason string ──
    reasons = []
    if confluence == 3:
        reasons.append("3/3 confluence")
    elif confluence == 2:
        reasons.append("2/3 confluence")
    if best_sharpe:
        reasons.append(f"Sharpe {best_sharpe}")
    if disc_sma:
        reasons.append(f"MA{disc_sma} {disc_direction} ({disc_tf})")
    if snap_convergence:
        reasons.append(f"conv {snap_convergence}")
    if snap_hits:
        reasons.append(f"{snap_hits} hits")

    return {
        "ticker":        ticker,
        "side":          side,
        "confidence":    confidence,
        "confluence":    confluence,
        "entry":         entry,
        "stop":          stop,
        "target":        target,
        "rr":            rr,
        "atr":           round(atr, 4),
        "best_sharpe":   best_sharpe or "",
        "best_win_rate": best_win_rate or "",
        "disc_tf":       disc_tf or "",
        "disc_sma":      f"MA{disc_sma}" if disc_sma else "",
        "disc_direction": disc_direction or "",
        "snap_tf":       snap_tf or "",
        "snap_outfit":   snap_outfit,
        "snap_hits":     snap_hits,
        "snap_conv":     snap_convergence,
        "reason":        " | ".join(reasons),
    }


# ─── Main ────────────────────────────────────────────────────────────────────

def run(
    min_confidence: str = "LOW",
    min_rr: float = 1.0,
    discovery_tf: list[str] | None = None,
    min_sharpe: float = 2.0,
):
    snapshot  = load_latest_snapshot()
    discovery = load_latest_discovery(timeframes=discovery_tf)
    backtest  = load_latest_backtest(min_sharpe=min_sharpe)
    candle_cache = load_candle_cache()

    all_tickers = set(snapshot) | set(discovery) | set(backtest)
    logging.info(f"Scoring {len(all_tickers)} unique tickers")

    trades = []
    for ticker in all_tickers:
        trade = build_trade(ticker, snapshot, discovery, backtest, candle_cache)
        if trade is None:
            continue
        if CONFIDENCE_LEVELS.index(trade["confidence"]) > CONFIDENCE_LEVELS.index(min_confidence):
            continue
        if trade["rr"] < min_rr:
            continue
        trades.append(trade)

    # Sort: confidence first, then R/R, then Sharpe
    conf_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    trades.sort(key=lambda t: (
        conf_order[t["confidence"]],
        -t["rr"],
        -(float(t["best_sharpe"]) if t["best_sharpe"] else 0),
    ))

    # ── Print ──
    print("\n" + "═" * 120)
    print(f"  TRADE ENGINE  |  {len(trades)} suggestions  |  min_confidence={min_confidence}  min_rr={min_rr}")
    print("═" * 120)
    print(f"  {'Ticker':<8} {'Conf':<8} {'Side':<5} {'Entry':>8} {'Stop':>8} {'Target':>8} {'R/R':>5}  {'Reason'}")
    print("─" * 120)

    for t in trades:
        print(
            f"  {t['ticker']:<8} {t['confidence']:<8} {t['side']:<5} "
            f"{t['entry']:>8} {t['stop']:>8} {t['target']:>8} {t['rr']:>5}  "
            f"{t['reason']}"
        )

    print("═" * 120)

    # ── Save ──
    if trades:
        TRADES_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        out_path = TRADES_DIR / f"trades_{ts}.csv"
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            writer.writeheader()
            writer.writerows(trades)
        logging.info(f"Saved: {out_path}")
        print(f"\n  Saved: {out_path}\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Paper Trade Suggestion Engine")
    parser.add_argument(
        "--min-confidence", type=str, default="LOW", choices=["HIGH", "MEDIUM", "LOW"],
        help="Minimum confidence level to include (default: LOW)"
    )
    parser.add_argument(
        "--min-rr", type=float, default=1.0,
        help="Minimum risk/reward ratio (default: 1.0)"
    )
    parser.add_argument(
        "--discovery-tf", type=str, default=None,
        help="Filter discovery to timeframes e.g. 1d,1w,1mo (default: all)"
    )
    parser.add_argument(
        "--min-sharpe", type=float, default=2.0,
        help="Minimum Sharpe to count backtest as valid (default: 2.0)"
    )
    args = parser.parse_args()

    tf_list = [t.strip() for t in args.discovery_tf.split(",")] if args.discovery_tf else None

    run(
        min_confidence=args.min_confidence,
        min_rr=args.min_rr,
        discovery_tf=tf_list,
        min_sharpe=args.min_sharpe,
    )
