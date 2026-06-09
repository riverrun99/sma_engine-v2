"""
normalizer_engine.py — Timeframe-Normalized Signal Ranking Engine

Reads the latest snapshot CSV and re-ranks signals using volume-normalized,
timeframe-adjusted scores so that a strong 4h signal can compete fairly
against a strong 1mo signal.

Normalization approach:
  1. Within each timeframe group, compute z-score of raw scores
  2. Apply timeframe structural weight (longer = more weight, but capped)
  3. Combine with convergence bonus and hit percentile
  4. Final score is comparable across all timeframes

Does NOT modify any existing files. Read-only.

Usage:
    docker exec e47_engine python /app/normalizer_engine.py
    docker exec e47_engine python /app/normalizer_engine.py --top 100
    docker exec e47_engine python /app/normalizer_engine.py --top 50 --min-conv 2
"""

from __future__ import annotations

import csv
import math
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

OUTPUT_DIR  = Path("./output")
SNAP_DIR    = OUTPUT_DIR / "snapshots"
NORM_DIR    = OUTPUT_DIR / "normalized"

# Structural weight per timeframe (longer = more structural, but capped at 3x)
TF_WEIGHT: dict[str, float] = {
    "1mo": 3.0,
    "1w":  2.5,
    "1d":  2.0,
    "4h":  1.5,
    "2h":  1.3,
    "1h":  1.1,
    "30m": 1.0,
    "15m": 0.9,
    "5m":  0.8,
    "1m":  0.7,
}

# Convergence bonus
CONV_BONUS: dict[str, float] = {
    "4/4": 2.0,
    "3/4": 1.5,
    "2/4": 1.0,
    "1/4": 0.0,
}


def load_latest_snapshot() -> list[dict]:
    files = sorted(SNAP_DIR.glob("snapshot_*.csv"))
    if not files:
        logging.error("No snapshot files found.")
        return []
    latest = files[-1]
    logging.info(f"Snapshot: {latest.name}")
    with open(latest, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def normalize(rows: list[dict]) -> list[dict]:
    """
    Normalize scores across timeframes using z-score within each TF group,
    then apply structural weight and convergence bonus.
    """
    # Group by timeframe
    tf_groups: dict[str, list[dict]] = {}
    for r in rows:
        tf = r["timeframe"]
        tf_groups.setdefault(tf, []).append(r)

    normalized = []

    for tf, group in tf_groups.items():
        scores = [float(r["score"]) for r in group]

        if len(scores) < 2:
            mean = scores[0] if scores else 0
            std  = 1.0
        else:
            mean = sum(scores) / len(scores)
            variance = sum((s - mean) ** 2 for s in scores) / len(scores)
            std = math.sqrt(variance) if variance > 0 else 1.0

        tf_w = TF_WEIGHT.get(tf, 1.0)

        for r in group:
            raw   = float(r["score"])
            hits  = int(r["hits"])
            conv  = r.get("convergence", "1/4")

            # Z-score within timeframe (0–1 range via sigmoid)
            z = (raw - mean) / std
            z_norm = 1 / (1 + math.exp(-z))  # sigmoid: 0–1

            # Hit percentile within timeframe
            all_hits = [int(x["hits"]) for x in group]
            hit_pct = sum(1 for h in all_hits if h <= hits) / len(all_hits)

            # Convergence bonus
            conv_b = CONV_BONUS.get(conv, 0.0)

            # Final normalized score
            norm_score = (z_norm * 50) + (hit_pct * 30) + (conv_b * 10)
            norm_score *= tf_w  # structural weight

            normalized.append({
                "ticker":       r["ticker"],
                "timeframe":    tf,
                "outfit":       r.get("outfit", ""),
                "hits":         hits,
                "convergence":  conv,
                "raw_score":    round(raw, 2),
                "tf_weight":    tf_w,
                "z_norm":       round(z_norm, 4),
                "hit_pct":      round(hit_pct * 100, 1),
                "conv_bonus":   conv_b,
                "norm_score":   round(norm_score, 4),
                "tier":         _tier(tf),
            })

    return normalized


def _tier(tf: str) -> str:
    if tf in ("1mo", "1w"):
        return "Structural"
    elif tf in ("1d", "4h", "2h"):
        return "Swing"
    else:
        return "Intraday"


def print_results(rows: list[dict], top: int = 50, min_conv: int = 0) -> None:
    filtered = [r for r in rows if int(r["convergence"].split("/")[0]) >= min_conv]
    filtered.sort(key=lambda r: -r["norm_score"])
    filtered = filtered[:top]

    print("\n" + "═" * 100)
    print(f"  NORMALIZER ENGINE  |  Top {len(filtered)} signals  |  Timeframe-adjusted ranking")
    print("═" * 100)
    print(f"  {'#':<4} {'Ticker':<8} {'TF':<6} {'Tier':<12} {'Hits':<8} {'Conv':<6} "
          f"{'TF Wt':<7} {'Z%':<7} {'Hit%':<7} {'Conv+':<7} {'Norm Score'}")
    print("─" * 100)

    for i, r in enumerate(filtered, 1):
        print(
            f"  {i:<4} {r['ticker']:<8} {r['timeframe']:<6} {r['tier']:<12} "
            f"{r['hits']:<8} {r['convergence']:<6} {r['tf_weight']:<7} "
            f"{r['z_norm']*100:.1f}%  {r['hit_pct']:.1f}%  {r['conv_bonus']:<7} {r['norm_score']:.2f}"
        )

    print("═" * 100)


def save_results(rows: list[dict], top: int = 50, min_conv: int = 0) -> None:
    filtered = [r for r in rows if int(r["convergence"].split("/")[0]) >= min_conv]
    filtered.sort(key=lambda r: -r["norm_score"])
    filtered = filtered[:top]

    NORM_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    out_path = NORM_DIR / f"normalized_{ts}.csv"

    fieldnames = ["rank", "ticker", "timeframe", "tier", "outfit", "hits", "convergence",
                  "raw_score", "tf_weight", "z_norm", "hit_pct", "conv_bonus", "norm_score"]

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for i, r in enumerate(filtered, 1):
            writer.writerow({"rank": i, **r})

    logging.info(f"Saved: {out_path}")
    print(f"\n  Saved: {out_path}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Timeframe-Normalized Signal Ranking Engine")
    parser.add_argument("--top", type=int, default=50,
                        help="Number of top signals to show (default: 50)")
    parser.add_argument("--min-conv", type=int, default=0,
                        help="Minimum convergence layers (e.g. 2 = only 2/4+ signals)")
    args = parser.parse_args()

    rows = load_latest_snapshot()
    if not rows:
        exit(1)

    logging.info(f"Normalizing {len(rows)} signals across timeframes...")
    normalized = normalize(rows)

    print_results(normalized, top=args.top, min_conv=args.min_conv)
    save_results(normalized, top=args.top, min_conv=args.min_conv)
