"""
scoring_v3.py — Composite Level Significance Scorer

Replaces the hit-count / decisecond scoring of engines 1 and 2 with a
multi-factor composite that measures how significant a price level is,
not just how many times it was touched.

Score components:
  1. hit_rate        — hits ÷ lookback_bars (timeframe-neutral frequency)
  2. volume_ratio    — avg volume at hit bars ÷ overall avg volume
  3. hold_rate       — % of hits where price reversed (didn't break through)
  4. cross_outfit    — how many outfits point to same price ± tolerance
  5. cross_tf        — how many timeframes have hits at same price ± tolerance
  6. recency         — exponential decay — recent hits score higher
  7. persistence     — rank stability across prior cycles (InfluxDB)

Final score is a weighted product, normalized 0–100.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


# ── Weights ───────────────────────────────────────────────────────────────────
WEIGHT_HIT_RATE     = 0.25
WEIGHT_VOLUME       = 0.15
WEIGHT_HOLD_RATE    = 0.25
WEIGHT_CROSS_OUTFIT = 0.15
WEIGHT_CROSS_TF     = 0.10
WEIGHT_RECENCY      = 0.05
WEIGHT_PERSISTENCE  = 0.05

PRICE_TOLERANCE  = 0.05
RECENCY_HALFLIFE = 30


@dataclass
class LevelScore:
    ticker:          str
    timeframe:       str
    outfit_id:       int
    outfit_name:     str
    outfit_periods:  tuple
    entry_price:     float
    parm_period:     int
    parm_price:      float
    hit_rate:        float = 0.0
    volume_ratio:    float = 0.0
    hold_rate:       float = 0.0
    cross_outfit:    float = 0.0
    cross_tf:        float = 0.0
    recency:         float = 0.0
    persistence:     float = 0.0
    hit_count:       int   = 0
    lookback_bars:   int   = 0
    outfit_matches:  int   = 0
    tf_matches:      int   = 0
    composite:       float = 0.0

    def compute(self) -> None:
        weighted = (
            self.hit_rate     * WEIGHT_HIT_RATE     +
            self.volume_ratio * WEIGHT_VOLUME        +
            self.hold_rate    * WEIGHT_HOLD_RATE     +
            self.cross_outfit * WEIGHT_CROSS_OUTFIT  +
            self.cross_tf     * WEIGHT_CROSS_TF      +
            self.recency      * WEIGHT_RECENCY       +
            self.persistence  * WEIGHT_PERSISTENCE
        )
        self.composite = round(weighted * 100, 2)

    @property
    def grade(self) -> str:
        if self.composite >= 80: return "A+"
        if self.composite >= 70: return "A"
        if self.composite >= 60: return "B+"
        if self.composite >= 50: return "B"
        if self.composite >= 40: return "C"
        if self.composite >= 30: return "D"
        return "F"


def compute_hit_rate(hit_count: int, lookback_bars: int) -> float:
    if lookback_bars == 0:
        return 0.0
    return min(1.0, hit_count / lookback_bars)


def compute_volume_ratio(df: pd.DataFrame, hit_indices: list[int]) -> float:
    if df.empty or not hit_indices or "volume" not in df.columns:
        return 0.5
    overall_avg = df["volume"].mean()
    if overall_avg == 0:
        return 0.5
    hit_vols = [df["volume"].iloc[i] for i in hit_indices if i < len(df)]
    if not hit_vols:
        return 0.5
    hit_avg = sum(hit_vols) / len(hit_vols)
    ratio = hit_avg / overall_avg
    return round(1 / (1 + math.exp(-(ratio - 1))), 4)


def compute_hold_rate(
    df: pd.DataFrame,
    hit_indices: list[int],
    sma_value: float,
    lookforward: int = 3,
) -> float:
    if df.empty or not hit_indices or len(df) < 2:
        return 0.5
    holds = 0
    closes = df["close"].to_numpy()
    for idx in hit_indices:
        if idx >= len(closes) - 1:
            holds += 1
            continue
        end = min(idx + lookforward + 1, len(closes))
        future_closes = closes[idx + 1:end]
        if len(future_closes) == 0:
            holds += 1
            continue
        sma_break = any(abs(c - sma_value) / sma_value > 0.01 for c in future_closes)
        if not sma_break:
            holds += 1
    return round(holds / len(hit_indices), 4) if hit_indices else 0.5


def compute_recency(
    hit_timestamps: list[int],
    total_bars: int,
    halflife: int = RECENCY_HALFLIFE,
) -> float:
    if not hit_timestamps or total_bars == 0:
        return 0.0
    weights = []
    for idx in hit_timestamps:
        age = total_bars - 1 - idx
        w = math.exp(-age * math.log(2) / halflife)
        weights.append(w)
    max_weight = len(hit_timestamps) * 1.0
    raw = sum(weights) / max_weight if max_weight > 0 else 0.0
    return round(min(1.0, raw), 4)


def compute_cross_outfit_score(
    ticker: str,
    timeframe: str,
    price: float,
    all_entries: list[dict],
    current_outfit_id: int,
    tolerance: float = PRICE_TOLERANCE,
) -> tuple[float, int]:
    matches = sum(
        1 for e in all_entries
        if e["ticker"] == ticker
        and e["timeframe"] == timeframe
        and e["outfit_id"] != current_outfit_id
        and abs(e["entry_price"] - price) <= tolerance
    )
    score = math.log1p(matches) / math.log1p(41)
    return round(score, 4), matches


def compute_cross_tf_score(
    ticker: str,
    price: float,
    current_tf: str,
    all_entries: list[dict],
    tolerance: float = PRICE_TOLERANCE,
) -> tuple[float, int]:
    tfs_seen = set()
    for e in all_entries:
        if (e["ticker"] == ticker
                and e["timeframe"] != current_tf
                and abs(e["entry_price"] - price) <= tolerance):
            tfs_seen.add(e["timeframe"])
    count = len(tfs_seen)
    score = math.log1p(count) / math.log1p(10)
    return round(score, 4), count


def compute_persistence_score(
    ticker: str,
    timeframe: str,
    outfit_id: int,
    cumulative_ds: dict,
) -> float:
    if not cumulative_ds:
        return 0.0
    total = sum(
        v for (t, tf, oid, _), v in cumulative_ds.items()
        if t == ticker and tf == timeframe and oid == str(outfit_id)
    )
    if total <= 0:
        return 0.0
    z = (math.log10(total) - 6) / 2
    return round(1 / (1 + math.exp(-z)), 4)


def score_entry(
    entry,
    df,
    all_entries: list,
    cumulative_ds,
    lookback: int,
) -> "LevelScore":
    """Compute the full LevelScore for a single HashMapEntry."""
    parm = entry.key_variable or (entry.outfit_periods[0] if entry.outfit_periods else 0)
    parm_price = entry.key_variable_price or entry.last_hit_price or 0.0
    entry_price = entry.last_hit_price or 0.0

    ls = LevelScore(
        ticker         = entry.ticker,
        timeframe      = entry.timeframe,
        outfit_id      = entry.outfit_id,
        outfit_name    = "",
        outfit_periods = entry.outfit_periods,
        entry_price    = entry_price,
        parm_period    = parm,
        parm_price     = parm_price,
        hit_count      = entry.hit_count,
        lookback_bars  = lookback,
    )

    hit_indices    = [h.bar_index for h in entry.hits if h.bar_index < len(df)]
    hit_timestamps = hit_indices

    ls.hit_rate     = compute_hit_rate(entry.hit_count, lookback)
    ls.volume_ratio = compute_volume_ratio(df, hit_indices)
    ls.hold_rate    = compute_hold_rate(df, hit_indices, parm_price)

    ls.cross_outfit, ls.outfit_matches = compute_cross_outfit_score(
        entry.ticker, entry.timeframe, entry_price, all_entries, entry.outfit_id
    )
    ls.cross_tf, ls.tf_matches = compute_cross_tf_score(
        entry.ticker, entry_price, entry.timeframe, all_entries
    )
    ls.recency     = compute_recency(hit_timestamps, len(df))
    ls.persistence = compute_persistence_score(
        entry.ticker, entry.timeframe, entry.outfit_id, cumulative_ds or {}
    )

    ls.compute()
    return ls
