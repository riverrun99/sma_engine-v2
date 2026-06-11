"""
Systems Panel — All-Markets State Display
==========================================
Fetches and displays the current state of all TraderBJones index systems:

  SPX    30m  [10/50/200]      POSITIVE = MA10 > MA50
  IXIC   30m  [20/100/250]     POSITIVE = MA20 > MA100
  DJI    15m  [90/300]         POSITIVE = MA90 > MA300   (active ops)
  DJI     1H  [90/300/900]     POSITIVE = MA90 > MA300   (structural)
  IWM     2H  [16/250/500]     POSITIVE = MA16 > MA250
  IWV     2H  [16/250/500]     POSITIVE = MA16 > MA250
  SOX    30m  [16/256/512]     POSITIVE = MA16 > MA256
  VIX     1H  [26/422]         MA26 > MA422  →  HIGH-VOL regime active
  SVIX    1D  [116/211/422]    cluster ~20   →  structural support

Under HIGH-VOL (rising VIX), the primary signal shifts from MA cross
to candle close vs key MA (MA50 / MA100 / MA300). This is flagged here.

Data source: Webull (same client as engine) with yfinance fallback.
IWM/IWV use Webull's native 2H interval (no resampling needed).
ETF proxies: QQQ=IXIC, DIA=DJI, SMH=SOX.
Results cached 5 minutes.
"""

import os
import sys
import time
import pandas as pd
import yfinance as yf
from datetime import datetime, timezone

# Import Webull client from parent engine (same credentials as engine)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from engine import WebullClient, MockClient

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box


# ─── Cache ────────────────────────────────────────────────────────────────────

_CACHE: dict = {}
CACHE_TTL   = 60    # seconds — refresh every overlay cycle for live prices


def _check_cache(key: str):
    entry = _CACHE.get(key)
    if entry and (time.time() - entry[0]) < CACHE_TTL:
        return entry[1]
    return None


def _set_cache(key: str, val):
    _CACHE[key] = (time.time(), val)


# ─── Webull client ────────────────────────────────────────────────────────────

_CLIENT = None

def _get_client():
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    app_key    = os.environ.get("WEBULL_APP_KEY", "")
    app_secret = os.environ.get("WEBULL_APP_SECRET", "")
    region     = os.environ.get("WEBULL_REGION", "us")
    if app_key and app_secret:
        _CLIENT = WebullClient(app_key, app_secret, region=region)
    else:
        _CLIENT = MockClient()
    return _CLIENT


# ─── Data helpers ─────────────────────────────────────────────────────────────

def _fetch_wb(ticker: str, interval: str, bars_needed: int) -> pd.DataFrame:
    """Fetch via Webull. Returns [timestamp, close] DataFrame or empty."""
    try:
        client = _get_client()
        count  = min(bars_needed + 20, 999)
        df     = client.fetch_bars(ticker, interval, count)
        if df is None or df.empty:
            return pd.DataFrame()
        df.columns = [str(c).lower() for c in df.columns]
        return df[["timestamp", "close"]].dropna()
    except Exception:
        return pd.DataFrame()


def _fetch_yf(ticker: str, interval: str, bars_needed: int) -> pd.DataFrame:
    """Fallback: yfinance download. Returns [timestamp, close] DataFrame or empty."""
    bars_per_day  = {"30m": 13, "15m": 26, "1h": 6.5, "2h": 3.25, "1d": 1}.get(interval, 10)
    trading_days  = max(10, int(bars_needed / bars_per_day) + 5)
    calendar_days = int(trading_days * 1.5) + 5
    # yfinance intraday caps
    if interval in ("30m", "15m", "5m", "2m"):
        calendar_days = min(calendar_days, 58)
    elif interval in ("1h", "2h"):
        calendar_days = min(calendar_days, 720)
    try:
        df = yf.download(
            ticker,
            period=f"{calendar_days}d",
            interval=interval,
            progress=False,
            auto_adjust=True,
            threads=False,
        )
        if df is None or df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        df = df.reset_index()
        df.columns = [str(c).lower() for c in df.columns]
        for col in ("datetime", "date", "timestamp", "index"):
            if col in df.columns:
                df = df.rename(columns={col: "timestamp"})
                break
        if "timestamp" not in df.columns:
            df = df.rename(columns={df.columns[0]: "timestamp"})
        return df[["timestamp", "close"]].dropna()
    except Exception:
        return pd.DataFrame()


def _resample_2h(df: pd.DataFrame) -> pd.DataFrame:
    """Resample 1h bars → 2h bars (take the last close in each 2h bin).
    Only used by yfinance fallback path — Webull provides 2H natively."""
    if df.empty:
        return df
    try:
        d = df.copy()
        d["timestamp"] = pd.to_datetime(d["timestamp"])
        d = d.set_index("timestamp")
        out = d["close"].resample("2h").last().dropna().reset_index()
        out.columns = ["timestamp", "close"]
        return out
    except Exception:
        return df


def _fetch(ticker_wb: str, ticker_yf: str, interval: str, bars_needed: int) -> pd.DataFrame:
    """Try Webull ticker first; fall back to yfinance ticker."""
    df = _fetch_wb(ticker_wb, interval, bars_needed)
    if not df.empty:
        return df
    return _fetch_yf(ticker_yf, interval, bars_needed)


def _sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()


def _last(series: pd.Series):
    """Return last value or None if NaN."""
    v = series.iloc[-1]
    return None if pd.isna(v) else float(v)


# ─── Per-system fetchers ──────────────────────────────────────────────────────

def _system_spx() -> dict:
    """SPX 30m [10/50/200]  POSITIVE = MA10 > MA50. Proxy: SPY."""
    key = "spx_30m"
    if (hit := _check_cache(key)):
        return hit
    df = _fetch("SPY", "SPY", "30m", 210)
    if df.empty:
        r = {"label": "SPX", "tf": "30m", "outfit": "10/50/200",
             "pos_ma": "MA10", "neg_ma": "MA50", "state": None, "error": "no data",
             "vehicle_pos": "UPRO", "vehicle_neg": "SPXU"}
        _set_cache(key, r); return r
    c = df["close"]
    ma10  = _last(_sma(c, 10))
    ma50  = _last(_sma(c, 50))
    ma200 = _last(_sma(c, 200))
    close = float(c.iloc[-1])
    if ma10 is None or ma50 is None:
        r = {"label": "SPX", "tf": "30m", "outfit": "10/50/200",
             "state": None, "error": "insufficient bars",
             "vehicle_pos": "UPRO", "vehicle_neg": "SPXU"}
        _set_cache(key, r); return r
    r = {
        "label": "SPX", "tf": "30m", "outfit": "10/50/200",
        "proxy": "SPY",
        "pos_ma": "MA10", "neg_ma": "MA50",
        "major": "MA200", "major_val": ma200,
        "ma_a": ma10, "ma_b": ma50, "close": close,
        "state": "POSITIVE" if ma10 > ma50 else "NEGATIVE",
        "spread_pct": (ma10 - ma50) / ma50 * 100,
        "vehicle_pos": "UPRO", "vehicle_neg": "SPXU",
    }
    _set_cache(key, r); return r


def _system_ixic() -> dict:
    """IXIC 20m [20/100/250]  POSITIVE = MA20 > MA100. Proxy: QQQ.
    Tries Webull QQQ 20m → Webull QQQ 30m → yfinance ^IXIC 30m → yfinance QQQ 30m.
    During underperformance (NEGATIVE), label shows 30m (active close-rule reference)."""
    key = "ixic_20m"
    if (hit := _check_cache(key)):
        return hit
    using_20m = False
    source    = "yf-30m"                         # updated as we find data
    df = _fetch_wb("QQQ", "20m", 270)            # Webull M20 — best resolution
    if not df.empty:
        using_20m = True
        source    = "WB-20m"
    else:
        df = _fetch_wb("QQQ", "30m", 270)        # Webull M30 — reliable fallback
        if not df.empty:
            source = "WB-30m"
    if df.empty:
        df = _fetch_yf("^IXIC", "30m", 260)     # yfinance index
        if not df.empty:
            source = "yf-30m"
    if df.empty:
        df = _fetch_yf("QQQ", "30m", 260)       # yfinance ETF last resort
        # source remains "yf-30m"
    if df.empty:
        r = {"label": "IXIC", "tf": "20m", "outfit": "20/100/250",
             "state": None, "error": "no data",
             "vehicle_pos": "TQQQ", "vehicle_neg": "SQQQ"}
        _set_cache(key, r); return r
    c = df["close"]
    ma20  = _last(_sma(c, 20))
    ma100 = _last(_sma(c, 100))
    ma250 = _last(_sma(c, 250))
    close = float(c.iloc[-1])
    if ma20 is None or ma100 is None:
        r = {"label": "IXIC", "tf": "20m", "outfit": "20/100/250",
             "state": None, "error": "insufficient bars",
             "vehicle_pos": "TQQQ", "vehicle_neg": "SQQQ"}
        _set_cache(key, r); return r
    state = "POSITIVE" if ma20 > ma100 else "NEGATIVE"
    # 20m when POSITIVE and Webull 20m available; 30m during NEGATIVE (close-rule active)
    tf_label = "20m" if (using_20m and state == "POSITIVE") else "30m"
    r = {
        "label": "IXIC", "tf": tf_label, "outfit": "20/100/250",
        "source": source,                        # WB-20m / WB-30m / yf-30m
        "proxy": "QQQ",
        "pos_ma": "MA20", "neg_ma": "MA100",
        "major": "MA250", "major_val": ma250,
        "ma_a": ma20, "ma_b": ma100, "close": close,
        "state": state,
        "spread_pct": (ma20 - ma100) / ma100 * 100,
        "vehicle_pos": "TQQQ", "vehicle_neg": "SQQQ",
    }
    _set_cache(key, r); return r


def _system_dji_15m() -> dict:
    """DJI 15m [90/300]  active operations timeframe. Proxy: DIA."""
    key = "dji_15m"
    if (hit := _check_cache(key)):
        return hit
    df = _fetch("DIA", "^DJI", "15m", 320)
    if df.empty:
        r = {"label": "DJI", "tf": "15m", "outfit": "90/300",
             "state": None, "error": "no data",
             "vehicle_pos": "UDOW", "vehicle_neg": "SDOW"}
        _set_cache(key, r); return r
    c = df["close"]
    ma90  = _last(_sma(c, 90))
    ma300 = _last(_sma(c, 300))
    close = float(c.iloc[-1])
    if ma90 is None or ma300 is None:
        r = {"label": "DJI", "tf": "15m", "outfit": "90/300",
             "state": None, "error": "insufficient bars",
             "vehicle_pos": "UDOW", "vehicle_neg": "SDOW"}
        _set_cache(key, r); return r
    r = {
        "label": "DJI", "tf": "15m", "outfit": "90/300",
        "proxy": "DIA",
        "pos_ma": "MA90", "neg_ma": "MA300",
        "ma_a": ma90, "ma_b": ma300, "close": close,
        "state": "POSITIVE" if ma90 > ma300 else "NEGATIVE",
        "spread_pct": (ma90 - ma300) / ma300 * 100,
        "vehicle_pos": "UDOW", "vehicle_neg": "SDOW",
    }
    _set_cache(key, r); return r


def _system_dji_1h() -> dict:
    """DJI 1H [90/300/900]  structural confirmation timeframe. Proxy: DIA."""
    key = "dji_1h"
    if (hit := _check_cache(key)):
        return hit
    df = _fetch("DIA", "^DJI", "1h", 920)
    if df.empty:
        r = {"label": "DJI", "tf": "1H", "outfit": "90/300/900",
             "state": None, "error": "no data",
             "vehicle_pos": "UDOW", "vehicle_neg": "SDOW"}
        _set_cache(key, r); return r
    c = df["close"]
    ma90  = _last(_sma(c, 90))
    ma300 = _last(_sma(c, 300))
    ma900 = _last(_sma(c, 900))
    close = float(c.iloc[-1])
    if ma90 is None or ma300 is None:
        r = {"label": "DJI", "tf": "1H", "outfit": "90/300/900",
             "state": None, "error": "insufficient bars",
             "vehicle_pos": "UDOW", "vehicle_neg": "SDOW"}
        _set_cache(key, r); return r
    r = {
        "label": "DJI", "tf": "1H", "outfit": "90/300/900",
        "proxy": "DIA",
        "pos_ma": "MA90", "neg_ma": "MA300",
        "major": "MA900", "major_val": ma900,
        "ma_a": ma90, "ma_b": ma300, "close": close,
        "state": "POSITIVE" if ma90 > ma300 else "NEGATIVE",
        "spread_pct": (ma90 - ma300) / ma300 * 100,
        "vehicle_pos": "UDOW", "vehicle_neg": "SDOW",
    }
    _set_cache(key, r); return r


def _system_iwm() -> dict:
    """IWM 2H [16/250/500]  POSITIVE = MA16 > MA250.
    Webull: native 2H bars. yfinance fallback: 1H → resample 2H."""
    key = "iwm_2h"
    if (hit := _check_cache(key)):
        return hit
    # Webull supports "2h" natively (M120) — no resampling needed
    df = _fetch_wb("IWM", "2h", 520)
    if df.empty:
        df1h = _fetch_yf("IWM", "1h", 520)
        df   = _resample_2h(df1h) if not df1h.empty else pd.DataFrame()
    if df.empty:
        r = {"label": "IWM", "tf": "2H", "outfit": "16/250/500",
             "state": None, "error": "no data",
             "vehicle_pos": "IWM", "vehicle_neg": "RWM"}
        _set_cache(key, r); return r
    c   = df["close"]
    ma16  = _last(_sma(c, 16))
    ma250 = _last(_sma(c, 250))
    close = float(c.iloc[-1])
    if ma16 is None or ma250 is None:
        r = {"label": "IWM", "tf": "2H", "outfit": "16/250/500",
             "state": None, "error": "insufficient bars",
             "vehicle_pos": "IWM", "vehicle_neg": "RWM"}
        _set_cache(key, r); return r
    r = {
        "label": "IWM", "tf": "2H", "outfit": "16/250/500",
        "proxy": "IWM",
        "pos_ma": "MA16", "neg_ma": "MA250",
        "ma_a": ma16, "ma_b": ma250, "close": close,
        "state": "POSITIVE" if ma16 > ma250 else "NEGATIVE",
        "spread_pct": (ma16 - ma250) / ma250 * 100,
        "vehicle_pos": "IWM", "vehicle_neg": "RWM",
    }
    _set_cache(key, r); return r


def _system_iwv() -> dict:
    """IWV 2H [16/250/500]  POSITIVE = MA16 > MA250.
    Webull: native 2H bars. yfinance fallback: 1H → resample 2H."""
    key = "iwv_2h"
    if (hit := _check_cache(key)):
        return hit
    df = _fetch_wb("IWV", "2h", 520)
    if df.empty:
        df1h = _fetch_yf("IWV", "1h", 520)
        df   = _resample_2h(df1h) if not df1h.empty else pd.DataFrame()
    if df.empty:
        r = {"label": "IWV", "tf": "2H", "outfit": "16/250/500",
             "state": None, "error": "no data",
             "vehicle_pos": "IWV", "vehicle_neg": "—"}
        _set_cache(key, r); return r
    c   = df["close"]
    ma16  = _last(_sma(c, 16))
    ma250 = _last(_sma(c, 250))
    close = float(c.iloc[-1])
    if ma16 is None or ma250 is None:
        r = {"label": "IWV", "tf": "2H", "outfit": "16/250/500",
             "state": None, "error": "insufficient bars",
             "vehicle_pos": "IWV", "vehicle_neg": "—"}
        _set_cache(key, r); return r
    r = {
        "label": "IWV", "tf": "2H", "outfit": "16/250/500",
        "proxy": "IWV",
        "pos_ma": "MA16", "neg_ma": "MA250",
        "ma_a": ma16, "ma_b": ma250, "close": close,
        "state": "POSITIVE" if ma16 > ma250 else "NEGATIVE",
        "spread_pct": (ma16 - ma250) / ma250 * 100,
        "vehicle_pos": "IWV", "vehicle_neg": "—",
    }
    _set_cache(key, r); return r


def _system_sox() -> dict:
    """SOX 30m [16/256/512]  POSITIVE = MA16 > MA256. Proxy: SMH (ETF)."""
    key = "sox_30m"
    if (hit := _check_cache(key)):
        return hit
    # SMH (VanEck Semiconductor ETF) is in engine's ETF_SYMBOLS → clean Webull fetch
    df = _fetch("SMH", "^SOX", "30m", 530)
    if df.empty:
        r = {"label": "SOX", "tf": "30m", "outfit": "16/256/512",
             "state": None, "error": "no data",
             "vehicle_pos": "SOXL", "vehicle_neg": "SOXS"}
        _set_cache(key, r); return r
    c = df["close"]
    ma16  = _last(_sma(c, 16))
    ma256 = _last(_sma(c, 256))
    ma512 = _last(_sma(c, 512))
    close = float(c.iloc[-1])
    if ma16 is None or ma256 is None:
        r = {"label": "SOX", "tf": "30m", "outfit": "16/256/512",
             "state": None, "error": "insufficient bars",
             "vehicle_pos": "SOXL", "vehicle_neg": "SOXS"}
        _set_cache(key, r); return r
    r = {
        "label": "SOX", "tf": "30m", "outfit": "16/256/512",
        "proxy": "SMH",
        "pos_ma": "MA16", "neg_ma": "MA256",
        "major": "MA512", "major_val": ma512,
        "ma_a": ma16, "ma_b": ma256, "close": close,
        "state": "POSITIVE" if ma16 > ma256 else "NEGATIVE",
        "spread_pct": (ma16 - ma256) / ma256 * 100,
        "vehicle_pos": "SOXL", "vehicle_neg": "SOXS",
    }
    _set_cache(key, r); return r


def _system_vix() -> dict:
    """VIX 1H [26/422]  MA26 > MA422 = HIGH-VOL regime active.
    Must use ^VIX (actual index) — not VXX/UVXY ETF proxies.
    VXX/UVXY carry futures roll costs (contango decay) that inflate historical
    MA values, making MA422 artificially high and breaking the regime crossover.

    Current price: CBOE direct API (15-min delayed, always reliable, no key).
      https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json
    Historical bars (for MA26/MA422): yfinance ^VIX 1h fallback.
    Having CBOE price means VIX row is always populated even when yfinance fails."""
    key = "vix_1h"
    if (hit := _check_cache(key)):
        return hit

    # ── Step 1: Current VIX price from CBOE ──────────────────────────────────
    vix_spot = None
    try:
        import urllib.request as _ur
        import json as _json
        with _ur.urlopen(
            "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json",
            timeout=5,
        ) as resp:
            cboe = _json.loads(resp.read())
        vix_spot = float(cboe["data"]["current_price"])
    except Exception:
        pass

    # ── Step 2: Historical bars from yfinance (for MA26 / MA422) ─────────────
    df = pd.DataFrame()
    for _period in ("1y", "2y", "6mo", "max"):
        # Attempt 1: yf.download
        for _adj in (False, True):
            try:
                raw = yf.download("^VIX", period=_period, interval="1h",
                                  progress=False, auto_adjust=_adj, threads=False)
                if raw is not None and not raw.empty:
                    if isinstance(raw.columns, pd.MultiIndex):
                        raw.columns = raw.columns.droplevel(1)
                    raw = raw.reset_index()
                    raw.columns = [str(c).lower() for c in raw.columns]
                    for _c in ("datetime", "date", "index"):
                        if _c in raw.columns:
                            raw = raw.rename(columns={_c: "timestamp"})
                            break
                    df = raw[["timestamp", "close"]].dropna()
                    if not df.empty:
                        break
            except Exception:
                pass
        if not df.empty:
            break
        # Attempt 2: Ticker.history()
        try:
            raw = yf.Ticker("^VIX").history(period=_period, interval="1h")
            if raw is not None and not raw.empty:
                raw = raw.reset_index()
                raw.columns = [str(c).lower() for c in raw.columns]
                for _c in ("datetime", "date", "index"):
                    if _c in raw.columns:
                        raw = raw.rename(columns={_c: "timestamp"})
                        break
                df = raw[["timestamp", "close"]].dropna()
                if not df.empty:
                    break
        except Exception:
            pass

    # ── Step 3: Build result ──────────────────────────────────────────────────
    if df.empty:
        if vix_spot is None:
            # Total failure — no price, no history; retry next cycle
            r = {"label": "VIX", "tf": "1H", "outfit": "26/422",
                 "high_vol": False, "regime": None, "error": "no data"}
            return r
        # CBOE price available but no MA history yet
        r = {
            "label": "VIX", "tf": "1H", "outfit": "26/422",
            "vix": vix_spot,
            "ma26": None, "ma422": None, "ma844": None,
            "spread_pct": None,
            "high_vol": False,
            "regime": "loading",
            "source": "CBOE",
        }
        _set_cache(key, r)
        return r

    c = df["close"]
    # Use CBOE spot as current price if available (more current than yfinance bar)
    vix_close = vix_spot if vix_spot is not None else float(c.iloc[-1])
    ma26  = _last(_sma(c, 26))
    ma422 = _last(_sma(c, 422))
    ma844 = _last(_sma(c, 844))
    if ma26 is None:
        r = {"label": "VIX", "tf": "1H", "outfit": "26/422",
             "vix": vix_close, "high_vol": False, "regime": None,
             "error": "insufficient bars for MA26",
             "source": "CBOE+yf" if vix_spot is not None else "yf"}
        _set_cache(key, r); return r
    high_vol = (ma422 is not None) and (ma26 > ma422)
    r = {
        "label": "VIX", "tf": "1H", "outfit": "26/422",
        "vix": vix_close,
        "ma26": ma26, "ma422": ma422, "ma844": ma844,
        "spread_pct": ((ma26 - ma422) / ma422 * 100) if ma422 else None,
        "high_vol": high_vol,
        "regime": ("HIGH-VOL ACTIVE" if high_vol else
                   "NORMAL" if ma422 is not None else "loading"),
        "source": "CBOE+yf" if vix_spot is not None else "yf",
    }
    _set_cache(key, r); return r


def _system_svix() -> dict:
    """SVIX 1D [116/211/422]  cluster ~20 = structural support.
    Tries Webull 'SVIX', falls back to yfinance 'SVIX'."""
    key = "svix_1d"
    if (hit := _check_cache(key)):
        return hit
    df = _fetch("SVIX", "SVIX", "1d", 440)
    if df.empty:
        r = {"label": "SVIX", "tf": "1D", "outfit": "116/211/422",
             "cluster_hold": None, "error": "no data"}
        _set_cache(key, r); return r
    c = df["close"]
    svix_close = float(c.iloc[-1])
    ma116 = _last(_sma(c, 116))
    ma211 = _last(_sma(c, 211))
    ma422 = _last(_sma(c, 422))
    vals  = [v for v in [ma116, ma211, ma422] if v is not None]
    cluster_val  = round(sum(vals) / len(vals), 2) if vals else None
    cluster_hold = (svix_close >= min(vals)) if vals else None
    cluster_note = (f"HOLDING {cluster_val:.1f}" if cluster_hold
                    else f"LOST {cluster_val:.1f}" if cluster_hold is not None
                    else "—")
    r = {
        "label": "SVIX", "tf": "1D", "outfit": "116/211/422",
        "svix": svix_close,
        "ma116": ma116, "ma211": ma211, "ma422": ma422,
        "cluster_val": cluster_val,
        "cluster_hold": cluster_hold,
        "cluster_note": cluster_note,
    }
    _set_cache(key, r); return r


# ─── Public API ───────────────────────────────────────────────────────────────

def fetch_all_systems() -> dict:
    """Fetch all system states. Cached per instrument. Safe to call every cycle."""
    return {
        "spx":     _system_spx(),
        "ixic":    _system_ixic(),
        "dji_15m": _system_dji_15m(),
        "dji_1h":  _system_dji_1h(),
        "iwm":     _system_iwm(),
        "iwv":     _system_iwv(),
        "sox":     _system_sox(),
        "vix":     _system_vix(),
        "svix":    _system_svix(),
    }


def build_systems_panel(systems: dict = None) -> Panel:
    """Build Rich panel showing current state of all index systems."""
    if systems is None:
        systems = fetch_all_systems()

    vix_data  = systems.get("vix", {})
    svix_data = systems.get("svix", {})
    high_vol  = vix_data.get("high_vol", False)

    # ── Main table ────────────────────────────────────────────────────────────
    tbl = Table(box=box.SIMPLE, show_header=True,
                header_style="bold dim", padding=(0, 1), expand=True)
    tbl.add_column("Index",  width=5,  style="bold")
    tbl.add_column("TF",     width=8,  style="dim")
    tbl.add_column("Price",  width=14)
    tbl.add_column("MA(+)",  width=15)
    tbl.add_column("MA(−)",  width=15)
    tbl.add_column("Spread", width=8)
    tbl.add_column("State",  width=14)
    tbl.add_column("Veh",    width=5,  style="dim")

    PRIMARY = ["spx", "ixic", "dji_15m", "dji_1h"]
    BROAD   = ["iwm", "iwv"]
    SEMI    = ["sox"]

    def _row(key: str):
        s = systems.get(key, {})
        label = s.get("label", key.upper())
        tf    = s.get("tf", "—")
        src   = s.get("source", "")

        # Show yfinance fallback in yellow — signals possible MA timeframe mismatch
        if src and "yf" in src:
            tf_cell: object = Text()
            tf_cell.append(tf, style="dim")
            tf_cell.append(" ·yf", style="dim yellow")
        else:
            tf_cell = tf

        if s.get("state") is None:
            err = s.get("error", "loading...")
            tbl.add_row(label, tf_cell, Text("—", style="dim"), "—", "—", "—",
                        Text(err, style="dim"), "—")
            return

        state   = s["state"]
        close   = s.get("close")
        proxy   = s.get("proxy", "")
        ma_a    = s.get("ma_a")
        ma_b    = s.get("ma_b")
        spread  = s.get("spread_pct")
        pos_ma  = s.get("pos_ma", "")
        neg_ma  = s.get("neg_ma", "")
        veh     = s["vehicle_pos"] if state == "POSITIVE" else s.get("vehicle_neg", "—")

        price_cell = Text()
        if close:
            price_cell.append(f"{proxy} ", style="dim")
            price_cell.append(f"{close:,.2f}", style="bold")
        ma_a_str  = f"{ma_a:,.1f}"  if ma_a  else "—"
        ma_b_str  = f"{ma_b:,.1f}"  if ma_b  else "—"

        col    = "green" if state == "POSITIVE" else "red"
        symbol = "●"

        # Under high-vol, flag which MA becomes the active close-rule level
        if high_vol:
            state_text = Text()
            state_text.append(f"{symbol} ", style=col)
            state_text.append(state[:3], style=f"bold {col}")
            state_text.append(f" /{neg_ma}", style="dim yellow")
        else:
            state_text = Text(f"{symbol} {state}", style=f"bold {col}")

        spread_str = f"{spread:+.2f}%" if spread is not None else "—"
        spread_col = "green" if (spread or 0) > 0 else "red"

        tbl.add_row(
            label, tf_cell,
            price_cell,
            f"[dim]{pos_ma}[/dim] {ma_a_str}",
            f"[dim]{neg_ma}[/dim] {ma_b_str}",
            Text(spread_str, style=spread_col),
            state_text,
            veh,
        )

    def _sep():
        tbl.add_row("", "", "", "", "", "", "", "", style="dim")

    def _vix_row():
        v       = systems.get("vix", {})
        vix_val = v.get("vix")
        ma26    = v.get("ma26")
        ma422   = v.get("ma422")
        error   = v.get("error")
        if vix_val is None:
            err_msg = error or "loading..."
            tbl.add_row("VIX", "1H", Text("^VIX", style="dim"),
                        "—", "—", "—", Text(f"⚠ {err_msg}", style="dim yellow"), "—")
            return
        high_v     = v.get("high_vol", False)
        regime     = v.get("regime", "—")
        spread     = v.get("spread_pct")
        vix_col    = "red" if vix_val > 25 else "yellow" if vix_val > 18 else "green"
        price_cell = Text()
        price_cell.append("^VIX ", style="dim")
        price_cell.append(f"{vix_val:.2f}", style=f"bold {vix_col}")
        ma26_str   = f"{ma26:.2f}"  if ma26  else "—"
        ma422_str  = f"{ma422:.2f}" if ma422 else "—"
        spread_str = f"{spread:+.2f}%" if spread is not None else "—"
        state_col  = "red" if high_v else "green"
        symbol     = "⚠" if high_v else "●"
        regime_short = "HIGH-VOL" if high_v else regime   # keep within col width
        state_text = Text()
        state_text.append(f"{symbol} ", style=state_col)
        state_text.append(regime_short, style=f"bold {state_col}")
        tbl.add_row(
            "VIX", "1H", price_cell,
            f"[dim]MA26[/dim] {ma26_str}",
            f"[dim]MA422[/dim] {ma422_str}",
            Text(spread_str, style=state_col),
            state_text, "—",
        )

    def _svix_row():
        sv       = systems.get("svix", {})
        svix_val = sv.get("svix")
        cl_val   = sv.get("cluster_val")
        cl_hold  = sv.get("cluster_hold")
        cl_note  = sv.get("cluster_note", "—")
        error    = sv.get("error")
        if svix_val is None:
            err_msg = error or "loading..."
            tbl.add_row("SVIX", "1D", Text("SVIX", style="dim"),
                        "—", "—", "—", Text(f"⚠ {err_msg}", style="dim yellow"), "—")
            return
        price_cell = Text()
        price_cell.append("SVIX ", style="dim")
        price_cell.append(f"{svix_val:.2f}", style="bold white")
        cl_str     = f"{cl_val:.2f}" if cl_val else "—"
        spread_val = (svix_val - cl_val) / cl_val * 100 if cl_val else None
        spread_str = f"{spread_val:+.2f}%" if spread_val is not None else "—"
        state_col  = "green" if cl_hold else "red"
        state_text = Text()
        state_text.append("● ", style=state_col)
        state_text.append(cl_note, style=f"bold {state_col}")
        tbl.add_row(
            "SVIX", "1D", price_cell,
            f"[dim]cluster[/dim] ~{cl_str}",
            "—",
            Text(spread_str, style=state_col),
            state_text, "—",
        )

    for k in PRIMARY:
        _row(k)
    _sep()
    for k in BROAD:
        _row(k)
    _sep()
    for k in SEMI:
        _row(k)
    _sep()
    _vix_row()
    _svix_row()

    # ── High-vol regime note (brief) ──────────────────────────────────────────
    t_footer = Text()
    if high_vol:
        t_footer.append(
            "\n⚠ HIGH-VOL REGIME — active signal shifts to candle close vs key MA",
            style="bold red"
        )
    elif vix_data.get("error"):
        t_footer.append(f"\n⚠ VIX: {vix_data['error']}", style="dim yellow")

    # ── Alignment summary ─────────────────────────────────────────────────────
    primary_states = [systems.get(k, {}).get("state") for k in PRIMARY]
    pos_count = sum(1 for s in primary_states if s == "POSITIVE")
    neg_count = sum(1 for s in primary_states if s == "NEGATIVE")
    known     = pos_count + neg_count

    t_align = Text("\n")
    if known == 0:
        t_align.append("Loading systems...", style="dim")
        border_col = "dim"
    elif pos_count == known:
        t_align.append("▲ ALL POSITIVE — institutional accumulation, firms actively buying",
                        style="bold bright_green")
        border_col = "green"
    elif neg_count == known:
        t_align.append("▼ ALL NEGATIVE — institutional distribution, no firm steps in ahead of majors",
                        style="bold bright_red")
        border_col = "red"
    elif pos_count > neg_count:
        t_align.append(f"▲ {pos_count}/{known} POSITIVE — bullish lean, watch lagging system for confirmation",
                        style="green")
        border_col = "green"
    elif neg_count > pos_count:
        t_align.append(f"▼ {neg_count}/{known} NEGATIVE — bearish lean, rotation in progress",
                        style="red")
        border_col = "red"
    else:
        t_align.append(f"↔ MIXED {pos_count}↑ {neg_count}↓ — fill-the-bucket rotation, systems splitting",
                        style="yellow")
        border_col = "yellow"

    if high_vol:
        border_col = "bright_red"

    now = datetime.now(timezone.utc).strftime("%H:%M UTC")

    return Panel(
        Group(tbl, t_footer, t_align),
        title="[bold]📊 SYSTEMS — INDEX STATE[/bold]",
        subtitle=f"[dim]Webull · {now}[/dim]",
        border_style=border_col,
        padding=(0, 1),
    )


# ─── Standalone test ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    from rich.console import Console
    console = Console()
    console.print("[dim]Fetching all systems...[/dim]")
    systems = fetch_all_systems()
    panel = build_systems_panel(systems)
    console.print(panel)
    # Raw data dump
    console.print("\n[dim]--- Raw data ---[/dim]")
    for k, v in systems.items():
        state = v.get("state") or v.get("regime") or v.get("error", "—")
        console.print(f"  [bold]{k:<10}[/bold] {state}")
