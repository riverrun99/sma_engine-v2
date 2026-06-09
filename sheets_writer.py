"""
sheets_writer.py — Google Sheets two-tab writer.

Writes engine output to a user-owned Google Sheet with two tabs:

  "Current": overwritten every cycle. Snapshot of latest top signal, top-10,
             system states, regime.

  "Log":     append-only. One new row per scan cycle. Schema matches the
             local_writer LOG_COLUMNS so the formats are interchangeable.

Setup required from each subscriber:
  1. Create a Google Cloud project
  2. Enable the Google Sheets API
  3. Create a service account, download its JSON key
  4. Create a blank Google Sheet, copy its Sheet ID from the URL
  5. Share the Sheet with the service account email (Editor permission)
  6. Set GOOGLE_SHEETS_CREDENTIALS_PATH and GOOGLE_SHEET_ID in .env

If Sheets isn't configured, this writer is a no-op (returns enabled=False)
and the engine continues normally using only the local file writer.

Failures during writes are logged and swallowed — Sheets is never allowed
to crash the engine.
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from local_writer import LOG_COLUMNS


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def load_google_clients():
    """Import Google clients only when Sheets is actually enabled."""
    try:
        from googleapiclient.discovery import build
        from google.oauth2 import service_account
        return build, service_account
    except ImportError:
        return None, None


class SheetsWriter:
    """Writes engine state to a Google Sheet with Current + Log tabs."""

    def __init__(
        self,
        credentials_path: Optional[str] = None,
        sheet_id: Optional[str] = None,
        enabled: bool = True,
    ):
        self.credentials_path = credentials_path or os.environ.get(
            "GOOGLE_SHEETS_CREDENTIALS_PATH", "")
        self.sheet_id = sheet_id or os.environ.get("GOOGLE_SHEET_ID", "")
        self.enabled = (
            enabled
            and bool(self.credentials_path)
            and bool(self.sheet_id)
        )
        self._service = None
        self._log_header_ensured = False
        self._build = None
        self._service_account = None

        if self.enabled:
            self._connect()

    def _connect(self) -> None:
        self._build, self._service_account = load_google_clients()
        if self._build is None or self._service_account is None:
            logging.warning(
                "SheetsWriter: Google client libraries not installed - Sheets writes disabled"
            )
            self.enabled = False
            return
        if not Path(self.credentials_path).exists():
            logging.warning(
                f"SheetsWriter: credentials file not found at "
                f"{self.credentials_path} — Sheets writes disabled"
            )
            self.enabled = False
            return
        try:
            self._service = self._build_fresh_service()
            # Test access
            self._service.spreadsheets().get(spreadsheetId=self.sheet_id).execute()
            logging.info(f"SheetsWriter: connected to sheet {self.sheet_id[:10]}...")
        except Exception as e:
            logging.warning(f"SheetsWriter: connection failed ({e}) — disabled")
            self.enabled = False

    def _build_fresh_service(self):
        """Create a new service client with a fresh HTTP connection.

        Called at startup and at the top of every write cycle to prevent
        stale connections causing BrokenPipeError after long idle periods.
        """
        creds = self._service_account.Credentials.from_service_account_file(
            self.credentials_path, scopes=SCOPES)
        return self._build("sheets", "v4", credentials=creds, cache_discovery=False)

    def _ensure_tabs_exist(self) -> None:
        """Create 'Current' and 'Log' tabs if missing. Idempotent."""
        if not self.enabled:
            return
        try:
            meta = self._service.spreadsheets().get(
                spreadsheetId=self.sheet_id).execute()
            existing_titles = {s["properties"]["title"] for s in meta.get("sheets", [])}

            requests = []
            for tab_name in ("Current", "Log"):
                if tab_name not in existing_titles:
                    requests.append({
                        "addSheet": {"properties": {"title": tab_name}}
                    })
            if requests:
                self._service.spreadsheets().batchUpdate(
                    spreadsheetId=self.sheet_id,
                    body={"requests": requests}).execute()
        except Exception as e:
            logging.warning(f"SheetsWriter: tab creation failed: {e}")

    def _ensure_log_header(self) -> None:
        """Write the header row to Log if it's not there already."""
        if not self.enabled or self._log_header_ensured:
            return
        try:
            result = self._service.spreadsheets().values().get(
                spreadsheetId=self.sheet_id,
                range="Log!A1:Z1",
            ).execute()
            existing = result.get("values", [])
            if not existing or existing[0] != LOG_COLUMNS:
                self._service.spreadsheets().values().update(
                    spreadsheetId=self.sheet_id,
                    range="Log!A1",
                    valueInputOption="RAW",
                    body={"values": [LOG_COLUMNS]},
                ).execute()
            self._log_header_ensured = True
        except Exception as e:
            logging.warning(f"SheetsWriter: header write failed: {e}")

    def write_current(
        self,
        signal: Optional[dict],
        top_n: list,
        systems: list,
        regime_label: Optional[str] = None,
        ts: Optional[datetime] = None,
    ) -> None:
        """Overwrite the Current tab with the latest snapshot."""
        if not self.enabled:
            return
        ts = ts or datetime.now(timezone.utc)

        # Build a flat 2D array representing the snapshot layout
        rows = []
        rows.append(["ELEMENT 47 — SMA ENGINE LIVE"])
        rows.append(["Last update:", ts.isoformat()])
        rows.append([])

        # Top signal
        rows.append(["TOP SIGNAL"])
        if signal:
            periods_str = "/".join(str(p) for p in signal.get("outfit_periods", []))
            rows.append(["Ticker", signal.get("ticker", "")])
            rows.append(["Timeframe", signal.get("timeframe", "")])
            rows.append(["Outfit", f"{periods_str} ({signal.get('outfit_name', '')})"])
            rows.append(["Entry price", signal.get("entry_price", "")])
            rows.append(["Offset", signal.get("offset_applied", "")])
            rows.append(["Hit count", signal.get("hit_count", "")])
            rows.append(["Convergence", signal.get("convergence", {}).get("score", "")])
            rows.append(["Risk", signal.get("risk", "")])
        else:
            rows.append(["(no signal detected this cycle)"])
        rows.append([])

        # Ranked leaderboard
        rows.append([f"TOP {len(top_n)} RANKED"])
        rows.append(["Rank", "Ticker", "TF", "Outfit", "Hits", "Conv", "Score"])
        for entry in top_n:
            periods_str = "/".join(str(p) for p in entry.get("outfit_periods", []))
            rows.append([
                entry.get("rank", ""),
                entry.get("ticker", ""),
                entry.get("timeframe", ""),
                periods_str,
                entry.get("hit_count", ""),
                entry.get("convergence", ""),
                entry.get("rank_score", ""),
            ])
        rows.append([])

        # Systems
        rows.append(["SYSTEM STATES"])
        rows.append(["System", "State", "Note"])
        for s in systems:
            rows.append([s.name, s.state.upper(), s.note])
        rows.append([])

        # Regime
        rows.append(["REGIME", regime_label or "(not yet computed)"])

        try:
            self._ensure_tabs_exist()
            # Clear, then write
            self._service.spreadsheets().values().clear(
                spreadsheetId=self.sheet_id,
                range="Current!A1:Z200",
            ).execute()
            self._service.spreadsheets().values().update(
                spreadsheetId=self.sheet_id,
                range="Current!A1",
                valueInputOption="RAW",
                body={"values": rows},
            ).execute()
        except Exception as e:
            logging.warning(f"SheetsWriter: Current write failed: {e}")

    def append_log_row(
        self,
        signal: Optional[dict],
        systems: list,
        regime_label: Optional[str] = None,
        ts: Optional[datetime] = None,
    ) -> None:
        """Append one row to the Log tab."""
        if not self.enabled:
            return
        ts = ts or datetime.now(timezone.utc)
        sys_state = {s.name: s.state for s in systems} if systems else {}

        if signal:
            conv = signal.get("convergence", {})
            periods_str = "/".join(str(p) for p in signal.get("outfit_periods", []))
            row = [
                ts.isoformat(),
                signal.get("ticker", ""), signal.get("timeframe", ""),
                signal.get("outfit_id", ""), periods_str,
                signal.get("entry_price", ""), signal.get("offset_applied", ""),
                signal.get("hit_count", ""), conv.get("score", ""),
                int(bool(conv.get("ohlc_detection", False))),
                int(bool(conv.get("candle_close", False))),
                int(bool(conv.get("parm_price", False))),
                int(bool(conv.get("time_series", False))),
                sys_state.get("S&P 500", ""), sys_state.get("NASDAQ", ""),
                sys_state.get("Dow Jones", ""), sys_state.get("VIX", ""),
                sys_state.get("SVIX", ""), sys_state.get("Russell 2000", ""),
                sys_state.get("Russell 3000", ""), sys_state.get("Semiconductors", ""),
                regime_label or "",
            ]
        else:
            row = [ts.isoformat()] + [""] * 12 + [
                sys_state.get("S&P 500", ""), sys_state.get("NASDAQ", ""),
                sys_state.get("Dow Jones", ""), sys_state.get("VIX", ""),
                sys_state.get("SVIX", ""), sys_state.get("Russell 2000", ""),
                sys_state.get("Russell 3000", ""), sys_state.get("Semiconductors", ""),
                regime_label or "",
            ]

        try:
            self._ensure_tabs_exist()
            self._ensure_log_header()
            self._service.spreadsheets().values().append(
                spreadsheetId=self.sheet_id,
                range="Log!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            ).execute()
        except Exception as e:
            logging.warning(f"SheetsWriter: Log append failed: {e}")

    def write_cycle(
        self,
        signal: Optional[dict],
        top_n: list,
        systems: list,
        regime_label: Optional[str] = None,
        ts: Optional[datetime] = None,
    ) -> None:
        """Convenience: write Current + append Log in one call."""
        if not self.enabled:
            return
        # Rebuild the HTTP service client each cycle — prevents BrokenPipeError
        # from stale connections after long idle periods between scans.
        try:
            self._service = self._build_fresh_service()
        except Exception as e:
            logging.warning(f"SheetsWriter: service rebuild failed ({e}) — skipping cycle")
            return
        ts = ts or datetime.now(timezone.utc)
        self.write_current(signal, top_n, systems, regime_label, ts)
        self.append_log_row(signal, systems, regime_label, ts)
