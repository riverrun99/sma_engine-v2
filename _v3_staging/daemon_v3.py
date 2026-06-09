"""
daemon_v3.py — Long-running loop for Engine V3

Runs engine_v3.py on a configurable interval.

Configuration via env vars:
  V3_INTERVAL_SECONDS   Seconds between cycles (default: 600)
  V3_TOP_N              Top N signals (default: 500)
  V3_LOOKBACK           Candle lookback (default: 390)
  V3_TIMEFRAMES         Comma-separated timeframes (default: all)
  V3_MIN_SCORE          Minimum composite score to output (default: 0)
  V3_SCAN_WORKERS       Parallel scan workers (default: 6)
  ENGINE_SOURCE         webull or mock (default: webull)

Usage:
    docker exec e47_engine_v3 python3 /app/daemon_v3.py
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


def handle_signal(signum, frame):
    global SHUTDOWN
    logging.info(f"Received signal {signum} — shutting down after current cycle...")
    SHUTDOWN = True


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT,  handle_signal)


def run_command(cmd: list[str], label: str) -> bool:
    logging.info(f"  [{label}] starting...")
    start = time.monotonic()
    try:
        result = subprocess.run(cmd, stdout=sys.stdout, stderr=sys.stderr)
        elapsed = time.monotonic() - start
        if result.returncode == 0:
            logging.info(f"  [{label}] completed in {elapsed:.0f}s")
            return True
        else:
            logging.warning(f"  [{label}] exited with code {result.returncode}")
            return False
    except Exception as e:
        logging.error(f"  [{label}] failed: {e}")
        return False


def main():
    interval    = int(os.environ.get("V3_INTERVAL_SECONDS", "600"))
    top_n       = int(os.environ.get("V3_TOP_N",            "500"))
    lookback    = int(os.environ.get("V3_LOOKBACK",         "390"))
    min_score   = float(os.environ.get("V3_MIN_SCORE",      "0"))
    timeframes  = os.environ.get("V3_TIMEFRAMES",
                                 "1m,5m,15m,30m,1h,2h,4h,1d,1w,1mo").split(",")
    source      = os.environ.get("ENGINE_SOURCE", "webull")

    print("\n" + "═" * 80)
    print("  ENGINE V3 DAEMON")
    print("═" * 80)
    print(f"  Interval:    {interval}s ({interval // 60}m {interval % 60}s)")
    print(f"  Top N:       {top_n}")
    print(f"  Lookback:    {lookback} bars")
    print(f"  Timeframes:  {', '.join(timeframes)}")
    print(f"  Min score:   {min_score}")
    print(f"  Source:      {source}")
    print("═" * 80 + "\n", flush=True)

    cycle = 0

    while not SHUTDOWN:
        cycle += 1
        cycle_start = time.monotonic()
        cycle_ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        print("\n" + "═" * 80)
        print(f"  CYCLE {cycle} — {cycle_ts}")
        print("═" * 80, flush=True)

        engine_cmd = [
            "python3", "-u", "/app_v3/engine_v3.py",
            "--source",    source,
            "--top-n",     str(top_n),
            "--lookback",  str(lookback),
            "--timeframes", *timeframes,
            "--xlsx",
            "--verbose",
        ]
        if min_score > 0:
            engine_cmd += ["--min-score", str(min_score)]

        run_command(engine_cmd, "ENGINE_V3")

        if SHUTDOWN:
            break

        elapsed = time.monotonic() - cycle_start
        wait    = max(0, interval - elapsed)

        if wait > 0:
            logging.info(
                f"  Cycle {cycle} done in {elapsed:.0f}s. "
                f"Next in {wait:.0f}s"
            )
            slept = 0
            while slept < wait and not SHUTDOWN:
                time.sleep(min(5, wait - slept))
                slept += 5
        else:
            logging.info(f"  Cycle {cycle} done in {elapsed:.0f}s — starting next immediately")

    print("\n" + "═" * 80)
    print("  ENGINE V3 DAEMON — shutdown complete")
    print("═" * 80 + "\n", flush=True)


if __name__ == "__main__":
    main()
