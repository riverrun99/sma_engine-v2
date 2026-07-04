"""
Zero Gamma Engine — Tikitrade free data source
Scrapes tikitrade.com/gamma (free, no login, updated 9:30 AM ET daily).
Falls back to cached result if unavailable.
"""

import re
import time
import urllib.request
from datetime import datetime, timezone
import warnings
warnings.filterwarnings("ignore")

URL = "https://tikitrade.com/gamma"

# Module-level cache
_last_good: dict = {}


def _fetch_page(retries: int = 3) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36"
    }
    for attempt in range(retries):
        try:
            req = urllib.request.Request(URL, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8")
        except Exception:
            if attempt < retries - 1:
                time.sleep(3)
    return ""


def _parse(html: str) -> dict:
    """Extract key=value pairs from the pre block."""
    block = re.search(r'<pre[^>]*>(.*?)</pre>', html, re.DOTALL)
    if not block:
        return {}
    text = block.group(1)
    # Strip HTML tags
    text = re.sub(r'<[^>]+>', '', text)

    data = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or ':' not in line:
            continue
        key, _, val = line.partition(':')
        key = key.strip()
        val = val.strip().rstrip(',')
        data[key] = val

    return data


def _float(s: str) -> float:
    try:
        return float(s.split(':')[0].strip())
    except Exception:
        return 0.0


def fetch_gex(ticker: str = "SPX", spot_hint: float = None) -> dict:
    global _last_good

    html = _fetch_page()
    if not html:
        if _last_good:
            stale = dict(_last_good)
            stale["stale"] = True
            stale["stale_note"] = "tikitrade.com unreachable — showing last known values"
            return stale
        return {"error": "Could not fetch Tikitrade gamma data"}

    raw = _parse(html)
    if not raw:
        if _last_good:
            stale = dict(_last_good)
            stale["stale"] = True
            stale["stale_note"] = "Parse failed — showing last known values"
            return stale
        return {"error": "Could not parse gamma data"}

    zero_gamma      = _float(raw.get("Zero Gamma", "0"))
    spot_str        = raw.get("Underlying", "0")
    # Underlying format: "7135.71 @ 2026-04-24 13:30Z" — grab first number only
    spot_num = re.search(r'([\d]+\.[\d]+)', spot_str)
    spot            = float(spot_num.group(1)) if spot_num else 0.0
    basis_shift     = _float(raw.get("Basis Shift", "0"))  # SPX→ES conversion
    max_pain        = _float(raw.get("Max Pain", "0"))
    exp_upper       = _float(raw.get("Expected Move Upper", "0"))
    exp_lower       = _float(raw.get("Expected Move Lower", "0"))
    key_strike      = _float(raw.get("Key Gamma Strike", "0"))
    vanna_inflection= _float(raw.get("Vanna Inflection", "0"))

    # Extract top call/put walls (first entry = highest strength)
    def top_wall(key: str) -> float:
        s = raw.get(key, "")
        if s:
            return _float(s.split(',')[0])
        return 0.0

    call_wall = top_wall("Call Walls")
    put_wall  = top_wall("Put Walls")

    # Always prefer live SPY price when available (Tikitrade spot is daily snapshot, stale intraday)
    if spot_hint:
        spot = round(spot_hint * 10, 2)  # SPY ≈ SPX / 10 — live Webull price
    elif spot <= 0:
        pass  # no data available

    dist_pct = (spot - zero_gamma) / zero_gamma * 100 if (zero_gamma and spot) else 0

    if spot > zero_gamma:
        regime      = "ABOVE ZERO GAMMA"
        regime_note = "Stabilizing — dealers buy dips, sell rallies"
    else:
        regime      = "BELOW ZERO GAMMA"
        regime_note = "Amplifying — dealers sell dips, buy rallies (vol ↑)"

    # Parse timestamp from Underlying field e.g. "7135.71 @ 2026-04-24 13:30Z"
    ts_match = re.search(r'@\s*([\d\-]+ [\d:]+Z?)', spot_str)
    data_ts  = ts_match.group(1) if ts_match else "—"

    result = {
        "ticker":             "SPX",
        "spot":               round(spot, 2),
        "zero_gamma":         round(zero_gamma, 2),
        "dist_from_zero_pct": round(dist_pct, 3),
        "regime":             regime,
        "regime_note":        regime_note,
        "call_wall":          call_wall,
        "put_wall":           put_wall,
        "max_pain":           max_pain,
        "key_strike":         key_strike,
        "vanna_inflection":   vanna_inflection,
        "exp_move_upper":     exp_upper,
        "exp_move_lower":     exp_lower,
        "basis_shift":        basis_shift,
        "stale":              False,
        "stale_note":         "",
        "data_as_of":         data_ts,
        "expirations_used":   "n/a",
        "strikes_analyzed":   "n/a",
        "timestamp":          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    _last_good = dict(result)
    return result


if __name__ == "__main__":
    r = fetch_gex()
    for k, v in r.items():
        print(f"  {k:<25}: {v}")
