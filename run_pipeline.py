"""
run_pipeline.py — full pipeline orchestration.

  scan (engine + async fetch)
    → top N entries
        → backtest each entry (walk-forward or CPCV)
            → significance test against permutation null
                → fit regime model
                    → tag trades with regime
                        → render full report
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import asdict

import pandas as pd

from engine import (
    OUTFITS, MockClient, WebullClient, EngineConfig, SMAOutfitEngine,
    UNIVERSE_TIER_1, UNIVERSE_TIER_2, UNIVERSE_TIER_3,
    rank_entries, render_dashboard,
)
from async_fetch import AsyncFetcher, build_requests, FetchRequest
from backtest import backtest_top_signals
from significance import test_top_entries, render_significance_table
from regime import (
    HMM_AVAILABLE, build_features, fit_regimes, predict_regimes,
    regime_conditional_summary, render_regime_summary,
)


def make_client(source: str) -> object:
    if source == "webull":
        app_key = os.environ.get("WEBULL_APP_KEY")
        app_secret = os.environ.get("WEBULL_APP_SECRET")
        if not (app_key and app_secret):
            print("ERROR: WEBULL_APP_KEY and WEBULL_APP_SECRET required for --source webull",
                  file=sys.stderr)
            sys.exit(1)
        return WebullClient(app_key, app_secret, region=os.environ.get("WEBULL_REGION", "us"))
    return MockClient()


async def async_scan(client, cfg, t1, t2, t3) -> dict[tuple[str, str], pd.DataFrame]:
    """Use AsyncFetcher to populate the candle cache concurrently."""
    reqs = build_requests(t1, t2, t3, cfg.timeframes, cfg.candle_count)
    fetcher = AsyncFetcher(client, max_concurrent=20, rate_limit=60, rate_window_seconds=60)
    return await fetcher.fetch_all(reqs)


def main():
    p = argparse.ArgumentParser(description="SMA Outfit Engine — full pipeline")
    p.add_argument("--source", choices=["mock", "webull"], default="mock")
    p.add_argument("--universe", choices=["tier1", "tier2", "all"], default="tier1")
    p.add_argument("--timeframes", nargs="+", default=["5m", "30m", "1h", "1d"])
    p.add_argument("--lookback", type=int, default=130)
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--backtest", choices=["none", "walk_forward", "cpcv"], default="walk_forward")
    p.add_argument("--horizon", type=int, default=10, help="Forward bars for backtest evaluation")
    p.add_argument("--significance", action="store_true", help="Run permutation significance tests")
    p.add_argument("--n-permutations", type=int, default=100)
    p.add_argument("--regime", action="store_true", help="Fit HMM regime model and condition results")
    p.add_argument("--n-regimes", type=int, default=3)
    p.add_argument("--async-fetch", action="store_true", help="Use AsyncFetcher (recommended for large universes)")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    client = make_client(args.source)
    universe_map = {
        "tier1": (UNIVERSE_TIER_1, [], []),
        "tier2": (UNIVERSE_TIER_1, UNIVERSE_TIER_2, []),
        "all":   (UNIVERSE_TIER_1, UNIVERSE_TIER_2, UNIVERSE_TIER_3),
    }
    t1, t2, t3 = universe_map[args.universe]
    full_universe = t1 + t2 + t3

    cfg = EngineConfig(
        universe=full_universe,
        timeframes=args.timeframes,
        lookback=args.lookback,
    )

    # ── 1. Scan ────────────────────────────────────────────────────────────
    print(f"\n{'═' * 71}\n  STAGE 1: SCAN ({len(full_universe)} tickers × {len(args.timeframes)} tfs × 40 outfits)\n{'═' * 71}")
    t0 = time.monotonic()
    engine = SMAOutfitEngine(client, cfg)

    if args.async_fetch:
        print("  Fetching concurrently via AsyncFetcher...")
        cache = asyncio.run(async_scan(client, cfg, t1, t2, t3))
        engine.candle_cache = cache

    engine.monitor_systems()
    engine.scan()
    elapsed = time.monotonic() - t0
    print(f"  Scan complete: {len(engine.store)} active combos, {elapsed:.1f}s")

    # ── 2. Top signals ─────────────────────────────────────────────────────
    signal = engine.top_signal()
    top_n_list = engine.top_n(args.top_n)
    print()
    print(render_dashboard(signal, engine.system_states, top_n_list))

    # ── 3. Backtest ────────────────────────────────────────────────────────
    if args.backtest != "none":
        print(f"\n{'═' * 71}\n  STAGE 2: BACKTEST (method={args.backtest}, horizon={args.horizon})\n{'═' * 71}")
        ranked = rank_entries(engine.store, pd.Timestamp.now(tz="UTC"))[:args.top_n]
        top_entries = [e for e, _ in ranked]
        outfits_by_id = {o["id"]: o for o in OUTFITS}

        t0 = time.monotonic()
        bt_results = backtest_top_signals(
            engine.candle_cache, top_entries, outfits_by_id,
            method=args.backtest, horizon_bars=args.horizon,
        )
        elapsed = time.monotonic() - t0
        print(f"  Backtested {len(bt_results)} entries in {elapsed:.1f}s")
        print()
        for key, result in bt_results.items():
            if result.n_trades > 0:
                print(f"  {key}: trades={result.n_trades:>3} "
                      f"win={result.win_rate:>5.1%} "
                      f"avg={result.avg_return:>+7.3%} "
                      f"sharpe={result.sharpe:>5.2f} "
                      f"maxDD={result.max_drawdown:>6.2%}")

        all_trades = [t for r in bt_results.values() for t in r.trades]
    else:
        bt_results = {}
        all_trades = []

    # ── 4. Significance ────────────────────────────────────────────────────
    if args.significance:
        print(f"\n{'═' * 71}\n  STAGE 3: SIGNIFICANCE (permutations={args.n_permutations})\n{'═' * 71}")
        outfits_by_id = {o["id"]: o for o in OUTFITS}
        t0 = time.monotonic()
        sig_results = test_top_entries(
            engine.store.all(), engine.candle_cache, outfits_by_id,
            top_n=args.top_n, n_permutations=args.n_permutations,
            lookback=args.lookback,
        )
        elapsed = time.monotonic() - t0
        print(f"  Significance testing complete: {elapsed:.1f}s")
        print()
        print(render_significance_table(sig_results, top=args.top_n))

    # ── 5. Regime ──────────────────────────────────────────────────────────
    if args.regime and HMM_AVAILABLE:
        print(f"\n{'═' * 71}\n  STAGE 4: REGIME ({args.n_regimes} states)\n{'═' * 71}")
        # Pull the proxies needed for regime features
        spy = engine.candle_cache.get(("SPY", "1d"))
        uvxy = engine.candle_cache.get(("UVXY", "1d"))
        smh = engine.candle_cache.get(("SMH", "1d"))

        # If 1d not in cache, fetch directly
        if spy is None or len(spy) == 0:
            spy = client.fetch_bars("SPY", "1d", 999)
        if uvxy is None or len(uvxy) == 0:
            uvxy = client.fetch_bars("UVXY", "1d", 999)
        if smh is None or len(smh) == 0:
            smh = client.fetch_bars("SMH", "1d", 999)

        try:
            features = build_features(spy, uvxy, smh)
            print(f"  Built {len(features)} feature observations")
            if len(features) >= 50:
                model = fit_regimes(features, n_states=args.n_regimes, n_iter=100)
                print(f"  HMM fit complete. State labels: {model.state_labels}")
                regimes = predict_regimes(model, features)

                # Distribution of regimes over history
                print("\n  Regime distribution over history:")
                dist = regimes["regime_label"].value_counts(normalize=True)
                for label, frac in dist.items():
                    print(f"    {label:<12} {frac:>6.1%}")

                # If we have backtest trades, condition them on regime
                if all_trades:
                    summary = regime_conditional_summary(all_trades, regimes)
                    print()
                    print(render_regime_summary(summary))
            else:
                print("  Insufficient data for HMM fit (need ≥50 observations)")
        except Exception as e:
            print(f"  Regime detection failed: {e}")

    elif args.regime and not HMM_AVAILABLE:
        print("\n  Regime requested but hmmlearn not installed. pip install hmmlearn")

    print(f"\n{'═' * 71}\n  PIPELINE COMPLETE\n{'═' * 71}\n")


if __name__ == "__main__":
    main()
