"""
FlashAlpha GEX — single-stock gamma exposure for top triangulated tickers.
Free tier: 5 requests/day, individual stocks only (no ETFs/indexes).
Results cached to disk once per trading day to preserve the daily budget.
"""

import json
import os
import time
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta
from pathlib import Path

BASE_URL   = "https://lab.flashalpha.com/v1/exposure/gex"
CACHE_FILE = Path(__file__).parent / ".gex_cache.json"
MAX_TICKERS = 5   # free tier daily budget


def _api_key() -> str:
    return os.environ.get("FLASHALPHA_API_KEY", "")


def _nearest_expiry() -> str:
    """Return nearest Friday (or today if Friday) as yyyy-MM-dd."""
    today = date.today()
    days_until_friday = (4 - today.weekday()) % 7
    if days_until_friday == 0 and today.weekday() == 4:
        return today.isoformat()
    friday = today + timedelta(days=days_until_friday if days_until_friday else 7)
    return friday.isoformat()


def _fetch_one(ticker: str, expiry: str, api_key: str) -> dict:
    url = f"{BASE_URL}/{ticker.upper()}?expiration={expiry}"
    req = urllib.request.Request(url, headers={"X-Api-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return {
                "ticker":        ticker.upper(),
                "gamma_flip":    data.get("gamma_flip"),
                "net_gex":       data.get("net_gex"),
                "regime":        data.get("net_gex_label", ""),  # "positive" / "negative"
                "spot":          data.get("underlying_price"),
                "as_of":         data.get("as_of", ""),
                "error":         None,
            }
    except urllib.error.HTTPError as e:
        return {"ticker": ticker.upper(), "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ticker": ticker.upper(), "error": str(e)}


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_cache(data: dict):
    try:
        CACHE_FILE.write_text(json.dumps(data))
    except Exception:
        pass


def fetch_gex_for_tickers(tickers: list[str]) -> dict[str, dict]:
    """
    Fetch GEX for up to MAX_TICKERS tickers.
    Results cached for the trading day — won't re-fetch if already have today's data.
    Returns dict keyed by ticker symbol.
    """
    api_key = _api_key()
    if not api_key:
        return {}   # no key → skip silently, overlay just omits the column

    tickers = [t.upper() for t in tickers[:MAX_TICKERS]]
    today   = date.today().isoformat()
    expiry  = _nearest_expiry()

    cache   = _load_cache()
    results = {}
    to_fetch = []

    for t in tickers:
        if t in cache and cache[t].get("cache_date") == today:
            results[t] = cache[t]
        else:
            to_fetch.append(t)

    for i, t in enumerate(to_fetch):
        r = _fetch_one(t, expiry, api_key)
        r["cache_date"] = today
        cache[t] = r
        results[t] = r
        if i < len(to_fetch) - 1:
            time.sleep(1.2)   # gentle pacing

    _save_cache(cache)
    return results


def regime_badge(gex_data: dict | None) -> str:
    """Return short colored markup for the signals table."""
    if not gex_data or gex_data.get("error") or not gex_data.get("regime"):
        return "[dim]—[/dim]"
    if gex_data["regime"] == "positive":
        return "[green]+γ[/green]"
    return "[red]-γ[/red]"


if __name__ == "__main__":
    import sys
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["HRL", "PFE", "TLT", "F", "WMT"]
    results = fetch_gex_for_tickers(tickers)
    for t, r in results.items():
        if r.get("error"):
            print(f"  {t}: ERROR — {r['error']}")
        else:
            flip = r.get("gamma_flip") or "—"
            reg  = r.get("regime", "—")
            spot = r.get("spot") or "—"
            print(f"  {t}: spot={spot}  gamma_flip={flip}  regime={reg}")
