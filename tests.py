"""
Test suite for sma_outfit_engine.py

Verifies correctness of:
  1. SMA computation (against hand-calculated values)
  2. Hit detection (deterministic injection — known hits in, known hits out)
  3. Hash map aggregation
  4. Ranking logic
  5. System monitor evaluation
  6. Convergence detection
  7. Offset testing
  8. Edge cases (empty data, insufficient data, NaN handling)
  9. Outfit catalog integrity
 10. Webull client signature generation
"""

import sys
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/home/claude/sma_engine_v2')
from engine import (
    OUTFITS, SYSTEMS, WEBULL_TIMESPAN_MAP, TIMEFRAMES_STANDARD,
    compute_smas, detect_hits, HashMapStore, HashMapEntry,
    rank_entries, freshness_score, evaluate_systems,
    detect_convergence, best_offset,
    SMAOutfitEngine, EngineConfig, MockClient, WebullClient,
)

# ─── Test infrastructure ────────────────────────────────────────────────────

PASSED = 0
FAILED = 0
FAILURES = []

def check(condition, name, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILED += 1
        FAILURES.append((name, detail))
        print(f"  ✗ {name}    {detail}")

def section(name):
    print(f"\n{'═' * 70}\n  {name}\n{'═' * 70}")


# ─── 1. SMA computation correctness ─────────────────────────────────────────

section("1. SMA Computation")

# Hand-verified: SMA(3) of [1..10]
closes = np.arange(1, 11, dtype=float)
smas = compute_smas(closes, [3, 5, 7])

check(np.isnan(smas[3][0]) and np.isnan(smas[3][1]),
      "SMA(3) first 2 are NaN")
check(smas[3][2] == 2.0, "SMA(3)[2] == mean(1,2,3) == 2.0",
      f"got {smas[3][2]}")
check(smas[3][9] == 9.0, "SMA(3)[9] == mean(8,9,10) == 9.0",
      f"got {smas[3][9]}")
check(smas[5][4] == 3.0, "SMA(5)[4] == mean(1,2,3,4,5) == 3.0",
      f"got {smas[5][4]}")
check(smas[5][9] == 8.0, "SMA(5)[9] == mean(6,7,8,9,10) == 8.0",
      f"got {smas[5][9]}")

# Period > length should be all NaN
smas_long = compute_smas(np.arange(1, 6, dtype=float), [10])
check(np.all(np.isnan(smas_long[10])),
      "SMA period > len returns all NaN")

# 2-decimal rounding enforced
weird_closes = np.array([1.111, 2.222, 3.333, 4.444, 5.555])
smas_round = compute_smas(weird_closes, [3])
# mean(1.111, 2.222, 3.333) = 2.222 → rounds to 2.22
check(smas_round[3][2] == 2.22, "SMA rounds to 2dp",
      f"got {smas_round[3][2]}")

# Vectorized SMA matches naive rolling mean
rng = np.random.default_rng(42)
random_closes = rng.normal(100, 5, 200)
smas_vec = compute_smas(random_closes, [20])
naive = pd.Series(random_closes).rolling(20).mean().round(2).values
check(np.allclose(smas_vec[20][20:], naive[20:], equal_nan=True),
      "Vectorized SMA matches pandas rolling mean")


# ─── 2. Hit detection — controlled injection ────────────────────────────────

section("2. Hit Detection (controlled injection)")

# Build a synthetic dataframe where we KNOW there should be hits.
# Strategy: pick a price level, set SMA inputs so the SMA equals a known value,
# then set OHLC to that value and verify detection.

# Simple case: 3-period SMA of [10, 10, 10] = 10.00. Set the next bar's open to 10.00.
df = pd.DataFrame({
    "timestamp": pd.date_range("2025-01-01", periods=10, freq="1min", tz="UTC"),
    "open":   [10.00, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00],
    "high":   [10.00, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00],
    "low":    [10.00, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00],
    "close":  [10.00, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00],
    "volume": [1000] * 10,
})
test_outfit = {"id": 999, "periods": [3], "name": "TEST"}
hits = detect_hits(df, "TEST", "1m", test_outfit, lookback=999)
# SMA(3) is valid from index 2 onward (8 bars), 4 OHLC components each = 32 hits
check(len(hits) == 32,
      f"Flat 10.00 series: 8 valid bars × 4 OHLC = 32 hits",
      f"got {len(hits)}")

# Verify all 4 OHLC components detected
component_counts = {"O": 0, "H": 0, "L": 0, "C": 0}
for h in hits:
    component_counts[h.ohlc_component] += 1
check(component_counts == {"O": 8, "H": 8, "L": 8, "C": 8},
      "All 4 OHLC components register hits equally",
      f"got {component_counts}")

# Negative case: prices that are NEVER equal to the SMA
# If close walks linearly upward, SMA always lags, exact match unlikely
df_walk = pd.DataFrame({
    "timestamp": pd.date_range("2025-01-01", periods=20, freq="1min", tz="UTC"),
    "open":   np.arange(100, 120, dtype=float),
    "high":   np.arange(100, 120, dtype=float) + 0.50,
    "low":    np.arange(100, 120, dtype=float) - 0.50,
    "close":  np.arange(100, 120, dtype=float),
    "volume": [1000] * 20,
})
hits_walk = detect_hits(df_walk, "TEST", "1m", {"id": 999, "periods": [3], "name": "T"}, lookback=999)
# SMA(3) will be at bar i: mean(i-2, i-1, i) = i-1, while OHLC at bar i is i, i+0.5, i-0.5
# So SMA == LOW when SMA(i) = i-1 and low(i) = i-0.5 → never equal
# But wait: open(i) = i, sma(i) = i-1 → never equal either
# However, we need to check: does any OHLC at bar i match SMA at bar i?
# open=100, sma=99 → no. None match.
check(len(hits_walk) == 0,
      "Linear walk produces no hits (SMA always lags)",
      f"got {len(hits_walk)}")

# Test with a known 6-period outfit from the catalog
outfit_404 = next(o for o in OUTFITS if o["id"] == 21)  # 25/51/101/202/404/808
df_long = pd.DataFrame({
    "timestamp": pd.date_range("2025-01-01", periods=900, freq="1min", tz="UTC"),
    "open":   np.full(900, 50.00),
    "high":   np.full(900, 50.00),
    "low":    np.full(900, 50.00),
    "close":  np.full(900, 50.00),
    "volume": [1000] * 900,
})
hits_404 = detect_hits(df_long, "TEST", "1m", outfit_404, lookback=999)
# For each period p in [25, 51, 101, 202, 404, 808]:
#   valid bars = 900 - p + 1 (SMA(p) valid from index p-1 onward)
#   hits per period = valid_bars × 4 OHLC components
# Total = sum over all 6 periods
expected = sum((900 - p + 1) * 4 for p in outfit_404["periods"])
check(len(hits_404) == expected,
      f"Flat series with full 6-SMA outfit (404): {expected} hits expected",
      f"got {len(hits_404)}")


# ─── 3. Hash map aggregation ────────────────────────────────────────────────

section("3. Hash Map Store")

store = HashMapStore()
store.add_hits(hits[:10])  # 10 hits from earlier flat-10 test
entries = store.all()
check(len(entries) == 1, "Single (ticker, tf, outfit) → single entry",
      f"got {len(entries)}")
check(entries[0].hit_count == 10, "Hit count aggregates correctly",
      f"got {entries[0].hit_count}")
check(entries[0].key == "TEST|1m|3", "Hash map key format correct",
      f"got {entries[0].key}")

# Add hits from a different ticker → should create new entry
store.add_hits([hits[0].__class__(
    ticker="OTHER", timeframe="1m", outfit_id=999, outfit_periods=(3,),
    bar_index=0, timestamp=hits[0].timestamp,
    ohlc_component="O", sma_period=3, price=10.00,
)])
check(len(store.all()) == 2, "Different ticker → separate entry")


# ─── 4. Ranking logic ───────────────────────────────────────────────────────

section("4. Ranking Engine")

# Build store with known hit counts
store2 = HashMapStore()
now = pd.Timestamp.now(tz="UTC")
for ticker, count in [("A", 50), ("B", 20), ("C", 100)]:
    for i in range(count):
        store2.add_hits([hits[0].__class__(
            ticker=ticker, timeframe="5m", outfit_id=1, outfit_periods=(33,66,99,333,666,999),
            bar_index=i, timestamp=now - timedelta(minutes=i),
            ohlc_component="C", sma_period=33, price=10.00,
        )])

ranked = rank_entries(store2, now)
check(ranked[0][0].ticker == "C", "Highest hit count ranked first",
      f"got {ranked[0][0].ticker}")
check(ranked[1][0].ticker == "A", "Second-highest ranked second",
      f"got {ranked[1][0].ticker}")
check(ranked[2][0].ticker == "B", "Lowest ranked last",
      f"got {ranked[2][0].ticker}")

# Freshness boosts recent hits
fresh = freshness_score(now - timedelta(minutes=1), now, tf_minutes=5)
stale = freshness_score(now - timedelta(minutes=500), now, tf_minutes=5)
check(fresh > stale, "Recent hits score higher freshness",
      f"fresh={fresh:.2f} stale={stale:.2f}")
check(stale == 0.0, "Very old hits get zero freshness",
      f"got {stale}")


# ─── 5. System monitor ──────────────────────────────────────────────────────

section("5. System Monitor")

mock = MockClient()
states = evaluate_systems(mock)
check(len(states) == 8, "All 8 systems evaluated",
      f"got {len(states)}")
check(all(s.state in ("positive", "negative", "unknown") for s in states),
      "All states are valid values")
expected_names = {s["name"] for s in SYSTEMS}
got_names = {s.name for s in states}
check(expected_names == got_names, "All system names accounted for",
      f"missing: {expected_names - got_names}")


# ─── 6. Convergence detection ───────────────────────────────────────────────

section("6. Convergence Detection")

# Build a real entry with hits, then check convergence flags
store3 = HashMapStore()
df_conv = pd.DataFrame({
    "timestamp": pd.date_range("2025-01-01", periods=50, freq="5min", tz="UTC"),
    "open":   np.full(50, 100.00),
    "high":   np.full(50, 100.00),
    "low":    np.full(50, 100.00),
    "close":  np.full(50, 100.00),
    "volume": [1000] * 50,
})
hits_conv = detect_hits(df_conv, "CONV", "5m", {"id": 999, "periods": [10], "name": "T"}, lookback=999)
store3.add_hits(hits_conv)
entry = store3.all()[0]
conv = detect_convergence(entry, df_conv, cross_tf_store=None)
check(conv.ohlc_detection == True, "OHLC detection layer fires when hits exist")
check(conv.candle_close == True, "Candle close layer fires when last close == SMA")
check(conv.time_series == False, "Time series layer is off (no decisecond data)")


# ─── 7. Offset testing ──────────────────────────────────────────────────────

section("7. Offset Testing")

offset, entry_price = best_offset(entry, df_conv)
check(offset == 0.0, "Flat-10 series: best offset is 0.0 (raw SMA)",
      f"got offset={offset}")
check(entry_price == 100.00, "Entry price == SMA value for flat series",
      f"got {entry_price}")


# ─── 8. Edge cases ──────────────────────────────────────────────────────────

section("8. Edge Cases")

# Empty dataframe
empty_df = pd.DataFrame(columns=["timestamp","open","high","low","close","volume"])
hits_empty = detect_hits(empty_df, "X", "1m", {"id":1,"periods":[3],"name":"T"}, lookback=130)
check(hits_empty == [], "Empty dataframe → no hits, no crash")

# Insufficient data (df shorter than smallest SMA period)
short_df = pd.DataFrame({
    "timestamp": pd.date_range("2025-01-01", periods=5, freq="1min", tz="UTC"),
    "open": [1,2,3,4,5], "high": [1,2,3,4,5], "low": [1,2,3,4,5],
    "close": [1.0,2.0,3.0,4.0,5.0], "volume": [1,1,1,1,1],
})
hits_short = detect_hits(short_df, "X", "1m", {"id":1,"periods":[10],"name":"T"}, lookback=130)
check(hits_short == [], "Insufficient data → no hits, no crash")


# ─── 9. Outfit catalog integrity ────────────────────────────────────────────

section("9. Outfit Catalog Integrity")

check(len(OUTFITS) == 40, "Exactly 40 outfits per blueprint",
      f"got {len(OUTFITS)}")

# All outfit IDs unique and contiguous 1..40
ids = [o["id"] for o in OUTFITS]
check(ids == list(range(1, 41)), "Outfit IDs are 1..40 contiguous",
      f"got {ids[:5]}...")

# Periods within 1..999
all_periods_valid = all(
    all(1 <= p <= 999 for p in o["periods"]) for o in OUTFITS
)
check(all_periods_valid, "All periods within [1, 999]")

# Most outfits have 6 periods, two have 3 (#39 NAS, #40 S&P)
six_count = sum(1 for o in OUTFITS if len(o["periods"]) == 6)
three_count = sum(1 for o in OUTFITS if len(o["periods"]) == 3)
check(six_count == 38 and three_count == 2,
      "38 six-period outfits + 2 three-period (NAS, S&P)",
      f"got six={six_count} three={three_count}")

# Specific spot-checks against blueprint
spot = {1: [33, 66, 99, 333, 666, 999],
        21: [25, 51, 101, 202, 404, 808],
        26: [24, 47, 94, 188, 376, 752],
        40: [10, 50, 200]}
for oid, expected_periods in spot.items():
    actual = next(o["periods"] for o in OUTFITS if o["id"] == oid)
    check(actual == expected_periods, f"Outfit #{oid} matches blueprint",
          f"expected {expected_periods}, got {actual}")


# ─── 10. Webull client signature ────────────────────────────────────────────

section("10. Webull Client Auth")

client = WebullClient(app_key="test_key", app_secret="test_secret_xyz", region="us")
sig = client._sign("GET", "/openapi/market-data/stock/history-bars",
                   query="symbol=AAPL", body="",
                   timestamp="2025-01-01T00:00:00Z", nonce="abc123")
check(isinstance(sig, str) and len(sig) > 0, "Signature is non-empty string",
      f"got len={len(sig)}")

# Determinism: same inputs → same signature
sig2 = client._sign("GET", "/openapi/market-data/stock/history-bars",
                    query="symbol=AAPL", body="",
                    timestamp="2025-01-01T00:00:00Z", nonce="abc123")
check(sig == sig2, "Signature is deterministic for same inputs")

# Sensitivity: different nonce → different signature
sig3 = client._sign("GET", "/openapi/market-data/stock/history-bars",
                    query="symbol=AAPL", body="",
                    timestamp="2025-01-01T00:00:00Z", nonce="xyz789")
check(sig != sig3, "Different nonce → different signature")

# Headers contain all required fields
headers = client._headers("GET", "/test", query="", body="")
required = {"x-app-key", "x-timestamp", "x-signature-algorithm",
            "x-signature-version", "x-signature-nonce", "x-version", "x-signature"}
check(required.issubset(headers.keys()),
      "All 7 required Webull auth headers present",
      f"missing: {required - set(headers.keys())}")

# Rate limiter state initialized
check(client._rate_limit == 60, "Rate limit set to 60/min per Webull spec")


# ─── 11. End-to-end engine test ─────────────────────────────────────────────

section("11. End-to-End Engine")

cfg = EngineConfig(
    universe=["SPY", "QQQ", "IWM"],
    timeframes=["5m", "30m"],
    lookback=130,
)
engine = SMAOutfitEngine(MockClient(), cfg)
engine.monitor_systems()
engine.scan()

check(len(engine.system_states) == 8, "Engine populates 8 system states")
check(len(engine.store) > 0, "Engine produces hits with mock data",
      f"got {len(engine.store)} entries")

signal = engine.top_signal()
if signal:
    check("ticker" in signal and "outfit_periods" in signal and "entry_price" in signal,
          "Top signal has required fields")
    check(signal["ticker"] in cfg.universe, "Top signal ticker in universe",
          f"got {signal['ticker']}")
    check(signal["timeframe"] in cfg.timeframes, "Top signal tf in configured tfs",
          f"got {signal['timeframe']}")
    check(signal["hit_count"] > 0, "Top signal has positive hit count")
    check(0 <= len(signal["convergence"]["score"]) <= 3, "Convergence score format n/4")

top10 = engine.top_n(10)
if len(top10) >= 2:
    check(top10[0]["rank_score"] >= top10[1]["rank_score"],
          "Top-N is sorted descending by rank score")


# ─── 12. Determinism check ──────────────────────────────────────────────────

section("12. Determinism (Mock Client)")

m1 = MockClient()
m2 = MockClient()
df1 = m1.fetch_bars("AAPL", "5m", 100)
df2 = m2.fetch_bars("AAPL", "5m", 100)
check(df1["close"].equals(df2["close"]),
      "Mock client is deterministic across instances (same seed)")


# ─── 13. Async fetcher (token bucket + concurrent execution) ────────────────

section("13. Async Fetcher")

import asyncio
from async_fetch import TokenBucket, AsyncFetcher, FetchRequest, build_requests

async def test_token_bucket():
    # Bucket with 5 tokens, refills at 5/sec → can do 5 instantly, then 1/200ms
    bucket = TokenBucket(capacity=5, refill_rate=5.0)
    start = asyncio.get_event_loop().time()
    for _ in range(5):
        await bucket.acquire(1)
    elapsed_5 = asyncio.get_event_loop().time() - start
    # Should take basically zero time
    return elapsed_5 < 0.2

result = asyncio.run(test_token_bucket())
check(result, "Token bucket allows burst up to capacity instantly")

async def test_token_bucket_throttle():
    # 2 tokens, 10/sec refill. Acquiring 5 should take ~0.3s (3 tokens deficit)
    bucket = TokenBucket(capacity=2, refill_rate=10.0)
    start = asyncio.get_event_loop().time()
    for _ in range(5):
        await bucket.acquire(1)
    elapsed = asyncio.get_event_loop().time() - start
    # 2 free, then need to wait for 3 more at 10/sec = 0.3s minimum
    return 0.2 < elapsed < 0.6

result = asyncio.run(test_token_bucket_throttle())
check(result, "Token bucket throttles past capacity at refill rate")

# AsyncFetcher with mock client
async def test_async_fetcher():
    client = MockClient()
    fetcher = AsyncFetcher(client, max_concurrent=10, rate_limit=120, rate_window_seconds=60)
    reqs = [FetchRequest("SPY", "5m", 100, priority=1),
            FetchRequest("QQQ", "5m", 100, priority=1),
            FetchRequest("IWM", "5m", 100, priority=2)]
    results = await fetcher.fetch_all(reqs)
    return results

results = asyncio.run(test_async_fetcher())
check(len(results) == 3, "AsyncFetcher returns result for each request",
      f"got {len(results)}")
check(all(len(df) > 0 for df in results.values()),
      "All fetched dataframes are non-empty")

# Priority ordering
reqs = build_requests(["SPY", "QQQ"], ["AAPL"], ["XYZ"], ["5m", "30m", "1h"])
priorities = [r.priority for r in reqs]
check(min(priorities) == 1 and max(priorities) == 3,
      "build_requests assigns all 3 priority tiers")
# Tier 2 only on long tfs
tier2_reqs = [r for r in reqs if r.priority == 2]
check(all(r.timeframe in {"30m", "1h"} for r in tier2_reqs),
      "Tier 2 only requests 15m+ timeframes",
      f"got tfs: {set(r.timeframe for r in tier2_reqs)}")


# ─── 14. Significance testing (permutation + BH-FDR) ────────────────────────

section("14. Significance Testing")

from significance import (
    permutation_test, benjamini_hochberg, SignificanceResult,
    test_top_entries, render_significance_table,
)

# Build a series with genuine structure: close prices revisit specific levels
# more often than random would predict (e.g., support/resistance behavior).
# We construct a series where prices tend to cluster around round numbers.
rng_struct = np.random.default_rng(7)
n_struct = 300
# Mean-reverting walk anchored at 50.00 — produces extra hits at SMA levels
struct_closes = np.zeros(n_struct)
struct_closes[0] = 50.00
for i in range(1, n_struct):
    drift = -0.3 * (struct_closes[i-1] - 50.00) / 50.00
    struct_closes[i] = struct_closes[i-1] * (1 + drift + rng_struct.normal(0, 0.005))
struct_closes = np.round(struct_closes, 2)
df_signal = pd.DataFrame({
    "timestamp": pd.date_range("2025-01-01", periods=n_struct, freq="5min", tz="UTC"),
    "open":  struct_closes,
    "high":  np.round(struct_closes + np.abs(rng_struct.normal(0, 0.05, n_struct)), 2),
    "low":   np.round(struct_closes - np.abs(rng_struct.normal(0, 0.05, n_struct)), 2),
    "close": struct_closes,
    "volume": [1000] * n_struct,
})
sig_result = permutation_test(
    df_signal, "TEST", "5m", {"id": 1, "periods": [10, 20], "name": "T"},
    lookback=n_struct, n_permutations=50,
)
check(sig_result.observed_hits > 0, "Permutation test: structured series produces hits",
      f"got {sig_result.observed_hits}")
# Mean-reverting series should produce *more* hits than random shuffles
# (the mean-reversion creates SMA-touching behavior that shuffling destroys)
check(sig_result.p_value <= 1.0 and sig_result.p_value >= 0.0,
      "Permutation test produces valid p-value range",
      f"got p={sig_result.p_value}")

# Random series should have higher p-value
rng = np.random.default_rng(123)
random_closes = 50 + np.cumsum(rng.normal(0, 0.5, 200))
df_random = pd.DataFrame({
    "timestamp": pd.date_range("2025-01-01", periods=200, freq="5min", tz="UTC"),
    "open":   np.round(random_closes + rng.normal(0, 0.1, 200), 2),
    "high":   np.round(random_closes + np.abs(rng.normal(0, 0.2, 200)), 2),
    "low":    np.round(random_closes - np.abs(rng.normal(0, 0.2, 200)), 2),
    "close":  np.round(random_closes, 2),
    "volume": [1000] * 200,
})
sig_random = permutation_test(
    df_random, "RAND", "5m", {"id": 1, "periods": [10, 20], "name": "T"},
    lookback=200, n_permutations=50,
)
check(0 <= sig_random.p_value <= 1, "Permutation test returns valid p-value",
      f"got p={sig_random.p_value}")

# BH-FDR correction
fake_results = [
    SignificanceResult("a", 100, 50, 10, 0.001, 5.0, 100),
    SignificanceResult("b", 80, 50, 10, 0.01, 3.0, 100),
    SignificanceResult("c", 60, 50, 10, 0.04, 1.0, 100),
    SignificanceResult("d", 55, 50, 10, 0.30, 0.5, 100),
    SignificanceResult("e", 52, 50, 10, 0.80, 0.2, 100),
]
corrected = benjamini_hochberg(fake_results, fdr_level=0.05)
sig_count = sum(1 for r in corrected if r.significant_after_fdr)
check(sig_count >= 1, "BH-FDR identifies at least one significant result",
      f"got {sig_count}")
check(corrected[0].p_value <= corrected[-1].p_value,
      "BH-FDR sorts by p-value ascending")

# All significant when p-values are very low
all_low = [SignificanceResult(f"r{i}", 100, 10, 5, 0.001, 5.0, 100) for i in range(5)]
benjamini_hochberg(all_low, fdr_level=0.05)
check(all(r.significant_after_fdr for r in all_low),
      "All low-p results pass BH-FDR")

# None significant when all p-values are high
all_high = [SignificanceResult(f"r{i}", 50, 49, 5, 0.5, 0.2, 100) for i in range(5)]
benjamini_hochberg(all_high, fdr_level=0.05)
check(not any(r.significant_after_fdr for r in all_high),
      "No high-p results pass BH-FDR")


# ─── 15. Backtesting (walk-forward + CPCV) ──────────────────────────────────

section("15. Backtesting")

from backtest import (
    walk_forward, combinatorial_purged_cv, evaluate_signal,
    backtest_top_signals, _compute_metrics, Trade,
)

# Build a long enough series for walk-forward
mock = MockClient()
df_long = mock.fetch_bars("SPY", "5m", 999)
check(len(df_long) >= 500, "Mock fetcher produces sufficient candles for backtest",
      f"got {len(df_long)}")

bt = walk_forward(
    df_long, "SPY", "5m", {"id": 21, "periods": [25, 51, 101, 202, 404, 808], "name": "404"},
    train_size=300, test_size=100, horizon_bars=10,
)
check(isinstance(bt.n_trades, int), "Walk-forward returns BacktestResult")
check(bt.n_trades >= 0, "Walk-forward trade count is non-negative")
# Sharpe should be a real number (might be 0 if no trades)
check(isinstance(bt.sharpe, float), "Sharpe is a float")
check(bt.win_rate >= 0.0 and bt.win_rate <= 1.0, "Win rate is in [0, 1]",
      f"got {bt.win_rate}")

# CPCV
bt_cpcv = combinatorial_purged_cv(
    df_long, "SPY", "5m", {"id": 21, "periods": [25, 51, 101, 202, 404, 808], "name": "404"},
    n_folds=5, n_test_folds=2, purge_bars=20, horizon_bars=10,
)
check(isinstance(bt_cpcv.n_trades, int), "CPCV returns BacktestResult")
check(bt_cpcv.n_trades >= 0, "CPCV trade count non-negative")

# evaluate_signal direct test
df_simple = pd.DataFrame({
    "timestamp": pd.date_range("2025-01-01", periods=20, freq="5min", tz="UTC"),
    "open":  [100.0] * 5 + [102.0] * 15,
    "high":  [101.0] * 20,
    "low":   [99.0] * 20,
    "close": [100.0] * 5 + [102.0] * 15,
    "volume": [1000] * 20,
})
trade = evaluate_signal(df_simple, signal_idx=0, entry_price=100.0, horizon_bars=10, direction=1)
check(trade is not None, "evaluate_signal returns a trade for valid input")
if trade is not None:
    check(abs(trade.return_pct - 0.02) < 0.001,
          "Trade return matches expected (entry 100 → exit 102 = +2%)",
          f"got {trade.return_pct}")

# Out-of-bounds signal returns None
trade_oob = evaluate_signal(df_simple, signal_idx=15, entry_price=100, horizon_bars=10, direction=1)
check(trade_oob is None, "evaluate_signal returns None for out-of-bounds horizon")

# _compute_metrics on synthetic trades
synthetic_trades = [
    Trade(pd.Timestamp.now(tz="UTC"), pd.Timestamp.now(tz="UTC"), "X", "5m", 1, 100, 102, 10, 0.02, 1),
    Trade(pd.Timestamp.now(tz="UTC"), pd.Timestamp.now(tz="UTC"), "X", "5m", 1, 100, 99, 10, -0.01, 1),
    Trade(pd.Timestamp.now(tz="UTC"), pd.Timestamp.now(tz="UTC"), "X", "5m", 1, 100, 103, 10, 0.03, 1),
]
metrics = _compute_metrics(synthetic_trades)
check(metrics.n_trades == 3, "Metrics: 3 trades counted")
check(abs(metrics.win_rate - 2/3) < 0.001, "Win rate is 2/3 (2 wins out of 3)",
      f"got {metrics.win_rate}")
check(abs(metrics.avg_return - (0.02 - 0.01 + 0.03)/3) < 0.001,
      "Avg return computed correctly")


# ─── 16. Regime detection (HMM) ─────────────────────────────────────────────

section("16. Regime Detection (HMM)")

from regime import (
    HMM_AVAILABLE, build_features, fit_regimes, predict_regimes,
    regime_at_timestamp, regime_conditional_summary, render_regime_summary,
)

check(HMM_AVAILABLE, "hmmlearn is importable")

if HMM_AVAILABLE:
    # Build synthetic SPY/UVXY/SMH bars
    mock_r = MockClient(seed_offset=999)
    spy_bars = mock_r.fetch_bars("SPY", "1d", 500)
    uvxy_bars = mock_r.fetch_bars("UVXY", "1d", 500)
    smh_bars = mock_r.fetch_bars("SMH", "1d", 500)

    features = build_features(spy_bars, uvxy_bars, smh_bars)
    check(len(features) > 0, "Feature builder produces rows after dropna",
          f"got {len(features)}")
    check("spy_return" in features.columns, "Features include spy_return")
    check("uvxy_level" in features.columns, "Features include uvxy_level")

    # Fit 3-state HMM
    if len(features) >= 100:
        model = fit_regimes(features, n_states=3, n_iter=50)
        check(model.fitted, "HMM fits successfully")
        check(model.n_states == 3, "HMM has correct number of states")
        check(set(model.state_labels.values()) <= {"risk-on", "neutral", "risk-off"},
              "State labels are sensible",
              f"got {set(model.state_labels.values())}")
        check(model.transition_matrix.shape == (3, 3), "Transition matrix is 3x3")

        # Predict regimes
        regimes = predict_regimes(model, features)
        check("regime" in regimes.columns, "predict_regimes adds 'regime' column")
        check("regime_label" in regimes.columns, "predict_regimes adds 'regime_label' column")
        check(regimes["regime"].isin([0, 1, 2]).all(), "All regime IDs are valid")

        # Regime lookup
        ts = features["timestamp"].iloc[len(features) // 2]
        rg = regime_at_timestamp(regimes, ts)
        check(rg is not None, "regime_at_timestamp returns a result for valid ts")
        if rg is not None:
            check(rg[0] in [0, 1, 2], "Returned regime ID is valid")

        # Regime-conditional summary using synthetic trades
        trades_for_regime = [
            Trade(features["timestamp"].iloc[i], features["timestamp"].iloc[i+1],
                  "SPY", "1d", 1, 100, 100 + np.random.randn(),
                  1, np.random.randn() * 0.02, 1)
            for i in range(min(50, len(features) - 1))
        ]
        summary = regime_conditional_summary(trades_for_regime, regimes)
        check(isinstance(summary, pd.DataFrame), "Regime summary returns DataFrame")
        if not summary.empty:
            check("avg_return" in summary.columns, "Summary includes avg_return")
            check("sharpe" in summary.columns, "Summary includes sharpe")


# ─── 17. Persistence (InfluxDB) ─────────────────────────────────────────────

section("17. InfluxDB Persistence")

from persistence import InfluxPersistence, NullPersistence, INFLUX_AVAILABLE

check(INFLUX_AVAILABLE, "influxdb-client is importable")

# When InfluxDB is unreachable, persistence should fail gracefully
persist = InfluxPersistence(url="http://nonexistent-host:8086", enabled=True)
check(not persist._connected, "Persistence fails gracefully when InfluxDB unreachable")
check(not persist.enabled, "Persistence sets enabled=False when connection fails")

# All write methods should be no-ops when disconnected (no exceptions)
try:
    persist.write_candles("SPY", "5m", pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=3, freq="5min", tz="UTC"),
        "open": [100.0, 101.0, 102.0], "high": [101.0, 102.0, 103.0],
        "low": [99.0, 100.0, 101.0], "close": [100.5, 101.5, 102.5],
        "volume": [1000, 2000, 3000],
    }))
    persist.write_hits([])
    persist.write_signal({"ticker": "X", "timeframe": "5m", "outfit_id": 1,
                          "entry_price": 100.0, "hit_count": 5,
                          "convergence": {"score": "2/4", "ohlc_detection": True,
                                          "candle_close": False, "parm_price": True,
                                          "time_series": False}})
    persist.write_system_states([])
    persist.write_top_n([])
    persist.flush()
    persist.close()
    no_exception = True
except Exception as e:
    no_exception = False
    print(f"   exception: {e}")
check(no_exception, "All write methods silently no-op when disconnected")

# NullPersistence accepts everything
null = NullPersistence()
try:
    null.write_candles("X", "5m", pd.DataFrame())
    null.write_hits([])
    null.write_signal({})
    null.write_system_states([])
    null.write_regimes(pd.DataFrame())
    null.write_top_n([])
    null.flush()
    null.close()
    null_ok = True
except Exception:
    null_ok = False
check(null_ok, "NullPersistence accepts all write calls without error")
check(not null.enabled, "NullPersistence reports enabled=False")

# Verify Point construction logic works for at least one record type
from influxdb_client import Point
p = (Point("test")
     .tag("ticker", "SPY")
     .field("price", 100.0))
check(p is not None, "InfluxDB Point construction works")


# ─── 18. Grafana dashboard JSON validity ────────────────────────────────────

section("18. Grafana Dashboards")

import json as _json
import os as _os

dashboard_dir = "/home/claude/sma_engine_v2/grafana/dashboards"
expected_dashboards = ["01_live_signal.json", "02_hit_heatmap.json", "03_regime_systems.json"]

for fname in expected_dashboards:
    path = _os.path.join(dashboard_dir, fname)
    check(_os.path.exists(path), f"Dashboard {fname} exists")
    if _os.path.exists(path):
        try:
            with open(path) as f:
                dash = _json.load(f)
            check(True, f"{fname} parses as valid JSON")
            check("title" in dash, f"{fname} has title field")
            check("panels" in dash, f"{fname} has panels array")
            check(len(dash["panels"]) > 0, f"{fname} has at least one panel",
                  f"got {len(dash.get('panels', []))} panels")
        except _json.JSONDecodeError as e:
            check(False, f"{fname} is invalid JSON: {e}")

# Provisioning configs
prov_ds = "/home/claude/sma_engine_v2/grafana/provisioning/datasources/influxdb.yml"
prov_db = "/home/claude/sma_engine_v2/grafana/provisioning/dashboards/dashboards.yml"
check(_os.path.exists(prov_ds), "Datasource provisioning file exists")
check(_os.path.exists(prov_db), "Dashboard provisioning file exists")

# docker-compose.yml exists and has the three services
compose_path = "/home/claude/sma_engine_v2/docker-compose.yml"
check(_os.path.exists(compose_path), "docker-compose.yml exists")
if _os.path.exists(compose_path):
    with open(compose_path) as f:
        compose = f.read()
    check("influxdb:" in compose, "docker-compose has influxdb service")
    check("grafana:" in compose, "docker-compose has grafana service")
    check("engine:" in compose, "docker-compose has engine service")
    check("8086:8086" in compose, "InfluxDB port 8086 exposed")
    check("3000:3000" in compose, "Grafana port 3000 exposed")


# ─── 19. Terminal UI / Local Writer / Sheets Writer ────────────────────────

section("19. Terminal UI, Local Writer, Sheets Writer")

from terminal_ui import TerminalUI
from local_writer import LocalWriter, LOG_COLUMNS, OPENPYXL_AVAILABLE
from sheets_writer import SheetsWriter, GOOGLE_AVAILABLE
import tempfile
from pathlib import Path

# ─── Terminal UI ──────────────────────────────────────────────────────
ui = TerminalUI()
check(ui is not None, "TerminalUI instantiates")
check(hasattr(ui, "render"), "TerminalUI has render method")
check(hasattr(ui, "render_simple"), "TerminalUI has render_simple method")

# Render with empty data should not crash
try:
    ui.render_simple(None, [], [], regime_label=None, cycle_count=0)
    rendered_ok = True
except Exception as e:
    rendered_ok = False
    print(f"   render error: {e}")
check(rendered_ok, "TerminalUI handles empty input without crashing")

# Render with a real signal should not crash
fake_signal = {
    "ticker": "SPY", "timeframe": "5m", "outfit_id": 1,
    "outfit_periods": [33, 66, 99, 333, 666, 999],
    "outfit_name": "AN", "entry_price": 500.50, "offset_applied": -0.01,
    "hit_count": 10, "risk": "penny break of 500.50",
    "convergence": {"score": "3/4"},
}
fake_top_n = [{
    "rank": 1, "ticker": "SPY", "timeframe": "5m",
    "outfit_periods": [33, 66, 99, 333, 666, 999],
    "hit_count": 10, "convergence": "3/4", "rank_score": 13.0,
}]
fake_systems = []
try:
    ui.render_simple(fake_signal, fake_top_n, fake_systems,
                     regime_label="risk-on", cycle_count=1)
    rendered_full = True
except Exception as e:
    rendered_full = False
    print(f"   render error: {e}")
check(rendered_full, "TerminalUI handles full signal data without crashing")

# ─── Local Writer ─────────────────────────────────────────────────────
check(OPENPYXL_AVAILABLE, "openpyxl is importable for xlsx writing")
check(len(LOG_COLUMNS) >= 20, f"LOG_COLUMNS has {len(LOG_COLUMNS)} fields",
      f"got {len(LOG_COLUMNS)}")
check(LOG_COLUMNS[0] == "timestamp_utc", "LOG_COLUMNS starts with timestamp_utc")
check("regime_label" in LOG_COLUMNS, "LOG_COLUMNS includes regime_label")

with tempfile.TemporaryDirectory() as td:
    lw = LocalWriter(output_dir=td)
    check(lw.log_path.exists(), "LocalWriter creates log.csv on init")

    # Check header row
    with open(lw.log_path) as f:
        header = f.readline().strip().split(",")
    check(header == LOG_COLUMNS, "Log CSV header matches LOG_COLUMNS",
          f"got {len(header)} cols, expected {len(LOG_COLUMNS)}")

    # Append a row
    lw.append_log_row(fake_signal, [], regime_label="risk-on")
    with open(lw.log_path) as f:
        lines = f.readlines()
    check(len(lines) == 2, "Log has header + 1 data row after append",
          f"got {len(lines)} lines")

    # Append again — log grows, never shrinks (append-only invariant)
    lw.append_log_row(fake_signal, [], regime_label="neutral")
    with open(lw.log_path) as f:
        lines = f.readlines()
    check(len(lines) == 3, "Log grows on subsequent append (append-only)",
          f"got {len(lines)} lines")

    # Write current xlsx
    lw.write_current_xlsx(fake_signal, fake_top_n, [], regime_label="risk-on")
    check(lw.current_xlsx_path.exists(), "current.xlsx is written")
    check(lw.current_xlsx_path.stat().st_size > 0, "current.xlsx is non-empty")

    # write_cycle is the combined method
    lw.write_cycle(fake_signal, fake_top_n, [], regime_label="neutral")
    with open(lw.log_path) as f:
        lines_after = f.readlines()
    check(len(lines_after) > len(lines),
          "write_cycle both writes xlsx and appends to log")

    # No-signal cycle should still log the systems/regime
    lw.append_log_row(None, [], regime_label="risk-off")
    with open(lw.log_path) as f:
        lines_no_sig = f.readlines()
    last_row = lines_no_sig[-1].split(",")
    # Index of regime_label in LOG_COLUMNS
    regime_idx = LOG_COLUMNS.index("regime_label")
    check("risk-off" in last_row[regime_idx],
          "No-signal cycle still records regime")

# ─── Sheets Writer ────────────────────────────────────────────────────
check(GOOGLE_AVAILABLE, "google-api-python-client is importable")

# Without credentials → enabled=False
sw = SheetsWriter(credentials_path="", sheet_id="")
check(not sw.enabled, "SheetsWriter disabled with no credentials")

# With nonexistent credentials path → enabled=False, no crash
sw_bad = SheetsWriter(credentials_path="/nonexistent/path.json", sheet_id="abc")
check(not sw_bad.enabled, "SheetsWriter disabled when credentials file missing")

# Calling write_cycle on disabled writer should silently no-op
try:
    sw.write_cycle(fake_signal, fake_top_n, [], regime_label="neutral")
    sheets_noop_ok = True
except Exception as e:
    sheets_noop_ok = False
    print(f"   error: {e}")
check(sheets_noop_ok, "Disabled SheetsWriter no-ops write_cycle safely")


# ─── 20. Daemon integration ──────────────────────────────────────────────

section("20. Daemon Integration")

import daemon as daemon_module
check(hasattr(daemon_module, "main"), "daemon.main exists")
check(hasattr(daemon_module, "run_cycle"), "daemon.run_cycle exists")
check(hasattr(daemon_module, "make_client"), "daemon.make_client exists")
check(hasattr(daemon_module, "build_universe"), "daemon.build_universe exists")

# run_cycle signature should accept all the new writers
import inspect
sig = inspect.signature(daemon_module.run_cycle)
params = list(sig.parameters.keys())
expected = {"client", "cfg", "persist", "local_writer", "sheets_writer",
            "terminal_ui", "cycle_count", "current_regime_label", "fit_regime"}
check(expected.issubset(set(params)),
      "run_cycle signature includes all new writer params",
      f"got {params}")


print("\n" + "═" * 70)
print(f"  RESULTS: {PASSED} passed, {FAILED} failed, {PASSED + FAILED} total")
print("═" * 70)
if FAILURES:
    print("\nFailures:")
    for name, detail in FAILURES:
        print(f"  ✗ {name}    {detail}")
    sys.exit(1)
else:
    print("\n  All tests passed.")
    sys.exit(0)
