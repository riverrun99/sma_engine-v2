"""
significance.py — randomization significance testing and FDR control.

Problem: with 1.36M [outfit × ticker × timeframe] combinations and pure
frequency-based ranking, the highest-ranked signals are guaranteed to include
multiple-hypothesis-testing artifacts. A "47-hit outfit" might be real signal,
or it might be the right tail of pure noise across millions of trials.

Solution:
  1. For each candidate entry, generate a null distribution by permuting the
     close price sequence and recomputing hits. p-value = fraction of null
     hit counts ≥ observed.
  2. Apply Benjamini-Hochberg FDR correction across all tested combinations
     to control the expected false discovery rate.
  3. Only entries passing the FDR threshold are promoted to "significant
     signals."

Permutation test rationale: shuffling close prices destroys any genuine
SMA-OHLC structural relationship while preserving the marginal distribution
of prices and the SMA computation logic. If an outfit produces hit counts
indistinguishable from shuffled data, it's noise.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional

from engine import compute_smas, detect_hits, HashMapEntry


@dataclass
class SignificanceResult:
    entry_key: str
    observed_hits: int
    null_mean: float
    null_std: float
    p_value: float
    z_score: float
    n_permutations: int
    significant_at_05: bool = False
    significant_after_fdr: bool = False


def permutation_test(
    df: pd.DataFrame,
    ticker: str,
    timeframe: str,
    outfit: dict,
    lookback: int,
    n_permutations: int = 200,
    rng_seed: int = 42,
) -> SignificanceResult:
    """Permutation test for one [ticker × tf × outfit] entry.

    Procedure:
      1. Compute observed hit count on real data.
      2. For n_permutations iterations:
         - Shuffle the close price sequence
         - Recompute SMAs and OHLC arrays consistently with shuffled closes
         - Count hits
      3. p_value = (1 + count of null_hits >= observed) / (1 + n_permutations)
         (the +1s prevent zero p-values, standard convention)

    Note: we shuffle closes and reconstruct OHLC by reapplying the original
    intra-bar deltas (high-close, close-low, etc). This preserves intra-bar
    structure while destroying the inter-bar SMA relationship — exactly the
    null we want to test against.
    """
    if len(df) == 0:
        return SignificanceResult(
            entry_key=f"{ticker}|{timeframe}|{outfit['id']}",
            observed_hits=0, null_mean=0.0, null_std=0.0,
            p_value=1.0, z_score=0.0, n_permutations=0,
        )

    # Observed hit count
    observed_hits = len(detect_hits(df, ticker, timeframe, outfit, lookback))

    # Pre-compute intra-bar deltas to preserve OHLC structure under permutation
    closes = df["close"].to_numpy()
    open_delta = df["open"].to_numpy() - closes
    high_delta = df["high"].to_numpy() - closes
    low_delta = df["low"].to_numpy() - closes

    rng = np.random.default_rng(rng_seed)
    null_counts = np.zeros(n_permutations, dtype=int)

    for i in range(n_permutations):
        # Shuffle closes
        perm_idx = rng.permutation(len(closes))
        perm_closes = closes[perm_idx]

        # Reconstruct OHLC with original deltas applied to permuted closes
        perm_df = pd.DataFrame({
            "timestamp": df["timestamp"].values,
            "open":  np.round(perm_closes + open_delta, 2),
            "high":  np.round(perm_closes + high_delta, 2),
            "low":   np.round(perm_closes + low_delta, 2),
            "close": np.round(perm_closes, 2),
            "volume": df["volume"].values,
        })

        null_counts[i] = len(detect_hits(perm_df, ticker, timeframe, outfit, lookback))

    null_mean = float(null_counts.mean())
    null_std = float(null_counts.std())
    # Standard permutation p-value with +1 correction
    p_value = (1 + int(np.sum(null_counts >= observed_hits))) / (1 + n_permutations)
    z_score = (observed_hits - null_mean) / null_std if null_std > 0 else 0.0

    return SignificanceResult(
        entry_key=f"{ticker}|{timeframe}|{outfit['id']}",
        observed_hits=observed_hits,
        null_mean=null_mean,
        null_std=null_std,
        p_value=p_value,
        z_score=z_score,
        n_permutations=n_permutations,
        significant_at_05=(p_value < 0.05),
    )


def benjamini_hochberg(
    results: list[SignificanceResult],
    fdr_level: float = 0.05,
) -> list[SignificanceResult]:
    """Apply Benjamini-Hochberg FDR correction in place.

    Sets `significant_after_fdr` flag on each result. The largest p-value that
    satisfies p_(i) <= (i/m) * fdr_level defines the threshold; all p-values
    at or below that threshold are flagged significant.

    Returns the same list (modified in place), sorted by p-value ascending.
    """
    if not results:
        return results

    m = len(results)
    results.sort(key=lambda r: r.p_value)

    # Find largest i such that p_(i) <= (i/m) * fdr_level
    threshold_p = 0.0
    for i, r in enumerate(results, start=1):
        if r.p_value <= (i / m) * fdr_level:
            threshold_p = r.p_value

    for r in results:
        r.significant_after_fdr = (r.p_value <= threshold_p)

    return results


def test_top_entries(
    store_entries: list[HashMapEntry],
    candle_cache: dict[tuple[str, str], pd.DataFrame],
    outfits_by_id: dict[int, dict],
    top_n: int = 50,
    n_permutations: int = 200,
    fdr_level: float = 0.05,
    lookback: int = 130,
) -> list[SignificanceResult]:
    """Run permutation test on top N entries by hit count, then BH-correct.

    We only test the top N because permutation testing is expensive
    (O(n_permutations × len(df) × n_outfit_periods)). Lower-ranked entries
    are even less likely to be significant.
    """
    sorted_entries = sorted(store_entries, key=lambda e: e.hit_count, reverse=True)[:top_n]

    results: list[SignificanceResult] = []
    for entry in sorted_entries:
        df = candle_cache.get((entry.ticker, entry.timeframe))
        if df is None or len(df) == 0:
            continue
        outfit = outfits_by_id.get(entry.outfit_id)
        if outfit is None:
            continue
        result = permutation_test(
            df, entry.ticker, entry.timeframe, outfit, lookback, n_permutations,
        )
        results.append(result)

    return benjamini_hochberg(results, fdr_level=fdr_level)


def render_significance_table(results: list[SignificanceResult], top: int = 20) -> str:
    """Format significance results as a text table."""
    lines = []
    lines.append("─" * 90)
    lines.append(f"  SIGNIFICANCE TESTING — top {min(top, len(results))} of {len(results)} tested")
    lines.append("─" * 90)
    lines.append(f"  {'Entry':<35} {'Obs':>5} {'Null μ':>8} {'Null σ':>7} {'z':>6} {'p':>8} {'p<.05':>6} {'FDR':>4}")
    lines.append("─" * 90)
    for r in results[:top]:
        sig05 = "✓" if r.significant_at_05 else " "
        sigfdr = "✓" if r.significant_after_fdr else " "
        lines.append(
            f"  {r.entry_key:<35} {r.observed_hits:>5} {r.null_mean:>8.2f} "
            f"{r.null_std:>7.2f} {r.z_score:>6.2f} {r.p_value:>8.4f} {sig05:>6} {sigfdr:>4}"
        )
    n_sig = sum(1 for r in results if r.significant_after_fdr)
    lines.append("─" * 90)
    lines.append(f"  {n_sig}/{len(results)} entries significant after BH-FDR correction")
    lines.append("─" * 90)
    return "\n".join(lines)
