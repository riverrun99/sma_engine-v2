"""
signal_tracker_triangulated.py — Triangulated Signal Tracker
=============================================================
Combines all engine outputs to find high-conviction signals appearing
in 2+ of: main engine / V3 / normalized / discovery / backtest.

Sources (all read-only):
  output/snapshots/snapshot_*.csv        → main engine (rank, score, convergence)
  output/v3/v3_*.xlsx                    → V3 state (STRONG/HOLD), score, entry price
  output/normalized_engine/normalized_*.xlsx  → normalized score, convergence
  output/confluence/confluence_*.csv     → discovery signal, backtest win rate / Sharpe
  output/backtest_*.csv                  → best win rate and Sharpe per ticker

Log:    output/signal_tracking/triangulated_signal_log.json

Scoring:
  +2.0  V3 STRONG
  +1.0  V3 HOLD
  +1.5  in main snapshot (top-N)
  +1.0  in normalized top-N
  +1.0  has discovery signal
  +1.5  backtest win rate >= 70%
  +0.75 backtest win rate 60-70%
  +0.5  Sharpe >= 10
  Minimum score to track: 2.5

Safety:
  - NEVER writes to any engine output file
  - All writes go to output/signal_tracking/ only

Usage:
    python3 signal_tracker_triangulated.py              # update + print report
    python3 signal_tracker_triangulated.py --top 30     # track top N by tri-score
    python3 signal_tracker_triangulated.py --report     # print only, no update
    python3 signal_tracker_triangulated.py --csv        # also save CSV
    python3 signal_tracker_triangulated.py --min-score 3.0   # raise minimum score
    python3 signal_tracker_triangulated.py --prune 60
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
LOG_FILE     = TRACKING_DIR / "triangulated_signal_log.json"

FORWARD_WINDOWS = [1, 3, 5, 10, 20]
MIN_SCORE_DEFAULT = 2.5


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


# ── Source file finders ───────────────────────────────────────────────────────

def _latest(pattern: str) -> Path | None:
    files = sorted(glob.glob(pattern))
    return Path(files[-1]) if files else None


def latest_snapshot()   -> Path | None:
    return _latest(str(OUTPUT_DIR / "snapshots" / "snapshot_*.csv"))

def latest_v3()         -> Path | None:
    return _latest(str(OUTPUT_DIR / "v3" / "v3_*.xlsx"))

def latest_normalized() -> Path | None:
    return _latest(str(OUTPUT_DIR / "normalized_engine" / "normalized_*.xlsx"))

def latest_confluence() -> Path | None:
    return _latest(str(OUTPUT_DIR / "confluence" / "confluence_*.csv"))

def latest_backtest()   -> Path | None:
    return _latest(str(OUTPUT_DIR / "backtest_*.csv"))


# ── Source loaders ────────────────────────────────────────────────────────────

def load_snapshot(path: Path, top: int) -> dict[str, dict]:
    """Returns {ticker: {rank, score, convergence, timeframe, outfit}}"""
    out = {}
    try:
        df = pd.read_csv(path)
        df.columns = [c.strip() for c in df.columns]
        if "score" in df.columns:
            df = df.sort_values("score", ascending=False)
        for rank, (_, row) in enumerate(df.head(top).iterrows(), start=1):
            t = str(row.get("ticker", "")).strip().upper()
            if t:
                out[t] = {
                    "snap_rank":        rank,
                    "snap_score":       float(row.get("score", 0) or 0),
                    "snap_convergence": str(row.get("convergence", "")),
                    "snap_timeframe":   str(row.get("timeframe", "")),
                    "snap_outfit":      str(row.get("outfit", "")),
                }
    except Exception as e:
        logging.warning(f"Snapshot load error: {e}")
    return out


def load_v3(path: Path) -> dict[str, dict]:
    """Returns {ticker: {v3_state, v3_score, v3_grade, v3_entry, v3_timeframe}}"""
    out = {}
    try:
        df = pd.read_excel(path, engine="openpyxl")
        df.columns = [c.strip() for c in df.columns]
        # Best row per ticker by Score
        df["_score"] = pd.to_numeric(df.get("Score", pd.Series(dtype=float)), errors="coerce").fillna(0)
        for ticker, grp in df.groupby("Ticker"):
            best = grp.loc[grp["_score"].idxmax()]
            state = str(best.get("State", "")).strip().upper()
            if state in ("STRONG", "HOLD", "WEAK"):
                entry = None
                for col in ("Entry", "PARM Price"):
                    val = best.get(col)
                    if val and pd.notna(val):
                        try:
                            entry = float(val)
                            break
                        except (ValueError, TypeError):
                            pass
                out[str(ticker).strip().upper()] = {
                    "v3_state":     state,
                    "v3_score":     float(best.get("Score", 0) or 0),
                    "v3_grade":     str(best.get("Grade", "")),
                    "v3_entry":     entry,
                    "v3_timeframe": str(best.get("Timeframe", "")),
                    "v3_outfit":    str(best.get("Outfit", "")),
                }
    except Exception as e:
        logging.warning(f"V3 load error: {e}")
    return out


def load_normalized(path: Path, top: int) -> dict[str, dict]:
    """Returns {ticker: {norm_score, norm_convergence}} for top-N by norm score."""
    out = {}
    try:
        df = pd.read_excel(path, engine="openpyxl")
        df.columns = [c.strip() for c in df.columns]
        if "Norm Score" in df.columns:
            df = df.sort_values("Norm Score", ascending=False)
        for _, row in df.head(top).iterrows():
            t = str(row.get("Ticker", "")).strip().upper()
            if t and t not in out:
                out[t] = {
                    "norm_score":       float(row.get("Norm Score", 0) or 0),
                    "norm_convergence": str(row.get("Convergence", "")),
                }
    except Exception as e:
        logging.warning(f"Normalized load error: {e}")
    return out


def load_confluence(path: Path) -> dict[str, dict]:
    """Returns {ticker: {disc_tf, disc_sma, disc_direction, best_sharpe, best_win_rate}}"""
    out = {}
    try:
        df = pd.read_csv(path)
        df.columns = [c.strip() for c in df.columns]
        for _, row in df.iterrows():
            t = str(row.get("ticker", "")).strip().upper()
            if not t:
                continue
            # Only store if has discovery or backtest
            in_disc   = str(row.get("in_discovery", "False")).lower() == "true"
            in_bt     = str(row.get("in_backtest",  "False")).lower() == "true"
            if not in_disc and not in_bt:
                continue
            win_rate = None
            sharpe   = None
            try:
                wr = row.get("best_win_rate")
                if wr and pd.notna(wr):
                    win_rate = float(wr)
            except (ValueError, TypeError):
                pass
            try:
                sh = row.get("best_sharpe")
                if sh and pd.notna(sh):
                    sharpe = float(sh)
            except (ValueError, TypeError):
                pass
            out[t] = {
                "in_discovery":     in_disc,
                "in_backtest":      in_bt,
                "disc_tf":          str(row.get("discovery_tf", "")),
                "disc_sma":         str(row.get("discovery_sma", "")),
                "disc_direction":   str(row.get("discovery_direction", "")),
                "best_win_rate":    win_rate,
                "best_sharpe":      sharpe,
            }
    except Exception as e:
        logging.warning(f"Confluence load error: {e}")
    return out


# ── Triangulation ─────────────────────────────────────────────────────────────

def triangulate(
    snap:   dict[str, dict],
    v3:     dict[str, dict],
    norm:   dict[str, dict],
    conf:   dict[str, dict],
    min_score: float,
) -> list[dict]:
    """
    Cross-reference all sources and build a scored signal list.
    Only returns signals with tri_score >= min_score.
    """
    all_tickers = set(snap) | set(v3) | set(norm) | set(conf)
    results = []

    for ticker in all_tickers:
        score = 0.0
        snap_data = snap.get(ticker, {})
        v3_data   = v3.get(ticker, {})
        norm_data = norm.get(ticker, {})
        conf_data = conf.get(ticker, {})

        # Score contributions
        if snap_data:
            score += 1.5

        v3_state = v3_data.get("v3_state", "")
        if v3_state == "STRONG":
            score += 2.0
        elif v3_state == "HOLD":
            score += 1.0

        if norm_data:
            score += 1.0

        if conf_data.get("in_discovery"):
            score += 1.0

        win_rate = conf_data.get("best_win_rate")
        if win_rate is not None:
            if win_rate >= 0.70:
                score += 1.5
            elif win_rate >= 0.60:
                score += 0.75

        sharpe = conf_data.get("best_sharpe")
        if sharpe is not None and sharpe >= 10:
            score += 0.5

        if score < min_score:
            continue

        # Engine count: main / V3 / normalized
        eng_count = sum([bool(snap_data), bool(v3_data), bool(norm_data)])

        results.append({
            "ticker":         ticker,
            "tri_score":      round(score, 2),
            "eng_count":      eng_count,
            # main engine
            "snap_rank":      snap_data.get("snap_rank"),
            "snap_score":     snap_data.get("snap_score"),
            "snap_tf":        snap_data.get("snap_timeframe", ""),
            "snap_outfit":    snap_data.get("snap_outfit", ""),
            "snap_conv":      snap_data.get("snap_convergence", ""),
            # V3
            "v3_state":       v3_state,
            "v3_score":       v3_data.get("v3_score"),
            "v3_grade":       v3_data.get("v3_grade", ""),
            "v3_entry":       v3_data.get("v3_entry"),
            "v3_tf":          v3_data.get("v3_timeframe", ""),
            # normalized
            "norm_score":     norm_data.get("norm_score"),
            "norm_conv":      norm_data.get("norm_convergence", ""),
            # discovery / backtest
            "in_discovery":   conf_data.get("in_discovery", False),
            "disc_tf":        conf_data.get("disc_tf", ""),
            "disc_sma":       conf_data.get("disc_sma", ""),
            "disc_direction": conf_data.get("disc_direction", ""),
            "best_win_rate":  win_rate,
            "best_sharpe":    sharpe,
        })

    results.sort(key=lambda x: x["tri_score"], reverse=True)
    return results


# ── Signal ingestion ──────────────────────────────────────────────────────────

def ingest_triangulated(signals: list[dict], log: dict, detected_date: str, detected_ts: str) -> int:
    """
    Add new triangulated signals to the log.
    Key: ticker|date — re-fires if the same ticker appears on a new day with a new signal.
    Returns count of newly added signals.
    """
    added = 0
    for sig in signals:
        ticker = sig["ticker"]
        # Key includes date so the same ticker can fire again after a gap
        key = f"{ticker}|{detected_date}"
        if key in log:
            continue

        # Entry price: prefer V3 entry
        entry_price = sig.get("v3_entry")

        log[key] = {
            "ticker":         ticker,
            "detected_date":  detected_date,
            "detected_ts":    detected_ts,
            "tri_score":      sig["tri_score"],
            "eng_count":      sig["eng_count"],
            "snap_tf":        sig.get("snap_tf", ""),
            "snap_outfit":    sig.get("snap_outfit", ""),
            "snap_conv":      sig.get("snap_conv", ""),
            "v3_state":       sig.get("v3_state", ""),
            "v3_grade":       sig.get("v3_grade", ""),
            "v3_tf":          sig.get("v3_tf", ""),
            "norm_conv":      sig.get("norm_conv", ""),
            "in_discovery":   sig.get("in_discovery", False),
            "disc_tf":        sig.get("disc_tf", ""),
            "disc_sma":       sig.get("disc_sma", ""),
            "disc_direction": sig.get("disc_direction", ""),
            "best_win_rate":  sig.get("best_win_rate"),
            "best_sharpe":    sig.get("best_sharpe"),
            "entry_price":    entry_price,
            "source":         "triangulated",
            "forward":        {str(w): None for w in FORWARD_WINDOWS},
        }
        added += 1

    return added


# ── Forward return fill ───────────────────────────────────────────────────────

def fill_forward_returns(log: dict) -> int:
    """Fill entry prices and forward windows. Returns count of signals updated."""
    today = date.today()

    to_fill: dict[str, list[int]] = {}
    earliest_dates: dict[str, date] = {}

    for key, sig in log.items():
        det_str = sig.get("detected_date")
        if not det_str:
            continue
        try:
            det = date.fromisoformat(det_str)
        except ValueError:
            continue

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

    if not to_fill:
        return 0

    if _webull_client is None:
        print("  Skipping price fill — Webull client unavailable")
        return 0

    min_date  = min(earliest_dates.values())
    days_back = (today - min_date).days + 5
    print(f"  Fetching daily history for {len(earliest_dates)} ticker(s) ...")

    hist_by_ticker: dict[str, "pd.Series"] = {}
    for ticker in earliest_dates:
        try:
            df = _webull_client.fetch_bars(ticker, "1d", min(max(days_back, 30), 999))
            if df is None or df.empty:
                continue
            df["date"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None).dt.normalize()
            s = df.set_index("date")["close"]
            s.index = pd.to_datetime(s.index)
            if not s.empty:
                hist_by_ticker[ticker] = s
        except Exception as e:
            logging.debug(f"Webull fetch error ({ticker}): {e}")

    updated = 0
    for key, unfilled_windows in to_fill.items():
        sig    = log[key]
        ticker = sig["ticker"]
        closes = hist_by_ticker.get(ticker)
        if closes is None or closes.empty:
            continue

        det_ts = pd.Timestamp(sig["detected_date"]).tz_localize(None)
        idx    = (pd.to_datetime(closes.index).tz_localize(None)
                  if closes.index.tz else pd.to_datetime(closes.index))
        after  = closes[idx > det_ts]

        # Fill entry price from close on or nearest to detection date
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
        print("  No triangulated signals tracked yet.")
        return

    signals = sorted(
        log.values(),
        key=lambda s: (s.get("detected_date", ""), -s.get("tri_score", 0)),
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
    print("═" * 115)
    print("  TRIANGULATED SIGNAL TRACKER")
    print("═" * 115)
    print(f"  {'Ticker':<8} {'Score':>5}  {'Eng':>4}  {'V3':>8}  {'Norm':>5}  "
          f"{'Disc':>12}  {'WR':>5}  {'Entry':>7}  {'Detected':<11}  "
          f"{'  +1d':>7}  {'  +3d':>7}  {'  +5d':>7}  {'  +10d':>7}  {'  +20d':>7}")
    print("  " + "─" * 111)

    for s in signals[:top]:
        ep     = s.get("entry_price")
        fwd    = s.get("forward", {})
        ep_str = f"${ep:.2f}" if ep else "    —"
        wr     = s.get("best_win_rate")
        wr_str = f"{wr*100:.0f}%" if wr is not None else " —"
        disc   = f"{s.get('disc_tf','')} {s.get('disc_sma','')}" if s.get("in_discovery") else "—"
        pcts   = [_pct(ep, fwd.get(str(w))) for w in FORWARD_WINDOWS]

        print(
            f"  {s['ticker']:<8} {s.get('tri_score', 0):>5.1f}  "
            f"{s.get('eng_count', 0):>3}/3  "
            f"{s.get('v3_state', '—'):>8}  "
            f"{s.get('norm_conv', '—'):>5}  "
            f"{disc:>12}  "
            f"{wr_str:>5}  "
            f"{ep_str:>7}  "
            f"{s.get('detected_date', ''):11}  "
            + "  ".join(_pct_flag(p) for p in pcts)
        )

    print("  " + "─" * 111)
    print(f"  Showing {min(top, len(signals))} of {len(signals)} tracked signals\n")

    print(f"  {'Avg return':>65}", end="")
    for w in FORWARD_WINDOWS:
        vals = all_pcts[w]
        print(f"  {_pct_flag(sum(vals)/len(vals) if vals else None)}", end="")
    print()

    print(f"  {'Win rate':>65}", end="")
    for w in FORWARD_WINDOWS:
        vals = all_pcts[w]
        if vals:
            wr  = sum(1 for v in vals if v > 0) / len(vals) * 100
            col = "\033[92m" if wr >= 60 else ("\033[91m" if wr < 40 else "\033[93m")
            print(f"  {col}{wr:5.1f}% \033[0m", end="")
        else:
            print(f"  {'  —   ':>7}", end="")
    print()

    print(f"  {'Sample n':>65}", end="")
    for w in FORWARD_WINDOWS:
        print(f"  {'n='+str(len(all_pcts[w])):>7}", end="")
    print("\n")
    print("═" * 115)


def save_report_csv(log: dict) -> Path:
    TRACKING_DIR.mkdir(parents=True, exist_ok=True)
    out_path = TRACKING_DIR / f"triangulated_performance_{date.today().isoformat()}.csv"
    rows = []
    for s in sorted(log.values(),
                    key=lambda x: (x.get("detected_date", ""), -x.get("tri_score", 0))):
        ep  = s.get("entry_price")
        fwd = s.get("forward", {})
        row = {
            "ticker":        s["ticker"],
            "detected_date": s.get("detected_date", ""),
            "tri_score":     s.get("tri_score", ""),
            "eng_count":     s.get("eng_count", ""),
            "v3_state":      s.get("v3_state", ""),
            "snap_tf":       s.get("snap_tf", ""),
            "disc_sma":      s.get("disc_sma", ""),
            "disc_direction":s.get("disc_direction", ""),
            "best_win_rate": s.get("best_win_rate", ""),
            "best_sharpe":   s.get("best_sharpe", ""),
            "entry_price":   ep,
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
    parser = argparse.ArgumentParser(description="Triangulated Signal Tracker")
    parser.add_argument("--top",       type=int,   default=50,
                        help="Top N signals to ingest by tri-score (default: 50)")
    parser.add_argument("--snap-top",  type=int,   default=100,
                        help="Top N snapshot rows to consider (default: 100)")
    parser.add_argument("--norm-top",  type=int,   default=100,
                        help="Top N normalized rows to consider (default: 100)")
    parser.add_argument("--min-score", type=float, default=MIN_SCORE_DEFAULT,
                        help=f"Minimum tri-score to log (default: {MIN_SCORE_DEFAULT})")
    parser.add_argument("--report",    action="store_true",
                        help="Print report only — skip ingestion and price fetch")
    parser.add_argument("--csv",       action="store_true",
                        help="Also save CSV report")
    parser.add_argument("--prune",     type=int,   default=60,
                        help="Drop signals older than N days (default: 60)")
    args = parser.parse_args()

    TRACKING_DIR.mkdir(parents=True, exist_ok=True)

    print()
    print("═" * 60)
    print("  SIGNAL TRACKER — TRIANGULATED")
    print("═" * 60)

    log = load_log()
    print(f"  Signals in log: {len(log)}")

    if not args.report:
        # ── Load all sources ──────────────────────────────────────────────────
        snap_path = latest_snapshot()
        v3_path   = latest_v3()
        norm_path = latest_normalized()
        conf_path = latest_confluence()

        print(f"  Snapshot:   {snap_path.name if snap_path else '—'}")
        print(f"  V3:         {v3_path.name   if v3_path   else '—'}")
        print(f"  Normalized: {norm_path.name if norm_path else '—'}")
        print(f"  Confluence: {conf_path.name if conf_path else '—'}")

        snap = load_snapshot(snap_path, args.snap_top) if snap_path else {}
        v3   = load_v3(v3_path)                        if v3_path   else {}
        norm = load_normalized(norm_path, args.norm_top) if norm_path else {}
        conf = load_confluence(conf_path)               if conf_path else {}

        print(f"  Main tickers: {len(snap)}  V3: {len(v3)}  Norm: {len(norm)}  Confluence: {len(conf)}")

        # ── Triangulate ───────────────────────────────────────────────────────
        signals = triangulate(snap, v3, norm, conf, args.min_score)
        print(f"  Triangulated signals (score >= {args.min_score}): {len(signals)}")

        # Detection timestamp from snapshot filename
        if snap_path:
            fname = snap_path.stem
            parts = fname.split("_", 1)
            if len(parts) == 2:
                date_part    = parts[1][:10]
                time_part    = parts[1][11:].replace("-", ":")
                detected_ts  = f"{date_part}T{time_part}+00:00"
                detected_date = date_part
            else:
                detected_ts   = datetime.now(timezone.utc).isoformat()
                detected_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        else:
            detected_ts   = datetime.now(timezone.utc).isoformat()
            detected_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        added = ingest_triangulated(signals[:args.top], log, detected_date, detected_ts)
        print(f"  New signals logged: {added}")

        # ── Prune ─────────────────────────────────────────────────────────────
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

        save_log(log)
        print(f"  Log saved → {LOG_FILE.relative_to(BASE_DIR)}")

    print_report(log, top=args.top)

    if args.csv:
        csv_path = save_report_csv(log)
        print(f"  CSV saved → {csv_path.relative_to(BASE_DIR)}")

    print()


if __name__ == "__main__":
    main()
