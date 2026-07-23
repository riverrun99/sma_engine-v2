"""
daemon_normalized.py — Long-running loop for the Normalized Engine.

Runs engine_normalized.py on a configurable interval, then automatically
runs signal_tracker.py after each cycle to log prices and returns.

Configuration via env vars:
  NORM_INTERVAL_SECONDS   Seconds between cycles (default: 300)
  NORM_TOP_N              Top N signals to output (default: 500)
  NORM_LOOKBACK           Candle lookback window (default: 390)
  NORM_TIMEFRAMES         Comma-separated timeframes (default: all)
  NORM_TRACKER_TOP        Top N signals for tracker to log (default: 50)
  ENGINE_SCAN_WORKERS     Parallel scan workers (default: 6)
  WEBULL_APP_KEY          Required for live data
  WEBULL_APP_SECRET       Required for live data

Usage:
    docker exec e47_engine python /app/daemon_normalized.py

    # Custom interval (every 10 minutes)
    docker exec -e NORM_INTERVAL_SECONDS=600 e47_engine python /app/daemon_normalized.py

    # Run in background
    docker exec -d e47_engine python /app/daemon_normalized.py
"""

from __future__ import annotations

import os
import sys
import time
import signal
import logging
import subprocess
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

SHUTDOWN = False
RUN_NOW  = False   # set True by SIGUSR1 to skip current sleep


def handle_signal(signum, frame):
    global SHUTDOWN
    logging.info(f"Received signal {signum} — shutting down after current cycle...")
    SHUTDOWN = True


def handle_sigusr1(signum, frame):
    global RUN_NOW
    logging.info("Received SIGUSR1 — skipping sleep, running next cycle immediately")
    RUN_NOW = True


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT,  handle_signal)
signal.signal(signal.SIGUSR1, handle_sigusr1)


def run_command(cmd: list[str], label: str) -> bool:
    """Run a subprocess command, stream output live, return True on success."""
    logging.info(f"  [{label}] starting...")
    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        elapsed = time.monotonic() - start
        if result.returncode == 0:
            logging.info(f"  [{label}] completed in {elapsed:.0f}s")
            return True
        else:
            logging.warning(f"  [{label}] exited with code {result.returncode} after {elapsed:.0f}s")
            return False
    except Exception as e:
        logging.error(f"  [{label}] failed: {e}")
        return False


def main():
    global RUN_NOW
    # ── Config from env vars ──────────────────────────────────────────────────
    interval     = int(os.environ.get("NORM_INTERVAL_SECONDS",  "300"))
    top_n        = int(os.environ.get("NORM_TOP_N",             "500"))
    lookback     = int(os.environ.get("NORM_LOOKBACK",          "390"))
    tracker_top  = int(os.environ.get("NORM_TRACKER_TOP",       "50"))
    timeframes   = os.environ.get(
        "NORM_TIMEFRAMES",
        "1m,5m,15m,30m,1h,2h,4h,1d,1w,1mo"
    ).split(",")
    source       = os.environ.get("ENGINE_SOURCE", "webull")
    # When true, don't scan on boot — wait for the coordinator's first SIGUSR1.
    # This preserves the main→normalized→V3 stagger on a COLD START. Without it,
    # every engine runs its first scan the instant its container boots, so a
    # fresh `fullrun` has all three scanning at once and overruns memory (OOM).
    wait_for_signal = os.environ.get("WAIT_FOR_SIGNAL", "false").lower() in ("true", "1", "yes")

    # ── Startup banner ────────────────────────────────────────────────────────
    print("\n" + "═" * 71)
    print("  NORMALIZED ENGINE DAEMON")
    print("═" * 71)
    print(f"  Interval:    {interval}s ({interval // 60}m {interval % 60}s)")
    print(f"  Top N:       {top_n}")
    print(f"  Lookback:    {lookback} bars")
    print(f"  Timeframes:  {', '.join(timeframes)}")
    print(f"  Tracker top: {tracker_top} signals logged per cycle")
    print(f"  Source:      {source}")
    print("═" * 71 + "\n")

    cycle = 0

    # ── Cold-start stagger: wait for the coordinator's first trigger ──────────
    # so we don't scan simultaneously with the other engines on boot.
    if wait_for_signal:
        logging.info("  WAIT_FOR_SIGNAL set — holding until coordinator triggers "
                     "(SIGUSR1) before first scan (preserves cold-start stagger)")
        while not RUN_NOW and not SHUTDOWN:
            time.sleep(2)
        RUN_NOW = False

    while not SHUTDOWN:
        cycle += 1
        cycle_start = time.monotonic()
        cycle_ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        print("\n" + "═" * 71)
        print(f"  CYCLE {cycle} — {cycle_ts}")
        print("═" * 71)

        # ── Step 1: Run normalized engine ─────────────────────────────────────
        engine_cmd = [
            "python3", "-u", "/app/engine_normalized.py", "--verbose",
            "--source",    source,
            "--top-n",     str(top_n),
            "--lookback",  str(lookback),
            "--timeframes", *timeframes,
            "--xlsx",
        ]
        engine_ok = run_command(engine_cmd, "ENGINE")

        if SHUTDOWN:
            break

        # ── Step 2: Run signal tracker ────────────────────────────────────────
        if engine_ok:
            tracker_cmd = [
                "python3", "-u", "/app/signal_tracker.py",
                "--top", str(tracker_top),
            ]
            run_command(tracker_cmd, "TRACKER")
        else:
            logging.warning("  Engine cycle failed — skipping tracker this cycle")

        if SHUTDOWN:
            break

        # ── Wait until next cycle ─────────────────────────────────────────────
        elapsed  = time.monotonic() - cycle_start
        wait     = max(0, interval - elapsed)
        next_run = datetime.now(timezone.utc).strftime("%H:%M:%S")

        if wait > 0:
            logging.info(
                f"  Cycle {cycle} done in {elapsed:.0f}s. "
                f"Next cycle in {wait:.0f}s (at ~{next_run} UTC)"
            )
            # Sleep in small chunks so SIGTERM/SIGUSR1 are caught quickly
            slept = 0
            while slept < wait and not SHUTDOWN and not RUN_NOW:
                time.sleep(min(5, wait - slept))
                slept += 5
            RUN_NOW = False
        else:
            logging.info(
                f"  Cycle {cycle} done in {elapsed:.0f}s "
                f"(exceeded interval of {interval}s — starting next cycle immediately)"
            )

    print("\n" + "═" * 71)
    print("  NORMALIZED ENGINE DAEMON — shutdown complete")
    print("═" * 71 + "\n")


if __name__ == "__main__":
    main()
