"""
signal_tracker_v2.py — Forward Return Tracker
===============================================
Standalone script. Safe to run after any engine cycle.

What it does:
  1. Reads the latest v3 xlsx output (top signals with grade/score/entry price)
  2. Logs new signals to output/signal_tracking/signal_log.json
  3. Fills in closing prices at +1d / +3d / +5d / +10d / +20d trading days
     using yfinance — once a window is filled it is never re-fetched
  4. Prints a summary table to the terminal
  5. Saves output/signal_tracking/performance_YYYY-MM-DD.csv

Safety rules:
  - NEVER writes to any existing output file
  - NEVER modifies any engine file
  - All writes go to output/signal_tracking/ only

Usage:
    python3 signal_tracker_v2.py              # update log + print report
    python3 signal_tracker_v2.py --top 100    # track top N from latest v3
    python3 signal_tracker_v2.py --report     # print report, skip update
    python3 signal_tracker_v2.py --csv        # also save CSV report
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

try:
    import pandas as pd
    import yfinance as yf
except ImportError as e:
    print(f"ERROR: Missing dependency — {e}")
    print("Run: pip install pandas yfinance openpyxl --break-system-packages")
    sys.exit(1)

# ── Paths — all reads from output/, all writes to output/signal_tracking/ ────
BASE_DIR     = Path(__file__).parent
OUTPUT_DIR   = BASE_DIR / "output"
TRACKING_DIR = OUTPUT_DIR / "signal_tracking"
LOG_FILE     = TRACKING_DIR / "signal_log.json"

FORWARD_WINDOWS = [1, 3, 5, 10, 20]   # trading days


# ─── Log I/O ──────────────────────────────────────────────────────────────────

def load_log() -> dict:
    if LOG_FILE.exists():
        with open(LOG_FILE) as f:
            return json.load(f)
    return {}


def save_log(log: dict) -> None:
    TRACKING_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2, default=str)


# ─── Source file finders ──────────────────────────────────────────────────────

def latest_v3() -> Path | None:
    files = sorted(glob.glob(str(OUTPUT_DIR / "v3" / "v3_*.xlsx")))
    return Path(files[-1]) if files else None


def latest_snapshot() -> Path | None:
    files = sorted(glob.glob(str(OUTPUT_DIR / "snapshots" / "snapshot_*.csv")))
    return Path(files[-1]) if files else None


def latest_trades() -> Path | None:
    files = sorted(glob.glob(str(OUTPUT_DIR / "trades" / "trades_*.csv")))
    return Path(files[-1]) if files else None


# ─── Signal ingestion ─────────────────────────────────────────────────────────

def ingest_v3(path: Path, log: dict, top: int) -> int:
    """
    Read latest v3 xlsx and add any new top-N signals to the log.
    Returns count of newly added signals.
    """
    try:
        df = pd.read_excel(path, engine="openpyxl")
        df.columns = [c.strip() for c in df.columns]
    except Exception as e:
        print(f"  ERROR reading v3: {e}")
        return 0

    # Derive detection timestamp from filename (e.g. v3_2026-06-10_22-15-24.xlsx)
    fname = path.stem  # v3_2026-06-10_22-15-24
    parts = fname.split("_", 1)
    if len(parts) == 2:
        ts_str = parts[1].replace("_", "T").replace("-", ":", 2)
        # ts_str is now like "2026-06-10T22:15:24" — fix: only replace time part hyphens
        # Safer: reconstruct
        date_part, time_part = parts[1][:10], parts[1][11:].replace("-", ":")
        detected_ts = f"{date_part}T{time_part}+00:00"
        detected_date = date_part
    else:
        detected_ts = datetime.now(timezone.utc).isoformat()
        detected_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    added = 0
    for _, row in df.head(top).iterrows():
        ticker    = str(row.get("Ticker", "")).strip().upper()
        timeframe = str(row.get("Timeframe", "")).strip()
        outfit    = str(row.get("Outfit", "")).strip()

        if not ticker or not timeframe:
            continue

        # Key: ticker + timeframe + outfit — tracks first detection of this combo
        key = f"{ticker}|{timeframe}|{outfit}"

        if key in log:
            continue   # already tracked, never overwrite

        # Entry price: prefer "Entry" column, fall back to "PARM Price"
        entry_price = None
        for col in ("Entry", "PARM Price"):
            val = row.get(col)
            if val and pd.notna(val):
                try:
                    entry_price = float(val)
                    break
                except (ValueError, TypeError):
                    pass

        log[key] = {
            "ticker":        ticker,
            "timeframe":     timeframe,
            "outfit":        outfit,
            "outfit_name":   str(row.get("Periods", "")).strip(),
            "grade":         str(row.get("Grade", "")).strip(),
            "score":         float(row.get("Score", 0) or 0),
            "rank":          int(row.get("Rank", 0) or 0),
            "entry_price":   entry_price,
            "detected_ts":   detected_ts,
            "detected_date": detected_date,
            "source":        "v3",
            "forward":       {str(w): None for w in FORWARD_WINDOWS},
        }
        added += 1

    return added


def ingest_trades(path: Path, log: dict) -> int:
    """
    Also ingest high-confidence trade signals. These have explicit entry prices.
    """
    try:
        df = pd.read_csv(path)
        df.columns = [c.strip() for c in df.columns]
    except Exception as e:
        return 0

    detected_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    detected_ts   = datetime.now(timezone.utc).isoformat()
    added = 0

    for _, row in df.iterrows():
        ticker    = str(row.get("ticker", "")).strip().upper()
        timeframe = str(row.get("snap_tf", "")).strip()
        outfit    = str(row.get("snap_outfit", "")).strip()
        confidence = str(row.get("confidence", "")).strip()

        if not ticker or confidence not in ("HIGH", "MEDIUM"):
            continue

        key = f"{ticker}|{timeframe}|{outfit}|trade"

        if key in log:
            continue

        entry_price = None
        try:
            entry_price = float(row.get("entry", 0) or 0) or None
        except (ValueError, TypeError):
            pass

        log[key] = {
            "ticker":        ticker,
            "timeframe":     timeframe,
            "outfit":        outfit,
            "outfit_name":   str(row.get("disc_sma", "")),
            "grade":         confidence,
            "score":         float(row.get("best_sharpe", 0) or 0),
            "rank":          0,
            "entry_price":   entry_price,
            "detected_ts":   detected_ts,
            "detected_date": detected_date,
            "source":        "trade",
            "forward":       {str(w): None for w in FORWARD_WINDOWS},
        }
        added += 1

    return added


# ─── Forward return filling ───────────────────────────────────────────────────

def fill_forward_returns(log: dict) -> int:
    """
    For each signal with unfilled forward windows that should be available
    by now, fetch daily close history via yfinance and fill them in.

    Returns count of signals that had at least one window filled.
    """
    today = date.today()

    # Figure out which tickers/signals need updating
    # signal_key -> list of windows that need filling
    to_fill: dict[str, list[int]] = {}
    earliest_dates: dict[str, date] = {}   # ticker -> earliest detection date needing data

    for key, sig in log.items():
        det_str = sig.get("detected_date")
        if not det_str:
            continue
        try:
            det = date.fromisoformat(det_str)
        except ValueError:
            continue

        entry_price = sig.get("entry_price")
        if not entry_price:
            continue

        fwd = sig.get("forward", {})
        unfilled = []
        for w in FORWARD_WINDOWS:
            if fwd.get(str(w)) is None:
                # Only try to fill if enough calendar days have passed
                # +1 trading day ~ 2 calendar days, +20 ~ 30 calendar days
                cal_days_needed = max(2, int(w * 1.5))
                if (today - det).days >= cal_days_needed:
                    unfilled.append(w)

        if unfilled:
            to_fill[key] = unfilled
            ticker = sig["ticker"]
            if ticker not in earliest_dates or det < earliest_dates[ticker]:
                earliest_dates[ticker] = det

    if not to_fill:
        return 0

    # Batch-download daily closes for all affected tickers
    # Group tickers by earliest needed date, download in batches of 50
    tickers_needed = list(earliest_dates.keys())
    min_date       = min(earliest_dates.values()) - timedelta(days=1)
    end_date       = today + timedelta(days=1)

    print(f"  Fetching daily history for {len(tickers_needed)} ticker(s) ...")
    hist_by_ticker: dict[str, pd.Series] = {}

    BATCH = 50
    for i in range(0, len(tickers_needed), BATCH):
        batch = tickers_needed[i : i + BATCH]
        try:
            raw = yf.download(
                batch,
                start=str(min_date),
                end=str(end_date),
                interval="1d",
                progress=False,
                auto_adjust=True,
                threads=True,
            )
            if raw is None or raw.empty:
                continue

            if isinstance(raw.columns, pd.MultiIndex):
                # Multiple tickers: (field, ticker) columns
                if "Close" in raw.columns.get_level_values(0):
                    closes_df = raw["Close"]
                    for t in batch:
                        if t in closes_df.columns:
                            s = closes_df[t].dropna()
                            if not s.empty:
                                hist_by_ticker[t] = s
            else:
                # Single ticker in batch
                if "Close" in raw.columns:
                    s = raw["Close"].dropna()
                    if not s.empty:
                        hist_by_ticker[batch[0]] = s

        except Exception as e:
            logging.debug(f"Batch download error ({batch[:3]}...): {e}")
            continue

    # Fill in forward returns for each signal
    updated = 0
    for key, unfilled_windows in to_fill.items():
        sig    = log[key]
        ticker = sig["ticker"]
        closes = hist_by_ticker.get(ticker)
        if closes is None or closes.empty:
            continue

        det_str = sig["detected_date"]
        det_ts  = pd.Timestamp(det_str).tz_localize(None)

        # Filter to trading days strictly after detection date
        idx = pd.to_datetime(closes.index).tz_localize(None) if closes.index.tz else pd.to_datetime(closes.index)
        after_mask = idx > det_ts
        after = closes[after_mask.values]

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


# ─── Reporting ────────────────────────────────────────────────────────────────

def _pct(entry: float | None, fwd_price: float | None) -> float | None:
    if entry and fwd_price and entry > 0:
        return round((fwd_price - entry) / entry * 100, 2)
    return None


def _pct_str(pct: float | None) -> str:
    if pct is None:
        return "  —   "
    arrow = "↑" if pct > 0 else ("↓" if pct < 0 else "→")
    return f"{arrow}{abs(pct):5.2f}%"


def _pct_flag(pct: float | None) -> str:
    """ANSI color for terminal."""
    if pct is None:
        return "\033[2m  —   \033[0m"
    color = "\033[92m" if pct > 0 else ("\033[91m" if pct < 0 else "\033[93m")
    arrow = "↑" if pct > 0 else ("↓" if pct < 0 else "→")
    return f"{color}{arrow}{abs(pct):5.2f}%\033[0m"


def print_report(log: dict, top: int = 50) -> None:
    """Print forward return table sorted by detection date then rank."""
    if not log:
        print("  No signals tracked yet.")
        return

    signals = sorted(
        log.values(),
        key=lambda s: (s.get("detected_date", ""), s.get("rank", 9999)),
    )

    # Summary stats
    all_pcts: dict[int, list[float]] = {w: [] for w in FORWARD_WINDOWS}
    for s in signals:
        fwd = s.get("forward", {})
        ep  = s.get("entry_price")
        for w in FORWARD_WINDOWS:
            p = _pct(ep, fwd.get(str(w)))
            if p is not None:
                all_pcts[w].append(p)

    print()
    print("═" * 95)
    print("  SIGNAL FORWARD RETURN TRACKER")
    print("═" * 95)
    print(f"  {'Ticker':<7} {'TF':<5} {'Grade':<6} {'Score':>6}  "
          f"{'Entry':>7}  {'Detected':<11}  "
          f"{'  +1d':>7}  {'  +3d':>7}  {'  +5d':>7}  "
          f"{'  +10d':>7}  {'  +20d':>7}")
    print("  " + "─" * 91)

    shown = 0
    for s in signals[:top]:
        ep  = s.get("entry_price")
        fwd = s.get("forward", {})
        ep_str = f"${ep:.2f}" if ep else "    —"

        pcts = [_pct(ep, fwd.get(str(w))) for w in FORWARD_WINDOWS]

        print(
            f"  {s['ticker']:<7} {s['timeframe']:<5} {s.get('grade',''):<6} "
            f"{s.get('score', 0):>6.1f}  "
            f"{ep_str:>7}  {s.get('detected_date',''):<11}  "
            + "  ".join(_pct_flag(p) for p in pcts)
        )
        shown += 1

    print("  " + "─" * 91)
    print(f"  Showing {shown} of {len(signals)} tracked signals\n")

    # Summary row
    print(f"  {'Avg return':>45}", end="")
    for w in FORWARD_WINDOWS:
        vals = all_pcts[w]
        if vals:
            avg = sum(vals) / len(vals)
            wr  = sum(1 for v in vals if v > 0) / len(vals) * 100
            print(f"  {_pct_flag(avg)}", end="")
        else:
            print(f"  {'  —   ':>7}", end="")
    print()

    print(f"  {'Win rate':>45}", end="")
    for w in FORWARD_WINDOWS:
        vals = all_pcts[w]
        if vals:
            wr = sum(1 for v in vals if v > 0) / len(vals) * 100
            col = "\033[92m" if wr >= 60 else ("\033[91m" if wr < 40 else "\033[93m")
            print(f"  {col}{wr:5.1f}% \033[0m", end="")
        else:
            print(f"  {'  —   ':>7}", end="")
    print()

    print(f"  {'Sample size':>45}", end="")
    for w in FORWARD_WINDOWS:
        n = len(all_pcts[w])
        print(f"  {'n='+str(n):>7}", end="")
    print("\n")
    print("═" * 95)


def save_report_csv(log: dict) -> Path:
    """Save the full signal log as a flat CSV."""
    TRACKING_DIR.mkdir(parents=True, exist_ok=True)
    today_str = date.today().isoformat()
    out_path  = TRACKING_DIR / f"performance_{today_str}.csv"

    rows = []
    for s in sorted(log.values(),
                    key=lambda x: (x.get("detected_date", ""), x.get("rank", 9999))):
        ep  = s.get("entry_price")
        fwd = s.get("forward", {})
        row = {
            "ticker":         s["ticker"],
            "timeframe":      s["timeframe"],
            "outfit":         s["outfit"],
            "grade":          s.get("grade", ""),
            "score":          s.get("score", ""),
            "rank":           s.get("rank", ""),
            "entry_price":    ep,
            "detected_date":  s.get("detected_date", ""),
            "source":         s.get("source", ""),
        }
        for w in FORWARD_WINDOWS:
            price = fwd.get(str(w))
            pct   = _pct(ep, price)
            row[f"price_{w}d"]  = price
            row[f"pct_{w}d"]    = pct
        rows.append(row)

    if rows:
        pd.DataFrame(rows).to_csv(out_path, index=False)

    return out_path


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="SMA Engine — Signal Forward Return Tracker v2"
    )
    parser.add_argument("--top",    type=int, default=100,
                        help="Top N signals to ingest from each v3 run (default: 100)")
    parser.add_argument("--report", action="store_true",
                        help="Print report only — skip ingestion and price fetch")
    parser.add_argument("--csv",    action="store_true",
                        help="Also save a CSV report to output/signal_tracking/")
    parser.add_argument("--prune",  type=int, default=60,
                        help="Drop signals older than N days (default: 60)")
    args = parser.parse_args()

    TRACKING_DIR.mkdir(parents=True, exist_ok=True)

    print()
    print("═" * 60)
    print("  SIGNAL TRACKER v2")
    print("═" * 60)

    # ── Load log ──────────────────────────────────────────────────────────────
    log = load_log()
    print(f"  Signals in log: {len(log)}")

    if not args.report:

        # ── Ingest new signals ────────────────────────────────────────────────
        v3_path = latest_v3()
        if v3_path:
            print(f"  v3 source:  {v3_path.name}")
            added = ingest_v3(v3_path, log, args.top)
            print(f"  New from v3: {added}")
        else:
            print("  WARNING: No v3 output found. Run engine first.")

        tr_path = latest_trades()
        if tr_path:
            added_tr = ingest_trades(tr_path, log)
            if added_tr:
                print(f"  New from trades: {added_tr}")

        # ── Prune old signals ─────────────────────────────────────────────────
        cutoff = date.today() - timedelta(days=args.prune)
        before = len(log)
        log = {
            k: v for k, v in log.items()
            if date.fromisoformat(v.get("detected_date", "2000-01-01")) >= cutoff
        }
        pruned = before - len(log)
        if pruned:
            print(f"  Pruned: {pruned} signals older than {args.prune} days")

        # ── Fill forward returns ───────────────────────────────────────────────
        updated = fill_forward_returns(log)
        print(f"  Windows filled: {updated} signal(s) updated")

        # ── Save log ──────────────────────────────────────────────────────────
        save_log(log)
        print(f"  Log saved → {LOG_FILE.relative_to(BASE_DIR)}")

    # ── Print report ──────────────────────────────────────────────────────────
    print_report(log, top=args.top)

    # ── Optional CSV ──────────────────────────────────────────────────────────
    if args.csv:
        csv_path = save_report_csv(log)
        print(f"  CSV saved → {csv_path.relative_to(BASE_DIR)}")

    print()


if __name__ == "__main__":
    main()
