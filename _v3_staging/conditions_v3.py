"""
conditions_v3.py — Sequential Conditional Layer

Implements the PARM logic described by @UnfairMarket:

  "Firms hold the program during the drop IF precision detections on
   micro-term SMA on the same outfit are detected/positive, overriding
   the candle close below parameter. It's sequential."

For each scored signal:
  1. Get PARM (key variable SMA period and price)
  2. Check if current candle close is ABOVE or BELOW the PARM
  3. If BELOW: check micro-term (1m, 5m) same outfit, same ticker
     → Micro-term ACTIVE  = HOLD  (precision detections override the drop)
     → Micro-term ABSENT  = IGNORE (no support at micro level — exit)
  4. If ABOVE: check micro-term confirmation
     → Micro-term ACTIVE  = STRONG (confirmed at all levels)
     → Micro-term ABSENT  = WEAK   (higher TF only, no micro confirmation)

Output state: STRONG | HOLD | WEAK | IGNORE
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


# Micro-term timeframes that can override the PARM close-below condition
MICRO_TIMEFRAMES = {"1m", "5m"}

# How close candle must be to PARM to be considered "at" the level (%)
PARM_PROXIMITY_PCT = 0.005  # 0.5%


@dataclass
class ConditionalState:
    """Result of the sequential conditional evaluation for one signal."""
    ticker:          str
    timeframe:       str
    outfit_id:       int
    outfit_periods:  tuple

    parm_period:     int
    parm_price:      float
    close_price:     float

    # Relationship to PARM
    close_vs_parm:   str    = ""       # "above" | "below" | "at"
    parm_distance:   float  = 0.0      # % distance from close to PARM
    parm_distance_dollars: float = 0.0

    # Micro-term check
    micro_active:    bool   = False    # any micro-term same-outfit hits found
    micro_tfs:       list   = None     # which micro TFs had hits

    # Final state
    state:           str    = "WEAK"   # STRONG | HOLD | WEAK | IGNORE

    # Entry/Stop/Target
    entry:           float  = 0.0
    stop:            float  = 0.0
    target:          float  = 0.0

    def __post_init__(self):
        if self.micro_tfs is None:
            self.micro_tfs = []

    @property
    def state_emoji(self) -> str:
        return {
            "STRONG": "🟢",
            "HOLD":   "🟡",
            "WEAK":   "🔵",
            "IGNORE": "🔴",
        }.get(self.state, "⬜")

    @property
    def actionable(self) -> bool:
        return self.state in ("STRONG", "HOLD")


def evaluate_condition(
    entry,                          # HashMapEntry from engine_v3
    df: pd.DataFrame,               # candle data for this ticker/tf
    all_store_entries: list,        # all HashMapEntry objects in the store
    candle_cache: dict,             # full cache for micro-term lookup
) -> ConditionalState:
    """
    Run the full sequential conditional evaluation for one signal entry.
    """
    parm = entry.key_variable or (entry.outfit_periods[0] if entry.outfit_periods else 0)
    parm_price = entry.key_variable_price or entry.last_hit_price or 0.0
    close_price = float(df["close"].iloc[-1]) if not df.empty else 0.0

    cs = ConditionalState(
        ticker         = entry.ticker,
        timeframe      = entry.timeframe,
        outfit_id      = entry.outfit_id,
        outfit_periods = entry.outfit_periods,
        parm_period    = parm,
        parm_price     = parm_price,
        close_price    = close_price,
        entry          = parm_price,
    )

    if parm_price <= 0 or close_price <= 0:
        cs.state = "WEAK"
        return cs

    # ── Step 1: Close vs PARM ─────────────────────────────────────────────────
    dist_pct = (close_price - parm_price) / parm_price
    cs.parm_distance         = round(abs(dist_pct) * 100, 3)
    cs.parm_distance_dollars = round(abs(close_price - parm_price), 2)

    if abs(dist_pct) <= PARM_PROXIMITY_PCT:
        cs.close_vs_parm = "at"
    elif close_price > parm_price:
        cs.close_vs_parm = "above"
    else:
        cs.close_vs_parm = "below"

    # ── Step 2: Micro-term same-outfit check ──────────────────────────────────
    micro_tfs_active = []
    for e in all_store_entries:
        if (e.ticker == entry.ticker
                and e.outfit_periods == entry.outfit_periods
                and e.timeframe in MICRO_TIMEFRAMES
                and e.timeframe != entry.timeframe
                and e.hit_count > 0):
            micro_tfs_active.append(e.timeframe)

    cs.micro_active = len(micro_tfs_active) > 0
    cs.micro_tfs    = sorted(set(micro_tfs_active))

    # ── Step 3: Sequential conditional ───────────────────────────────────────
    if cs.close_vs_parm == "below":
        if cs.micro_active:
            cs.state = "HOLD"    # precision micro detections override the drop
        else:
            cs.state = "IGNORE"  # no micro support — level failed
    elif cs.close_vs_parm == "at":
        if cs.micro_active:
            cs.state = "STRONG"  # sitting right at level with micro confirmation
        else:
            cs.state = "WEAK"    # at level but no micro confirmation yet
    else:  # above
        if cs.micro_active:
            cs.state = "STRONG"  # above PARM + micro confirms = highest quality
        else:
            cs.state = "WEAK"    # above PARM but no micro confirmation

    # ── Step 4: Entry / Stop / Target ─────────────────────────────────────────
    cs.entry = parm_price

    # Stop: penny below the next lower SMA period in the outfit
    periods = sorted(entry.outfit_periods)
    parm_idx = periods.index(parm) if parm in periods else 0
    if parm_idx > 0:
        lower_period = periods[parm_idx - 1]
        # Approximate stop using the parm price ratio
        cs.stop = round(parm_price * (lower_period / parm) * 0.999, 2)
    else:
        cs.stop = round(parm_price * 0.99, 2)  # 1% below as fallback

    # Target: next higher SMA period price (approx)
    if parm_idx < len(periods) - 1:
        upper_period = periods[parm_idx + 1]
        cs.target = round(parm_price * (upper_period / parm) * 1.001, 2)
    else:
        cs.target = round(parm_price * 1.02, 2)  # 2% above as fallback

    return cs


def filter_actionable(states: list[ConditionalState]) -> list[ConditionalState]:
    """Return only STRONG and HOLD states, sorted by state then score."""
    order = {"STRONG": 0, "HOLD": 1, "WEAK": 2, "IGNORE": 3}
    actionable = [s for s in states if s.actionable]
    return sorted(actionable, key=lambda s: order[s.state])


def summarize_states(states: list[ConditionalState]) -> dict:
    """Count signals by state for dashboard summary."""
    counts = {"STRONG": 0, "HOLD": 0, "WEAK": 0, "IGNORE": 0}
    for s in states:
        counts[s.state] = counts.get(s.state, 0) + 1
    return counts
