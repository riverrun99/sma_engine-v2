"""
sheets_sync.py — Writes all engine output categories to Google Sheets.
=====================================================================
Tabs written:
  Current       — main engine snapshot (read from signals_current.xlsx)
  Discovery     — latest discovery engine output
  Confluence    — latest confluence engine output
  Backtest      — latest backtest results
  Triangulation — top triangulated signals (overlay composite)
  Overlay       — The System + Zero Gamma snapshot

Run standalone:  python3 sheets_sync.py
Or import and call sync_all() from overlay.py after each refresh.

Never modifies the original engine code or sheets_writer.py.
"""

import os
import sys
import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

# Load .env from parent folder
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

OUTPUT_DIR   = Path(__file__).parent.parent / "output"
CREDS_PATH   = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_PATH", "")
SHEET_ID     = os.environ.get("GOOGLE_SHEET_ID", "")
SCOPES       = ["https://www.googleapis.com/auth/spreadsheets"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("sheets_sync")


# ── Google Sheets client ──────────────────────────────────────────────────────

def _build_service():
    try:
        from googleapiclient.discovery import build
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(
            CREDS_PATH, scopes=SCOPES)
        svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        # Quick connection test
        svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
        return svc
    except Exception as e:
        log.warning(f"Sheets connection failed: {e}")
        return None


def _ensure_tabs(svc, tab_names: list[str]):
    try:
        meta     = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
        existing = {s["properties"]["title"] for s in meta.get("sheets", [])}
        requests = [{"addSheet": {"properties": {"title": t}}}
                    for t in tab_names if t not in existing]
        if requests:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"requests": requests}).execute()
            log.info(f"Created tabs: {[r['addSheet']['properties']['title'] for r in requests]}")
    except Exception as e:
        log.warning(f"Tab creation error: {e}")


def _write_tab(svc, tab: str, rows: list[list]):
    """Clear tab and write rows."""
    try:
        svc.spreadsheets().values().clear(
            spreadsheetId=SHEET_ID,
            range=f"{tab}!A1:Z5000").execute()
        if rows:
            svc.spreadsheets().values().update(
                spreadsheetId=SHEET_ID,
                range=f"{tab}!A1",
                valueInputOption="RAW",
                body={"values": rows}).execute()
        log.info(f"  ✓ {tab}: {len(rows)} rows written")
    except Exception as e:
        log.warning(f"  ✗ {tab}: write failed — {e}")


# ── Data readers ──────────────────────────────────────────────────────────────

def _latest_csv(folder: str) -> Path | None:
    d = OUTPUT_DIR / folder
    if not d.exists():
        return None
    files = sorted(d.glob("*.csv"))
    return files[-1] if files else None


def _read_csv(path: Path) -> list[list]:
    if not path or not path.exists():
        return []
    rows = []
    with open(path, newline="") as f:
        for row in csv.reader(f):
            rows.append(row)
    return rows


def _latest_root_csv(prefix: str) -> Path | None:
    files = sorted(OUTPUT_DIR.glob(f"{prefix}*.csv"))
    return files[-1] if files else None


# ── Tab builders ──────────────────────────────────────────────────────────────

def build_current_rows() -> list[list]:
    """Read signals_current.xlsx and return all rows for the Current tab."""
    xlsx_path = OUTPUT_DIR / "signals_current.xlsx"
    if not xlsx_path.exists():
        return [["No signals_current.xlsx found"]]
    try:
        import openpyxl
        wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
        ws = wb.active
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append([("" if v is None else v) for v in row])
        wb.close()
        return rows
    except Exception as e:
        return [["Error reading signals_current.xlsx"], [str(e)]]


def build_discovery_rows() -> list[list]:
    path = _latest_csv("discovery")
    rows = _read_csv(path)
    if not rows:
        return [["No discovery data found"]]
    header_extra = [["DISCOVERY ENGINE — Latest Output"],
                    [f"File: {path.name if path else '—'}"],
                    [f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"],
                    []]
    return header_extra + rows


def build_confluence_rows() -> list[list]:
    path = _latest_csv("confluence")
    rows = _read_csv(path)
    if not rows:
        return [["No confluence data found"]]
    header_extra = [["CONFLUENCE ENGINE — Latest Output"],
                    [f"File: {path.name if path else '—'}"],
                    [f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"],
                    []]
    return header_extra + rows


def build_backtest_rows() -> list[list]:
    path = _latest_root_csv("backtest_")
    rows = _read_csv(path)
    if not rows:
        return [["No backtest data found"]]
    header_extra = [["BACKTEST — Latest Output"],
                    [f"File: {path.name if path else '—'}"],
                    [f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"],
                    []]
    return header_extra + rows


def build_trades_rows() -> list[list]:
    path = _latest_csv("trades")
    rows = _read_csv(path)
    if not rows:
        # Try root-level trades files
        path = _latest_root_csv("trades_")
        rows = _read_csv(path)
    if not rows:
        return [["No trade signals found — run trade engine first"]]
    header_extra = [["TRADE ENGINE — Latest Output"],
                    [f"File: {path.name if path else '—'}"],
                    [f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"],
                    []]
    return header_extra + rows


def build_triangulation_rows() -> list[list]:
    """Read triangulation output from the _triangulation_staging folder."""
    try:
        tri_path = Path(__file__).parent.parent / "_triangulation_staging"
        sys.path.insert(0, str(tri_path))
        from triangulator import (
            read_original, read_normalized, read_v3,
            read_discovery, read_confluence, read_trades,
            triangulate,
        )
        orig  = read_original()
        norm  = read_normalized()
        v3    = read_v3()
        disc  = read_discovery()
        conf  = read_confluence()
        trade = read_trades()
        signals = triangulate(orig, norm, v3, disc, conf, trade)

        rows = [
            ["TRIANGULATION — Top Signals"],
            [f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"],
            [],
            ["#", "Ticker", "Score", "Engines", "V3 State", "Norm Conv",
             "Orig Conv", "Disc TF", "Disc SMA", "Entry Price"],
        ]
        shown = [s for s in signals if s.score > 0]
        for i, sig in enumerate(shown, 1):
            rows.append([
                i,
                sig.ticker,
                round(sig.score, 1),
                f"{sig.engines_confirmed}/3",
                sig.v3_state or "—",
                sig.norm_conv or "—",
                sig.orig_conv or "—",
                sig.disc_tf or "—",
                sig.disc_sma or "—",
                f"${sig.trade_entry:.2f}" if sig.trade_entry else "—",
            ])
        return rows
    except Exception as e:
        return [["Triangulation data unavailable"], [str(e)]]


def build_v3_rows() -> list[list]:
    path = _latest_csv("v3")
    rows = _read_csv(path)
    if not rows:
        # Try output/v3/ subfolder with any file type
        v3_dir = OUTPUT_DIR / "v3"
        if v3_dir.exists():
            files = sorted(v3_dir.iterdir())
            if files:
                path = files[-1]
                rows = _read_csv(path)
    if not rows:
        return [["No V3 data found"]]
    header_extra = [["V3 ENGINE — Latest Output"],
                    [f"File: {path.name if path else '—'}"],
                    [f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"],
                    []]
    return header_extra + rows


def build_normalized_rows() -> list[list]:
    path = _latest_csv("normalized")
    rows = _read_csv(path)
    if not rows:
        norm_dir = OUTPUT_DIR / "normalized_engine"
        if norm_dir.exists():
            files = sorted(norm_dir.glob("*.csv"))
            if files:
                path = files[-1]
                rows = _read_csv(path)
    if not rows:
        return [["No normalized engine data found"]]
    header_extra = [["NORMALIZED ENGINE — Latest Output"],
                    [f"File: {path.name if path else '—'}"],
                    [f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"],
                    []]
    return header_extra + rows


def build_overlay_rows(sys_data: dict = None, gex_data: dict = None) -> list[list]:
    """Snapshot of The System + Zero Gamma for the Overlay tab."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = [
        ["MARKET OVERLAY SNAPSHOT"],
        [f"Updated: {now}"],
        [],
    ]
    if sys_data and "error" not in sys_data:
        rows += [
            ["THE SYSTEM"],
            ["State",        sys_data.get("state", "—")],
            ["Vehicle",      sys_data.get("vehicle", "—")],
            ["SPY Close",    sys_data.get("close", "—")],
            ["SMA10",        sys_data.get("sma10", "—")],
            ["SMA50",        sys_data.get("sma50", "—")],
            ["SMA200",       sys_data.get("sma200", "—")],
            ["SMA50 Dir",    sys_data.get("sma50_dir", "—")],
            ["Condition",    sys_data.get("ob_os", "—")],
            ["Signal",       sys_data.get("entry_signal", "—")],
            ["Reason",       sys_data.get("entry_reason", "—")],
            ["Choppy",       sys_data.get("choppy", False)],
            [],
        ]
        ndx = sys_data.get("nasdaq", {})
        if ndx and "error" not in ndx:
            rows += [
                ["NASDAQ (QQQ)"],
                ["State",     ndx.get("state", "—")],
                ["QQQ Close", ndx.get("close", "—")],
                ["Day %",     f"{ndx.get('gap_pct', 0):+.2f}%"],
                ["vs SMA50",  f"{ndx.get('dist_sma50', 0):+.2f}%"],
                ["SMA50 Dir", ndx.get("sma50_dir", "—")],
                [],
            ]
    if gex_data and "error" not in gex_data:
        rows += [
            ["ZERO GAMMA (SPX — Tikitrade)"],
            ["Spot",         gex_data.get("spot", "—")],
            ["Zero Gamma",   gex_data.get("zero_gamma", "—")],
            ["Regime",       gex_data.get("regime", "—")],
            ["Distance %",   f"{gex_data.get('dist_from_zero_pct', 0):+.3f}%"],
            ["Call Wall",    gex_data.get("call_wall", "—")],
            ["Put Wall",     gex_data.get("put_wall", "—")],
            ["Max Pain",     gex_data.get("max_pain", "—")],
            ["Data As Of",   gex_data.get("data_as_of", "—")],
        ]
    return rows


# ── Main sync ─────────────────────────────────────────────────────────────────

TABS = ["Current", "Discovery", "Confluence", "Backtest", "Trades",
        "Normalized", "V3", "Triangulation", "Overlay"]


def sync_all(sys_data: dict = None, gex_data: dict = None):
    """Sync all tabs. Call from overlay after each refresh, or run standalone."""
    if not CREDS_PATH or not SHEET_ID:
        log.warning("GOOGLE_SHEETS_CREDENTIALS_PATH or GOOGLE_SHEET_ID not set — skipping sync")
        return

    svc = _build_service()
    if not svc:
        return

    _ensure_tabs(svc, TABS)

    tab_data = {
        "Current":       build_current_rows,
        "Discovery":     build_discovery_rows,
        "Confluence":    build_confluence_rows,
        "Backtest":      build_backtest_rows,
        "Trades":        build_trades_rows,
        "Normalized":    build_normalized_rows,
        "V3":            build_v3_rows,
        "Triangulation": build_triangulation_rows,
        "Overlay":       lambda: build_overlay_rows(sys_data, gex_data),
    }

    for tab, builder in tab_data.items():
        try:
            rows = builder()
            _write_tab(svc, tab, rows)
        except Exception as e:
            log.warning(f"  ✗ {tab}: builder error — {e}")

    log.info("Sheets sync complete.")


if __name__ == "__main__":
    log.info("Running full sheets sync...")
    sync_all()
