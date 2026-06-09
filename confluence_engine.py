"""
confluence_engine.py — Cross-Reference Signal Confluence Engine

Reads the three existing output sources and scores each ticker by how many
engines agree on it:

  +1  Main ranker (latest snapshot)        — hit frequency × volume signal
  +1  Discovery engine (latest discovery)  — first-touch structural signal
  +1  Backtest (latest backtest CSV)       — historically proven outfit (Sharpe threshold)

A ticker scoring 3/3 is the strongest possible signal.

Does NOT modify any existing files. Read-only.

Usage:
    docker exec e47_engine python /app/confluence_engine.py
    docker exec e47_engine python /app/confluence_engine.py --min-score 2
    docker exec e47_engine python /app/confluence_engine.py --min-sharpe 3.0 --discovery-tf 1d,1w,1mo
"""

from __future__ import annotations

import csv
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

OUTPUT_DIR    = Path("./output")
SNAPSHOT_DIR  = OUTPUT_DIR / "snapshots"
DISCOVERY_DIR = OUTPUT_DIR / "discovery"


# ─── Loaders ─────────────────────────────────────────────────────────────────

def load_latest_snapshot() -> dict[str, dict]:
    """Load the most recent snapshot CSV. Returns {ticker: row}."""
    files = sorted(SNAPSHOT_DIR.glob("snapshot_*.csv"))
    if not files:
        logging.warning("No snapshot files found.")
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
    """Load the most recent discovery CSV. Returns {ticker: [signals]}."""
    files = sorted(DISCOVERY_DIR.glob("discovery_*.csv"))
    if not files:
        logging.warning("No discovery files found.")
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
    """Load the most recent backtest CSV. Returns {ticker: [rows above min_sharpe]}."""
    files = sorted(OUTPUT_DIR.glob("backtest_*.csv"))
    if not files:
        logging.warning("No backtest files found.")
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


# ─── Confluence scoring ───────────────────────────────────────────────────────

def score_confluence(
    snapshot: dict[str, dict],
    discovery: dict[str, list[dict]],
    backtest: dict[str, list[dict]],
) -> list[dict]:
    """Score each ticker across all three sources."""
    all_tickers = set(snapshot) | set(discovery) | set(backtest)
    results = []

    for ticker in sorted(all_tickers):
        in_snapshot  = ticker in snapshot
        in_discovery = ticker in discovery
        in_backtest  = ticker in backtest

        score = sum([in_snapshot, in_discovery, in_backtest])

        # Best Sharpe from backtest
        best_sharpe = None
        best_win_rate = None
        best_outfit = None
        if in_backtest:
            best_bt = max(backtest[ticker], key=lambda r: float(r["sharpe"]))
            best_sharpe   = round(float(best_bt["sharpe"]), 2)
            best_win_rate = round(float(best_bt["win_rate"]), 2)
            best_outfit   = best_bt.get("outfit_id", "")

        # Discovery direction and SMA
        discovery_direction = None
        discovery_sma = None
        discovery_tf = None
        if in_discovery:
            # Pick the signal with the largest SMA period
            best_disc = max(discovery[ticker], key=lambda r: int(r["sma_period"]))
            discovery_direction = best_disc["direction"]
            discovery_sma       = int(best_disc["sma_period"])
            discovery_tf        = best_disc["timeframe"]

        # Snapshot data
        snap_tf    = snapshot[ticker]["timeframe"] if in_snapshot else None
        snap_score = snapshot[ticker]["score"] if in_snapshot else None
        snap_hits  = snapshot[ticker]["hits"] if in_snapshot else None
        snap_outfit = snapshot[ticker]["outfit"] if in_snapshot else None

        results.append({
            "ticker":               ticker,
            "score":                score,
            "in_snapshot":          in_snapshot,
            "in_discovery":         in_discovery,
            "in_backtest":          in_backtest,
            "snap_tf":              snap_tf,
            "snap_outfit":          snap_outfit,
            "snap_hits":            snap_hits,
            "snap_score":           snap_score,
            "discovery_tf":         discovery_tf,
            "discovery_sma":        discovery_sma,
            "discovery_direction":  discovery_direction,
            "best_sharpe":          best_sharpe,
            "best_win_rate":        best_win_rate,
            "best_outfit":          best_outfit,
        })

    results.sort(key=lambda r: (-r["score"], -(r["best_sharpe"] or 0)))
    return results


# ─── Output ───────────────────────────────────────────────────────────────────

def print_results(results: list[dict], min_score: int = 1) -> None:
    filtered = [r for r in results if r["score"] >= min_score]

    print("\n" + "═" * 110)
    print(f"  CONFLUENCE ENGINE  |  {len(filtered)} tickers scoring >= {min_score}/3")
    print("═" * 110)
    print(f"  {'Ticker':<8} {'Score':<7} {'Snap':<5} {'Disc':<5} {'BT':<5} "
          f"{'Snap TF':<7} {'Disc TF':<7} {'Disc SMA':<10} {'Direction':<14} "
          f"{'Sharpe':<8} {'WinRate':<8} {'Snap Hits'}")
    print("─" * 110)

    for r in filtered:
        score_str     = f"{r['score']}/3"
        snap_str      = "✓" if r["in_snapshot"]  else "-"
        disc_str      = "✓" if r["in_discovery"] else "-"
        bt_str        = "✓" if r["in_backtest"]  else "-"
        direction_str = r["discovery_direction"] or "-"
        sharpe_str    = str(r["best_sharpe"])   if r["best_sharpe"]   is not None else "-"
        wr_str        = str(r["best_win_rate"]) if r["best_win_rate"] is not None else "-"
        sma_str       = f"MA{r['discovery_sma']}" if r["discovery_sma"] else "-"
        disc_tf_str   = r["discovery_tf"] or "-"
        snap_tf_str   = r["snap_tf"] or "-"
        hits_str      = r["snap_hits"] or "-"

        print(
            f"  {r['ticker']:<8} {score_str:<7} {snap_str:<5} {disc_str:<5} {bt_str:<5} "
            f"{snap_tf_str:<7} {disc_tf_str:<7} {sma_str:<10} {direction_str:<14} "
            f"{sharpe_str:<8} {wr_str:<8} {hits_str}"
        )

    print("═" * 110)


def save_csv(results: list[dict], min_score: int = 1) -> None:
    filtered = [r for r in results if r["score"] >= min_score]
    out_dir = OUTPUT_DIR / "confluence"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    out_path = out_dir / f"confluence_{ts}.csv"
    fieldnames = list(filtered[0].keys()) if filtered else []
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(filtered)
    logging.info(f"Saved: {out_path}")
    print(f"\n  Saved: {out_path}\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Confluence Signal Engine")
    parser.add_argument(
        "--min-score", type=int, default=1,
        help="Minimum confluence score to display (1-3, default: 1)"
    )
    parser.add_argument(
        "--min-sharpe", type=float, default=2.0,
        help="Minimum Sharpe ratio to count a backtest as valid (default: 2.0)"
    )
    parser.add_argument(
        "--discovery-tf", type=str, default=None,
        help="Filter discovery signals by timeframe (e.g. 1d,1w,1mo). Default: all."
    )
    args = parser.parse_args()

    tf_list = [t.strip() for t in args.discovery_tf.split(",")] if args.discovery_tf else None

    snapshot  = load_latest_snapshot()
    discovery = load_latest_discovery(timeframes=tf_list)
    backtest  = load_latest_backtest(min_sharpe=args.min_sharpe)

    if not any([snapshot, discovery, backtest]):
        print("No data found. Run the main engine, discovery engine, and backtest first.")
        exit(1)

    results = score_confluence(snapshot, discovery, backtest)
    print_results(results, min_score=args.min_score)

    if any(r["score"] >= args.min_score for r in results):
        save_csv(results, min_score=args.min_score)
