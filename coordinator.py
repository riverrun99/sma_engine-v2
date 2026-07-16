"""
coordinator.py — Stagger the three SMA engines: main → normalized → v3
                 Then run discovery and confluence after each full cycle.

Watches the main engine output for a new cycle, then signals each subsequent
engine to run immediately (via SIGUSR1) rather than waiting for its own timer.
After V3 completes, runs discovery then confluence inside the main container.

Run from the sma_engine directory:
    python3 coordinator.py

No Docker restart needed — the containers pick up SIGUSR1 live.
Stop with Ctrl-C.
"""

from __future__ import annotations

import glob
import os
import subprocess
import time
import logging
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [coordinator] %(message)s",
    datefmt="%H:%M:%S",
)

BASE = os.path.dirname(os.path.abspath(__file__))

# Output file patterns (relative to sma_engine/)
MAIN_FILE   = os.path.join(BASE, "output", "signals_current.xlsx")
NORM_GLOB   = os.path.join(BASE, "output", "normalized_engine", "normalized_*.xlsx")
V3_GLOB     = os.path.join(BASE, "output", "v3", "v3_*.xlsx")

# Docker container names
NORM_CONTAINER = "e47_engine_normalized"
V3_CONTAINER   = "e47_engine_v3"

MAIN_CONTAINER = "e47_engine"
POLL_INTERVAL  = 10  # seconds between checks
NORM_STAGGER   = 180  # seconds to wait after main output before triggering normalized
                      # gives main engine time to free candle cache before normalized loads its own


def latest_mtime(pattern: str) -> float:
    files = glob.glob(pattern)
    return max((os.path.getmtime(f) for f in files), default=0.0)


def file_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except FileNotFoundError:
        return 0.0


def run_in_container(container: str, cmd: list[str], label: str) -> None:
    """Run a command inside a container via docker exec."""
    try:
        logging.info(f"Running {label} in {container}...")
        result = subprocess.run(
            ["docker", "exec", container] + cmd,
            capture_output=True, text=True
        )
        if result.returncode == 0:
            logging.info(f"{label} complete")
        else:
            logging.warning(f"{label} exited with code {result.returncode}: {result.stderr.strip()[:200]}")
    except Exception as e:
        logging.error(f"{label} failed: {e}")


def signal_container(name: str) -> None:
    try:
        result = subprocess.run(
            ["docker", "kill", "--signal=USR1", name],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            logging.info(f"Signalled {name} → running next cycle immediately")
        else:
            logging.warning(f"Could not signal {name}: {result.stderr.strip()}")
    except Exception as e:
        logging.error(f"docker kill failed for {name}: {e}")


def main() -> None:
    logging.info("Starting — watching main engine output")
    logging.info(f"  Main:       {MAIN_FILE}")
    logging.info(f"  Normalized: {NORM_GLOB}")
    logging.info(f"  V3:         {V3_GLOB}")
    logging.info(f"  Poll:       every {POLL_INTERVAL}s")

    last_main_mtime = file_mtime(MAIN_FILE)
    last_norm_mtime = latest_mtime(NORM_GLOB)
    last_v3_mtime   = latest_mtime(V3_GLOB)

    # State machine: track what we've triggered this chain
    triggered_norm = False
    triggered_v3   = False
    norm_trigger_at = None  # timestamp when we should fire normalized

    logging.info(
        f"Baseline — main: {datetime.fromtimestamp(last_main_mtime).strftime('%H:%M:%S') if last_main_mtime else 'none'}, "
        f"norm: {datetime.fromtimestamp(last_norm_mtime).strftime('%H:%M:%S') if last_norm_mtime else 'none'}, "
        f"v3: {datetime.fromtimestamp(last_v3_mtime).strftime('%H:%M:%S') if last_v3_mtime else 'none'}"
    )

    while True:
        time.sleep(POLL_INTERVAL)

        # ── Check main engine output ──────────────────────────────────────────
        current_main = file_mtime(MAIN_FILE)
        if current_main > last_main_mtime:
            last_main_mtime = current_main
            triggered_norm = False
            triggered_v3   = False
            norm_trigger_at = time.time() + NORM_STAGGER
            logging.info(f"Main engine output updated — normalized queued in {NORM_STAGGER}s (stagger to free candle cache)")

        # ── Fire normalized after stagger delay ───────────────────────────────
        if norm_trigger_at and not triggered_norm and time.time() >= norm_trigger_at:
            logging.info(f"Stagger elapsed — triggering normalized")
            signal_container(NORM_CONTAINER)
            triggered_norm = True
            norm_trigger_at = None

        # ── Check normalized output (only after we triggered it) ──────────────
        if triggered_norm and not triggered_v3:
            current_norm = latest_mtime(NORM_GLOB)
            if current_norm > last_norm_mtime:
                logging.info(f"Normalized output updated — triggering V3")
                last_norm_mtime = current_norm
                signal_container(V3_CONTAINER)
                triggered_v3 = True

        # ── Check V3 output — then run discovery + confluence ─────────────────
        if triggered_v3:
            current_v3 = latest_mtime(V3_GLOB)
            if current_v3 > last_v3_mtime:
                logging.info(f"V3 output updated — full chain complete. Running discovery + confluence...")
                last_v3_mtime = current_v3
                run_in_container(
                    MAIN_CONTAINER,
                    ["python3", "/app/discovery_engine.py", "--timeframes", "1d,1w,1mo"],
                    "DISCOVERY"
                )
                run_in_container(
                    MAIN_CONTAINER,
                    ["python3", "/app/confluence_engine.py", "--min-score", "2", "--discovery-tf", "1d,1w,1mo"],
                    "CONFLUENCE"
                )
                # ── Sync to Google Sheets ─────────────────────────────────────
                try:
                    logging.info("Syncing to Google Sheets...")
                    result = subprocess.run(
                        ["python3", os.path.join(BASE, "market_overlay", "sheets_sync.py")],
                        capture_output=True, text=True, timeout=120
                    )
                    if result.returncode == 0:
                        logging.info("Sheets sync complete")
                    else:
                        logging.warning(f"Sheets sync exited {result.returncode}: {result.stderr.strip()[:200]}")
                except Exception as e:
                    logging.error(f"Sheets sync failed: {e}")
                logging.info("Cycle complete — signalling main engine to start next cycle.")
                signal_container(MAIN_CONTAINER)
                triggered_norm = False
                triggered_v3   = False


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Stopped.")
