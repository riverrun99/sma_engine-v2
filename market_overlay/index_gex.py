"""
Index GEX — Zero gamma for major ETF indexes.
Computes GEX from yfinance options chains for QQQ, IWM, DIA.
Cached to disk once per trading day to avoid rate limiting.

SPX zero gamma comes from Tikitrade (gamma_engine.py).
This module covers: QQQ (Nasdaq), IWM (Russell 2000), DIA (Dow).
"""

import json
import time
import warnings
import math
from datetime import date, datetime, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

TICKERS    = ["QQQ", "IWM", "DIA"]
CACHE_FILE = Path(__file__).parent / ".index_gex_cache.json"
MAX_EXPIRIES = 4     # limit yfinance calls per ticker
EXPIRY_DELAY = 2.0   # seconds between expiry fetches


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_cache(data: dict):
    try:
        CACHE_FILE.write_text(json.dumps(data, default=str))
    except Exception:
        pass


def _bs_gamma(S, K, T, r, sigma) -> float:
    """Black-Scholes gamma."""
    if T <= 0 or sigma <= 0:
        return 0.0
    try:
        from math import log, sqrt, exp
        from scipy.stats import norm
        d1 = (log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt(T))
        return norm.pdf(d1) / (S * sigma * sqrt(T))
    except Exception:
        return 0.0


def _compute_gex(ticker: str, spot: float) -> dict:
    """Compute zero gamma for one ticker from yfinance options chain."""
    try:
        import yfinance as yf
        import pandas as pd
        import numpy as np

        t = yf.Ticker(ticker)
        exps = t.options[:MAX_EXPIRIES]
        if not exps:
            return {"error": f"No options for {ticker}"}

        r = 0.05
        today = date.today()
        strikes_gex: dict[float, float] = {}

        for exp in exps:
            exp_date = date.fromisoformat(exp)
            T = max((exp_date - today).days / 365, 1/365)
            try:
                chain = t.option_chain(exp)
            except Exception:
                continue

            for opt_type, df in [("call", chain.calls), ("put", chain.puts)]:
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    K   = float(row.get("strike", 0))
                    oi  = float(row.get("openInterest", 0) or 0)
                    iv  = float(row.get("impliedVolatility", 0) or 0)
                    if K <= 0 or oi <= 0 or iv <= 0:
                        continue
                    g = _bs_gamma(spot, K, T, r, iv)
                    # Dealers are short calls, long puts (standard assumption)
                    gex = g * oi * 100 * spot * spot * 0.01
                    if opt_type == "call":
                        strikes_gex[K] = strikes_gex.get(K, 0) + gex
                    else:
                        strikes_gex[K] = strikes_gex.get(K, 0) - gex

            time.sleep(EXPIRY_DELAY)

        if not strikes_gex:
            return {"error": f"No GEX data computed for {ticker}"}

        # Sort strikes high → low, find zero crossing
        sorted_strikes = sorted(strikes_gex.keys(), reverse=True)
        cumulative = 0.0
        zero_gamma = None
        prev_k, prev_cum = None, 0.0

        for k in sorted_strikes:
            cumulative += strikes_gex[k]
            if prev_k is not None and prev_cum * cumulative < 0:
                # Linear interpolation
                zero_gamma = prev_k + (k - prev_k) * abs(prev_cum) / (abs(prev_cum) + abs(cumulative))
                break
            prev_k, prev_cum = k, cumulative

        if zero_gamma is None:
            # Fallback: weighted average
            total_abs = sum(abs(v) for v in strikes_gex.values())
            zero_gamma = (sum(k * abs(v) for k, v in strikes_gex.items()) / total_abs
                          if total_abs else spot)

        net_gex   = sum(strikes_gex.values())
        regime    = "ABOVE ZERO GAMMA" if spot > zero_gamma else "BELOW ZERO GAMMA"

        # Call/put walls
        call_wall = max((k for k, v in strikes_gex.items() if v > 0 and k > spot), default=None)
        put_wall  = min((k for k, v in strikes_gex.items() if v < 0 and k < spot), default=None)
        dist_pct  = (spot - zero_gamma) / zero_gamma * 100

        return {
            "ticker":       ticker,
            "spot":         round(spot, 2),
            "zero_gamma":   round(zero_gamma, 2),
            "dist_pct":     round(dist_pct, 3),
            "regime":       regime,
            "net_gex":      round(net_gex, 0),
            "call_wall":    round(call_wall, 2) if call_wall else None,
            "put_wall":     round(put_wall, 2) if put_wall else None,
            "exps_used":    len(exps),
            "error":        None,
        }

    except Exception as e:
        return {"ticker": ticker, "error": str(e)}


def fetch_all(spot_hints: dict = None) -> dict[str, dict]:
    """
    Fetch zero gamma for all index ETFs.
    spot_hints: dict of {ticker: price} to skip yfinance spot fetch.
    Results cached for the trading day.
    """
    spot_hints = spot_hints or {}
    today      = date.today().isoformat()
    cache      = _load_cache()
    results    = {}

    for ticker in TICKERS:
        cached = cache.get(ticker, {})
        if cached.get("cache_date") == today and not cached.get("error"):
            results[ticker] = cached
            continue

        # Get spot price
        spot = spot_hints.get(ticker)
        if not spot:
            try:
                import yfinance as yf
                info = yf.Ticker(ticker).fast_info
                spot = float(info.get("last_price") or info.get("regularMarketPrice") or 0)
            except Exception:
                spot = 0

        if not spot:
            results[ticker] = {"ticker": ticker, "error": "No spot price"}
            continue

        r = _compute_gex(ticker, spot)
        r["cache_date"] = today
        r["timestamp"]  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        cache[ticker]   = r
        results[ticker] = r
        time.sleep(3)  # pace between tickers

    _save_cache(cache)
    return results


if __name__ == "__main__":
    print("Fetching index GEX (QQQ, IWM, DIA)... this takes ~2 min first run, cached after.")
    results = fetch_all()
    for ticker, r in results.items():
        if r.get("error"):
            print(f"  {ticker}: ERROR — {r['error']}")
        else:
            print(f"  {ticker}: spot={r['spot']}  zero_gamma={r['zero_gamma']}  "
                  f"regime={r['regime']}  dist={r['dist_pct']:+.2f}%")
