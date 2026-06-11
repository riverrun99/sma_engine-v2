"""
The System — TraderBJones Implementation (accurate to document)
===============================================================
Indicators (30m SPY):
  SMA10, SMA50, SMA200
  EMA9, EMA21, EMA50

State:    SMA10 > SMA50  → SYSTEM UP   (vehicle: UPRO)
          SMA10 < SMA50  → SYSTEM DOWN (vehicle: SPXU)

Entry types:
  CROSS   — SMA10/50 cross just occurred + EMA9 crossed EMA50 (confirmation)
  BOUNCE  — Extreme oversold (price < -2% from SMA50) + price reclaims SMA10
  EXIT    — Price closes opposite side of SMA50

Oversold:   price < -2% below SMA50  (extreme: < -3%)
Overbought: price > +2% above SMA50  (extreme: > +3%)

Key notes from document:
  - Invalid cross: bullish cross but price still below both SMAs
  - Caution: cross but SMA50 still sloping down
  - Watch SMA200 as major support/resistance
  - Higher timeframe SMA50 matters (1hr SMA50 vs 30m)
"""

import os
import sys
import time
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from engine import WebullClient, MockClient


TICKER       = "SPY"
VEHICLE_UP   = "UPRO"
VEHICLE_DOWN = "SPXU"
CANDLE_COUNT = 200

# Thresholds (document-defined)
# Bounce trade: market has fallen > -3% from SMA50 (confirmed in doc slide 4)
# FAQ slide says -2 to -3% = oversold; > -3% = extreme/bounce territory
OVERSOLD_PCT       = -2.0   # price % below SMA50 — starting to get oversold
OVERSOLD_EXTREME   = -3.0   # extreme oversold — bounce trade zone
OVERBOUGHT_PCT     = +2.0
OVERBOUGHT_EXTREME = +3.0


def get_client():
    app_key    = os.environ.get("WEBULL_APP_KEY", "")
    app_secret = os.environ.get("WEBULL_APP_SECRET", "")
    region     = os.environ.get("WEBULL_REGION", "us")
    if app_key and app_secret:
        return WebullClient(app_key, app_secret, region=region)
    return MockClient()


def fetch_yfinance_fallback() -> pd.DataFrame:
    try:
        import yfinance as yf
        for attempt in range(3):
            df = yf.download(TICKER, period="10d", interval="30m",
                             progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.droplevel(1)
            if not df.empty:
                df = df.reset_index()
                df.columns = [c.lower() for c in df.columns]
                df = df.rename(columns={"datetime": "timestamp", "date": "timestamp"})
                if "timestamp" not in df.columns and df.columns[0] != "timestamp":
                    df = df.rename(columns={df.columns[0]: "timestamp"})
                return df[["timestamp", "open", "high", "low", "close", "volume"]]
            time.sleep(3 * (attempt + 1))
    except Exception:
        pass
    return pd.DataFrame()


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"].copy()
    df = df.copy()
    df["sma10"]  = c.rolling(10).mean()
    df["sma50"]  = c.rolling(50).mean()
    df["sma200"] = c.rolling(200).mean()
    df["ema9"]   = c.ewm(span=9,  adjust=False).mean()
    df["ema21"]  = c.ewm(span=21, adjust=False).mean()
    df["ema50"]  = c.ewm(span=50, adjust=False).mean()
    return df


def fetch_nasdaq() -> dict:
    """
    Fetch QQQ 30m bars for NASDAQ leading indicator.
    Returns key levels and relative performance vs SPY.
    "The NASDAQ always leads the way, up or down."
    """
    try:
        client = get_client()
        df = client.fetch_bars("QQQ", "30m", CANDLE_COUNT)
        if df is None or df.empty:
            import yfinance as yf
            df = yf.download("QQQ", period="10d", interval="30m",
                             progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.droplevel(1)
            df = df.reset_index()
            df.columns = [c.lower() for c in df.columns]
            # Normalize timestamp column name (yfinance uses 'datetime' or 'date')
            for _col in ("datetime", "date", "index"):
                if _col in df.columns:
                    df = df.rename(columns={_col: "timestamp"})
                    break

        if df is None or df.empty:
            return {"error": "No QQQ data"}

        df = compute_indicators(df)
        df = df.dropna(subset=["sma10", "sma50"])
        if len(df) < 2:
            return {"error": "Not enough QQQ bars"}

        last  = df.iloc[-1]
        close   = float(last["close"])
        sma10   = float(last["sma10"])
        sma50   = float(last["sma50"])
        state   = "UP" if sma10 > sma50 else "DOWN"

        # Day % = change vs yesterday's official close (last bar before today).
        # Using yesterday's last bar avoids the pre-market reference bug where
        # Webull's first bar of today can be a 4 AM pre-market bar at a different
        # level than the official prior close.
        gap_pct = 0.0
        open_   = close
        if "timestamp" in df.columns:
            today_str = str(last["timestamp"])[:10]
            prev_bars = df[df["timestamp"].astype(str).str[:10] < today_str]
            if not prev_bars.empty:
                prev_close = float(prev_bars.iloc[-1]["close"])
                gap_pct = (close - prev_close) / prev_close * 100
                open_   = prev_close

        dist_sma50 = (close - sma50) / sma50 * 100
        sma50_slope = _slope(df["sma50"])

        return {
            "close":       round(close, 2),
            "open":        round(open_, 2),
            "gap_pct":     round(gap_pct, 3),
            "sma10":       round(sma10, 3),
            "sma50":       round(sma50, 3),
            "state":       state,
            "dist_sma50":  round(dist_sma50, 3),
            "sma50_slope": round(sma50_slope, 4),
            "sma50_dir":   ("rising" if sma50_slope > 0.05 else
                            "falling" if sma50_slope < -0.05 else "flat"),
        }
    except Exception as e:
        return {"error": str(e)}


def _slope(series, n=5) -> float:
    """% change of a series over last n bars — positive = rising, negative = falling."""
    if len(series) < n + 1:
        return 0.0
    return (series.iloc[-1] - series.iloc[-n]) / series.iloc[-n] * 100


def analyze() -> dict:
    client = get_client()
    df = client.fetch_bars(TICKER, "30m", CANDLE_COUNT)

    source = "Webull"
    if df is None or df.empty:
        df = fetch_yfinance_fallback()
        source = "yfinance"

    if df is None or df.empty:
        return {"error": "No data for SPY 30m (Webull + yfinance both failed)"}

    df = compute_indicators(df)
    df = df.dropna(subset=["sma10", "sma50", "ema9", "ema21", "ema50"])

    if len(df) < 3:
        return {"error": "Not enough bars after dropna"}

    last = df.iloc[-1]
    prev = df.iloc[-2]

    close   = float(last["close"])
    sma10   = float(last["sma10"])
    sma50   = float(last["sma50"])
    sma200  = float(last.get("sma200", 0)) if last.get("sma200") and not pd.isna(last.get("sma200", float("nan"))) else None
    ema9    = float(last["ema9"])
    ema21   = float(last["ema21"])
    ema50   = float(last["ema50"])

    # ── State ──────────────────────────────────────────────────────────────────
    system_up = sma10 > sma50
    state     = "UP"   if system_up else "DOWN"
    vehicle   = VEHICLE_UP if system_up else VEHICLE_DOWN

    # ── Spreads & distances ────────────────────────────────────────────────────
    sma_spread_pct  = (sma10  - sma50)  / sma50  * 100
    dist_sma50_pct  = (close  - sma50)  / sma50  * 100
    dist_sma10_pct  = (close  - sma10)  / sma10  * 100
    dist_sma200_pct = (close  - sma200) / sma200 * 100 if sma200 else None
    dist_ema9_pct   = (close  - ema9)   / ema9   * 100
    dist_ema50_pct  = (close  - ema50)  / ema50  * 100

    # ── SMA50 slope (is it flattening/turning?) ────────────────────────────────
    sma50_slope = _slope(df["sma50"])   # % over last 5 bars
    sma50_dir   = ("rising" if sma50_slope > 0.05 else
                   "falling" if sma50_slope < -0.05 else "flat")

    # ── Cross detection (this bar vs previous bar) ─────────────────────────────
    prev_sma_up  = float(prev["sma10"]) > float(prev["sma50"])
    prev_ema_up  = float(prev["ema9"])  > float(prev["ema50"])
    curr_ema_up  = ema9 > ema50

    sma_cross    = prev_sma_up != system_up   # SMA10/50 just crossed
    ema_cross    = prev_ema_up != curr_ema_up  # EMA9/50 just crossed
    cross_confirmed = sma_cross and ema_cross  # both crossed same direction

    # ── Choppy / no-trend detection ───────────────────────────────────────────
    # Doc: "If there is no general direction, wash trades can occur — sit on cash"
    # Use SMA spread as proxy: tight spread = no definitive trend
    choppy = abs(sma_spread_pct) < 0.3   # SMAs nearly equal = no trend

    # ── Oversold / Overbought ──────────────────────────────────────────────────
    if dist_sma50_pct <= OVERSOLD_EXTREME:
        ob_os = "EXTREME OVERSOLD"
    elif dist_sma50_pct <= OVERSOLD_PCT:
        ob_os = "OVERSOLD"
    elif dist_sma50_pct >= OVERBOUGHT_EXTREME:
        ob_os = "EXTREME OVERBOUGHT"
    elif dist_sma50_pct >= OVERBOUGHT_PCT:
        ob_os = "OVERBOUGHT"
    else:
        ob_os = "NEUTRAL"

    # ── Entry / Exit signal ────────────────────────────────────────────────────
    entry_signal = "HOLD / WAIT"
    entry_reason = ""
    entry_type   = ""
    caution      = False
    invalid      = False

    if system_up:
        # Exit: price closes below SMA50
        if close < sma50:
            entry_signal = "EXIT LONG"
            entry_reason = f"Close below SMA50 ({dist_sma50_pct:+.2f}%)"
            entry_type   = "EXIT"

        # Cross entry
        elif sma_cross:
            if close < sma10 or close < sma50:
                # Bullish cross but price still below SMAs → invalid
                entry_signal = "CROSS — INVALID"
                entry_reason = "Price still below SMA10/50 after cross"
                entry_type   = "CROSS"
                invalid      = True
            else:
                entry_signal = "ENTER LONG — CROSS" if cross_confirmed else "ENTER LONG — CROSS (EMA unconfirmed)"
                entry_reason = f"SMA10/50 bullish cross" + (" + EMA9/50 confirmed" if cross_confirmed else "")
                entry_type   = "CROSS"
                caution      = sma50_dir == "falling"  # SMA50 still pointing down

        # Bounce entry: extreme oversold + price reclaims SMA10
        elif ob_os in ("OVERSOLD", "EXTREME OVERSOLD") and close > sma10:
            entry_signal = "ENTER LONG — BOUNCE"
            entry_reason = f"Oversold ({dist_sma50_pct:+.2f}% from SMA50) + reclaimed SMA10"
            entry_type   = "BOUNCE"

    else:
        # Exit: price closes above SMA50
        if close > sma50:
            entry_signal = "EXIT SHORT"
            entry_reason = f"Close above SMA50 ({dist_sma50_pct:+.2f}%)"
            entry_type   = "EXIT"

        # Cross entry — two steps per document:
        # Step 1 (just crossed): GO TO CASH — safe default, protects against fake signal
        # Step 2 (market still weak + SMA50 sloping down): ENTER SHORT
        elif sma_cross:
            if close > sma10 or close > sma50:
                entry_signal = "CROSS — INVALID"
                entry_reason = "Price still above SMA10/50 after bearish cross"
                entry_type   = "CROSS"
                invalid      = True
            elif sma50_dir == "falling":
                # SMA50 already sloping down + cross = strong short signal
                entry_signal = "ENTER SHORT — CROSS" if cross_confirmed else "ENTER SHORT — CROSS (EMA unconfirmed)"
                entry_reason = f"SMA10/50 bearish cross + SMA50 trending down" + (" + EMA9/50 confirmed" if cross_confirmed else "")
                entry_type   = "CROSS"
            else:
                # SMA50 flat or rising = cautious, go to cash first
                entry_signal = "GO TO CASH — CROSS"
                entry_reason = "Bearish cross but SMA50 not yet sloping down — wait for confirmation"
                entry_type   = "CROSS"
                caution      = True

        # Bounce entry: extreme overbought + price breaks back below SMA10
        elif ob_os in ("OVERBOUGHT", "EXTREME OVERBOUGHT") and close < sma10:
            entry_signal = "ENTER SHORT — BOUNCE"
            entry_reason = f"Overbought ({dist_sma50_pct:+.2f}% from SMA50) + broke below SMA10"
            entry_type   = "BOUNCE"

    # ── SMA200 context ─────────────────────────────────────────────────────────
    sma200_context = None
    if sma200:
        if abs(dist_sma200_pct) < 0.5:
            sma200_context = f"AT SMA200 ({dist_sma200_pct:+.2f}%) — key level"
        elif close < sma200:
            sma200_context = f"Below SMA200 ({dist_sma200_pct:+.2f}%) — resistance above"
        else:
            sma200_context = f"Above SMA200 ({dist_sma200_pct:+.2f}%) — support below"

    ts = last.get("timestamp", "")

    return {
        "ticker":            TICKER,
        "timestamp":         str(ts),
        "close":             round(close, 2),
        "source":            source,

        # State
        "state":             state,
        "vehicle":           vehicle,

        # SMAs
        "sma10":             round(sma10, 3),
        "sma50":             round(sma50, 3),
        "sma200":            round(sma200, 3) if sma200 else None,
        "sma_spread_pct":    round(sma_spread_pct, 3),
        "sma50_slope":       round(sma50_slope, 4),
        "sma50_dir":         sma50_dir,

        # EMAs
        "ema9":              round(ema9, 3),
        "ema21":             round(ema21, 3),
        "ema50":             round(ema50, 3),

        # Distances
        "dist_sma50_pct":    round(dist_sma50_pct, 3),
        "dist_sma10_pct":    round(dist_sma10_pct, 3),
        "dist_sma200_pct":   round(dist_sma200_pct, 3) if dist_sma200_pct is not None else None,
        "dist_ema9_pct":     round(dist_ema9_pct, 3),
        "dist_ema50_pct":    round(dist_ema50_pct, 3),

        # Conditions
        "ob_os":             ob_os,
        "sma200_context":    sma200_context,

        # Signals
        "entry_signal":      entry_signal,
        "entry_reason":      entry_reason,
        "entry_type":        entry_type,
        "cross_confirmed":   cross_confirmed,
        "caution":           caution,
        "invalid_cross":     invalid,
        "trend_flip":        sma_cross,
        "choppy":            choppy,
        "nasdaq":            fetch_nasdaq(),
    }


if __name__ == "__main__":
    r = analyze()
    for k, v in r.items():
        if v is not None:
            print(f"{k:22s}: {v}")
