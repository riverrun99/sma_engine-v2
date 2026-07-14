"""
signal_tracker_main.py — Main Engine Signal Tracker
=====================================================
Tracks top signals from the main engine snapshot output.
Logs new signals and computes forward returns at +1d/+3d/+5d/+10d/+20d.

Source: output/snapshots/snapshot_*.csv (latest each run)
Log:    output/signal_tracking/main_signal_log.json

Safety:
  - NEVER writes to any engine output file
  - All writes go to output/signal_tracking/ only

Usage:
    python3 signal_tracker_main.py              # update log + print report
    python3 signal_tracker_main.py --top 30     # track top N signals (default 50)
    python3 signal_tracker_main.py --report     # print report only, no update
    python3 signal_tracker_main.py --csv        # also save CSV report
    python3 signal_tracker_main.py --prune 60   # drop signals older than N days
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

# ── Load .env ─────────────────────────────────────────────────────────────────
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

try:
    import pandas as pd
except ImportError as e:
    print(f"ERROR: Missing dependency — {e}")
    print("Run: pip install pandas openpyxl --break-system-packages")
    sys.exit(1)

# ── Webull client ─────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
try:
    from engine import WebullClient
    _wb_key    = os.environ.get("WEBULL_APP_KEY", "")
    _wb_secret = os.environ.get("WEBULL_APP_SECRET", "")
    _webull_client = (
        WebullClient(_wb_key, _wb_secret, region=os.environ.get("WEBULL_REGION", "us"))
        if _wb_key and _wb_secret else None
    )
    if not _webull_client:
        print("  WARNING: WEBULL_APP_KEY/WEBULL_APP_SECRET not set — price fills unavailable")
except Exception as _e:
    _webull_client = None
    print(f"  WARNING: Could not init Webull client — {_e}")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
OUTPUT_DIR   = BASE_DIR / "output"
TRACKING_DIR = OUTPUT_DIR / "signal_tracking"
LOG_FILE     = TRACKING_DIR / "main_signal_log.json"

FORWARD_WINDOWS = [1, 3, 5, 10, 20]


# ── Log I/O ───────────────────────────────────────────────────────────────────

def load_log() -> dict:
    if LOG_FILE.exists():
        with open(LOG_FILE) as f:
            return json.load(f)
    return {}


def save_log(log: dict) -> None:
    TRACKING_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2, default=str)


# ── Source finder ─────────────────────────────────────────────────────────────

def latest_snapshot() -> Path | None:
    files = sorted(glob.glob(str(OUTPUT_DIR / "snapshots" / "snapshot_*.csv")))
    return Path(files[-1]) if files else None


# ── Signal ingestion ──────────────────────────────────────────────────────────

def ingest_snapshot(path: Path, log: dict, top: int) -> int:
    """
    Read the latest snapshot CSV and log any new top-N signals.
    Returns count of newly added signals.
    """
    try:
        df = pd.read_csv(path)
        df.columns = [c.strip() for c in df.columns]
    except Exception as e:
        print(f"  ERROR reading snapshot: {e}")
        return 0

    # Timestamp from file
    fname = path.stem   # snapshot_2026-07-13_18-16-35
    parts = fname.split("_", 1)
    if len(parts) == 2:
        date_part = parts[1][:10]
        time_part = parts[1][11:].replace("-", ":")
        detected_ts   = f"{date_part}T{time_part}+00:00"
        detected_date = date_part
    else:
        detected_ts   = datetime.now(timezone.utc).isoformat()
        detected_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Sort by score descending — snapshot is already sorted but enforce it
    if "score" in df.columns:
        df = df.sort_values("score", ascending=False)

    added = 0
    for rank, (_, row) in enumerate(df.head(top).iterrows(), start=1):
        ticker    = str(row.get("ticker", "")).strip().upper()
        timeframe = str(row.get("timeframe", "")).strip()
        outfit    = str(row.get("outfit", "")).strip()

        if not ticker or not timeframe:
            continue

        key = f"{ticker}|{timeframe}|{outfit}"
        if key in log:
            continue

        log[key] = {
            "ticker":        ticker,
            "timeframe":     timeframe,
            "outfit":        outfit,
            "hits":          int(row.get("hits", 0) or 0),
            "convergence":   str(row.get("convergence", "")),
            "score":         float(row.get("score", 0) or 0),
            "rank":          rank,
            "entry_price":   None,   # filled on first price-fetch run
            "detected_ts":   detected_ts,
            "detected_date": detected_date,
            "source":        "main",
            "forward":       {str(w): None for w in FORWARD_WINDOWS},
        }
        added += 1

    return added


# ── Price fetching & forward return fill ──────────────────────────────────────

def _fetch_daily_bars(ticker: str, days_back: int) -> "pd.Series | None":
    """Return a pd.Series of close prices indexed by date (pd.Timestamp)."""
    if _webull_client is None:
        return None
    try:
        df = _webull_client.fetch_bars(ticker, "1d", min(max(days_back, 30), 999))
        if df is None or df.empty:
            return None
        df["date"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None).dt.normalize()
        s = df.set_index("date")["close"]
        s.index = pd.to_datetime(s.index)
        return s
    except Exception as e:
        logging.debug(f"Webull fetch error ({ticker}): {e}")
        return None


def fill_forward_returns(log: dict) -> int:
    """
    Fill entry prices (if missing) and forward return windows.
    Returns count of signals updated.
    """
    today = date.today()

    # Figure out what needs fetching
    to_fill: dict[str, list[int]] = {}
    needs_entry: list[str] = []
    earliest_dates: dict[str, date] = {}

    for key, sig in log.items():
        det_str = sig.get("detected_date")
        if not det_str:
            continue
        try:
            det = date.fromisoformat(det_str)
        except ValueError:
            continue

        # Need entry price?
        if sig.get("entry_price") is None:
            needs_entry.append(key)

        fwd = sig.get("forward", {})
        unfilled = []
        for w in FORWARD_WINDOWS:
            if fwd.get(str(w)) is None:
                cal_days_needed = max(2, int(w * 1.5))
                if (today - det).days >= cal_days_needed:
                    unfilled.append(w)

        if unfilled or sig.get("entry_price") is None:
            ticker = sig["ticker"]
            to_fill[key] = unfilled
            if ticker not in earliest_dates or det < earliest_dates[ticker]:
                earliest_dates[ticker] = det

    if not to_fill and not needs_entry:
        return 0

    if _webull_client is None:
        print("  Skipping price fill — Webull client unavailable")
        return 0

    # Fetch bars per ticker
    min_date  = min(earliest_dates.values()) if earliest_dates else date.today()
    days_back = (today - min_date).days + 5
    print(f"  Fetching daily history for {len(earliest_dates)} ticker(s) ...")

    hist_by_ticker: dict[str, pd.Series] = {}
    for ticker in earliest_dates:
        s = _fetch_daily_bars(ticker, days_back)
        if s is not None and not s.empty:
            hist_by_ticker[ticker] = s

    updated = 0
    for key, unfilled_windows in to_fill.items():
        sig    = log[key]
        ticker = sig["ticker"]
        closes = hist_by_ticker.get(ticker)
        if closes is None or closes.empty:
            continue

        det_ts   = pd.Timestamp(sig["detected_date"]).tz_localize(None)
        idx      = pd.to_datetime(closes.index).tz_localize(None) if closes.index.tz else pd.to_datetime(closes.index)
        after_mask = idx > det_ts
        after    = closes[after_mask]

        # Fill entry price from close on detection date (or nearest after)
        if sig.get("entry_price") is None:
            on_day = closes[idx == det_ts]
            if not on_day.empty:
                sig["entry_price"] = round(float(on_day.iloc[0]), 4)
            elif not after.empty:
                sig["entry_price"] = round(float(after.iloc[0]), 4)

        if after.empty:
            continue

        fwd     = sig.setdefault("forward", {str(w): None for w in FORWARD_WINDOWS})
        changed = False
        for w in unfilled_windows:
            if len(after) >= w:
                fwd[str(w)] = round(float(after.iloc[w - 1]), 4)
                changed = True

        if changed:
            updated += 1

    return updated


# ── Reporting ─────────────────────────────────────────────────────────────────

def _pct(entry: float | None, fwd_price: float | None) -> float | None:
    if entry and fwd_price and entry > 0:
        return round((fwd_price - entry) / entry * 100, 2)
    return None


def _pct_flag(pct: float | None) -> str:
    if pct is None:
        return "\033[2m  —   \033[0m"
    color = "\033[92m" if pct > 0 else ("\033[91m" if pct < 0 else "\033[93m")
    arrow = "↑" if pct > 0 else ("↓" if pct < 0 else "→")
    return f"{color}{arrow}{abs(pct):5.2f}%\033[0m"


def print_report(log: dict, top: int = 50) -> None:
    if not log:
        print("  No signals tracked yet.")
        return

    signals = sorted(
        log.values(),
        key=lambda s: (s.get("detected_date", ""), s.get("rank", 9999)),
    )

    all_pcts: dict[int, list[float]] = {w: [] for w in FORWARD_WINDOWS}
    for s in signals:
        ep  = s.get("entry_price")
        fwd = s.get("forward", {})
        for w in FORWARD_WINDOWS:
            p = _pct(ep, fwd.get(str(w)))
            if p is not None:
                all_pcts[w].append(p)

    print()
    print("═" * 100)
    print("  MAIN ENGINE — SIGNAL FORWARD RETURN TRACKER")
    print("═" * 100)
    print(f"  {'#':<4} {'Ticker':<8} {'TF':<5} {'Conv':<6} {'Score':>14}  "
          f"{'Entry':>7}  {'Detected':<11}  "
          f"{'  +1d':>7}  {'  +3d':>7}  {'  +5d':>7}  {'  +10d':>7}  {'  +20d':>7}")
    print("  " + "─" * 96)

    for s in signals[:top]:
        ep     = s.get("entry_price")
        fwd    = s.get("forward", {})
        ep_str = f"${ep:.2f}" if ep else "    —"
        score  = s.get("score", 0)
        # Format large scores (e.g. 34372622678 → 34.4B)
        if score >= 1e9:
            score_str = f"{score/1e9:.1f}B"
        elif score >= 1e6:
            score_str = f"{score/1e6:.1f}M"
        else:
            score_str = f"{score:.1f}"

        pcts = [_pct(ep, fwd.get(str(w))) for w in FORWARD_WINDOWS]
        print(
            f"  {s.get('rank', 0):<4} {s['ticker']:<8} {s['timeframe']:<5} "
            f"{s.get('convergence',''):<6} {score_str:>14}  "
            f"{ep_str:>7}  {s.get('detected_date',''):<11}  "
            + "  ".join(_pct_flag(p) for p in pcts)
        )

    print("  " + "─" * 96)
    print(f"  Showing {min(top, len(signals))} of {len(signals)} tracked signals\n")

    print(f"  {'Avg return':>51}", end="")
    for w in FORWARD_WINDOWS:
        vals = all_pcts[w]
        print(f"  {_pct_flag(sum(vals)/len(vals) if vals else None)}", end="")
    print()

    print(f"  {'Win rate':>51}", end="")
    for w in FORWARD_WINDOWS:
        vals = all_pcts[w]
        if vals:
            wr  = sum(1 for v in vals if v > 0) / len(vals) * 100
            col = "\033[92m" if wr >= 60 else ("\033[91m" if wr < 40 else "\033[93m")
            print(f"  {col}{wr:5.1f}% \033[0m", end="")
        else:
            print(f"  {'  —   ':>7}", end="")
    print()

    print(f"  {'Sample n':>51}", end="")
    for w in FORWARD_WINDOWS:
        print(f"  {'n='+str(len(all_pcts[w])):>7}", end="")
    print("\n")
    print("═" * 100)


def save_report_csv(log: dict) -> Path:
    TRACKING_DIR.mkdir(parents=True, exist_ok=True)
    out_path = TRACKING_DIR / f"main_performance_{date.today().isoformat()}.csv"
    rows = []
    for s in sorted(log.values(),
                    key=lambda x: (x.get("detected_date", ""), x.get("rank", 9999))):
        ep  = s.get("entry_price")
        fwd = s.get("forward", {})
        row = {
            "ticker":        s["ticker"],
            "timeframe":     s["timeframe"],
            "outfit":        s["outfit"],
            "hits":          s.get("hits", ""),
            "convergence":   s.get("convergence", ""),
            "score":         s.get("score", ""),
            "rank":          s.get("rank", ""),
            "entry_price":   ep,
            "detected_date": s.get("detected_date", ""),
        }
        for w in FORWARD_WINDOWS:
            row[f"price_{w}d"] = fwd.get(str(w))
            row[f"pct_{w}d"]   = _pct(ep, fwd.get(str(w)))
        rows.append(row)
    if rows:
        pd.DataFrame(rows).to_csv(out_path, index=False)
    return out_path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Main Engine — Signal Forward Return Tracker")
    parser.add_argument("--top",    type=int, default=50,
                        help="Top N signals to ingest from snapshot (default: 50)")
    parser.add_argument("--report", action="store_true",
                        help="Print report only — skip ingestion and price fetch")
    parser.add_argument("--csv",    action="store_true",
                        help="Also save CSV report to output/signal_tracking/")
    parser.add_argument("--prune",  type=int, default=60,
                        help="Drop signals older than N days (default: 60)")
    args = parser.parse_args()

    TRACKING_DIR.mkdir(parents=True, exist_ok=True)

    print()
    print("═" * 60)
    print("  SIGNAL TRACKER — MAIN ENGINE")
    print("═" * 60)

    log = load_log()
    print(f"  Signals in log: {len(log)}")

    if not args.report:
        snap_path = latest_snapshot()
        if snap_path:
            print(f"  Snapshot: {snap_path.name}")
            added = ingest_snapshot(snap_path, log, args.top)
            print(f"  New signals: {added}")
        else:
            print("  WARNING: No snapshot found. Run engine first.")

        # Prune old signals
        cutoff = date.today() - timedelta(days=args.prune)
        before = len(log)
        log = {
            k: v for k, v in log.items()
            if date.fromisoformat(v.get("detected_date", "2000-01-01")) >= cutoff
        }
        pruned = before - len(log)
        if pruned:
            print(f"  Pruned: {pruned} signals older than {args.prune} days")

        updated = fill_forward_returns(log)
        print(f"  Windows filled: {updated} signal(s) updated")

        save_log(log)
        print(f"  Log saved → {LOG_FILE.relative_to(BASE_DIR)}")

    print_report(log, top=args.top)

    if args.csv:
        csv_path = save_report_csv(log)
        print(f"  CSV saved → {csv_path.relative_to(BASE_DIR)}")

    print()


if __name__ == "__main__":
    main()
