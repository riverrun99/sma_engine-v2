"""
daemon.py — long-running scan loop that writes to InfluxDB.

Runs the engine every ENGINE_INTERVAL_SECONDS, writes:
  - candles (incremental, new bars only)
  - hits (all detected this cycle)
  - signal (the top-ranked one)
  - top_n (the leaderboard)
  - system_states (snapshot)
  - regimes (refit every N cycles)

Designed to run inside the docker-compose stack alongside InfluxDB and Grafana.
"""

from __future__ import annotations

import os
import time
import signal
import logging
import dataclasses
import gzip
import pickle
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from engine import (
    OUTFITS, SMAOutfitEngine, EngineConfig, MockClient, WebullClient,
    UNIVERSE_TIER_1, UNIVERSE_TIER_2, UNIVERSE_TIER_3,
    rank_entries,
)
from persistence import InfluxPersistence
from regime import HMM_AVAILABLE, build_features, fit_regimes, predict_regimes
from local_writer import LocalWriter
from sheets_writer import SheetsWriter
from terminal_ui import TerminalUI
from stream_client import WebullStreamClient


SHUTDOWN = False

# Paths to the hot-reload filter files (mounted read-only from host at /app/)
MUTED_TICKERS_PATH  = "/app/muted_tickers.txt"
CUSTOM_TICKERS_PATH = "/app/custom_tickers.txt"

# Disk cache — persists across restarts via engine_cache Docker volume
CACHE_DIR = "/cache/candle_cache"


def save_candle_cache(cache: dict, cache_dir: str = CACHE_DIR) -> None:
    """
    Save the in-memory candle cache to disk, one gzip-pickle file per ticker.
    Stored in the engine_cache Docker volume so it survives container restarts.
    """
    if not cache:
        return
    try:
        os.makedirs(cache_dir, exist_ok=True)

        # Group by ticker
        by_ticker: dict[str, dict] = {}
        for (ticker, tf), df in cache.items():
            if df is not None and not df.empty:
                if ticker not in by_ticker:
                    by_ticker[ticker] = {}
                by_ticker[ticker][tf] = df

        for ticker, tf_data in by_ticker.items():
            path = os.path.join(cache_dir, f"{ticker}.pkl.gz")
            with gzip.open(path, "wb", compresslevel=3) as f:
                pickle.dump(tf_data, f)

        logging.info(
            f"  cache saved: {len(by_ticker)} tickers → {cache_dir}"
        )
    except Exception as e:
        logging.warning(f"Cache save failed (non-fatal): {e}")


def load_candle_cache(cache_dir: str = CACHE_DIR) -> dict:
    """
    Load the candle cache from disk at startup.
    Returns empty dict if no cache exists yet (first ever run).
    """
    cache: dict = {}
    if not os.path.exists(cache_dir):
        return cache
    try:
        files = [f for f in os.listdir(cache_dir) if f.endswith(".pkl.gz")]
        for fname in files:
            ticker = fname[:-7]  # strip .pkl.gz
            path = os.path.join(cache_dir, fname)
            try:
                with gzip.open(path, "rb") as f:
                    tf_data: dict = pickle.load(f)
                for tf, df in tf_data.items():
                    cache[(ticker, tf)] = df
            except Exception as e:
                logging.warning(f"  cache load failed for {ticker}: {e}")

        if cache:
            logging.info(
                f"  cache loaded: {len(files)} tickers, "
                f"{len(cache)} (ticker, tf) pairs from {cache_dir}"
            )
    except Exception as e:
        logging.warning(f"Cache load failed (non-fatal): {e}")
    return cache


def load_ticker_file(path: str) -> list[str]:
    """
    Read a ticker list file and return uppercase ticker symbols.

    Format:
      - One ticker per line
      - Lines starting with # are comments (ignored)
      - Inline comments after # are stripped
      - Blank lines ignored
      - Case-insensitive (normalised to uppercase)

    Returns an empty list if the file doesn't exist.
    """
    tickers = []
    try:
        with open(path) as f:
            for line in f:
                line = line.split("#")[0].strip()   # strip inline comments
                if line:
                    tickers.append(line.upper())
    except FileNotFoundError:
        pass
    return tickers


def handle_signal(signum, frame):
    global SHUTDOWN
    logging.info(f"Received signal {signum}, shutting down after current cycle...")
    SHUTDOWN = True


def make_client(source: str):
    if source == "webull":
        app_key = os.environ.get("WEBULL_APP_KEY")
        app_secret = os.environ.get("WEBULL_APP_SECRET")
        if not (app_key and app_secret):
            logging.error("WEBULL_APP_KEY/SECRET not set, falling back to mock")
            return MockClient()
        return WebullClient(app_key, app_secret,
                            region=os.environ.get("WEBULL_REGION", "us"))
    return MockClient()


def build_universe(tier: str) -> list[str]:
    if tier == "tier1":
        return UNIVERSE_TIER_1
    if tier == "tier2":
        return UNIVERSE_TIER_1 + UNIVERSE_TIER_2
    if tier == "all":
        return UNIVERSE_TIER_1 + UNIVERSE_TIER_2 + UNIVERSE_TIER_3
    return UNIVERSE_TIER_1


def run_cycle(
    client, cfg, persist, local_writer, sheets_writer, terminal_ui,
    cycle_count: int, current_regime_label: Optional[str],
    top_n_count: int = 50,
    fit_regime: bool = False,
    stream_client=None,
    persistent_cache: dict | None = None,
) -> tuple[Optional[str], dict]:
    """One full scan cycle: detect, rank, write to all outputs.
    Returns the (possibly updated) current regime label."""
    cycle_ts = datetime.now(timezone.utc)
    cycle_start = time.monotonic()
    logging.info(f"Cycle {cycle_count} start at {cycle_ts.isoformat()}")

    # ── Hot-reload mute + custom ticker lists ─────────────────────────────────
    muted  = set(load_ticker_file(MUTED_TICKERS_PATH))
    custom = load_ticker_file(CUSTOM_TICKERS_PATH)

    # Build effective universe: base + custom, minus muted, deduped, order preserved
    base = cfg.universe
    seen = set()
    effective_universe = []
    for t in base + custom:
        if t not in muted and t not in seen:
            effective_universe.append(t)
            seen.add(t)

    if muted:
        logging.info(f"  muted {len(muted)} tickers: {', '.join(sorted(muted))}")
    if custom:
        new_custom = [t for t in custom if t not in set(cfg.universe)]
        if new_custom:
            logging.info(f"  added {len(new_custom)} custom tickers: {', '.join(new_custom)}")

    # Swap in the effective universe for this cycle only (cfg is not mutated)
    cycle_cfg = dataclasses.replace(cfg, universe=effective_universe)

    engine = SMAOutfitEngine(
        client, cycle_cfg,
        stream_client=stream_client,
        initial_cache=persistent_cache,
    )
    engine.monitor_systems()
    engine.scan()

    # ─── Query cumulative deciseconds (time-series persistence across cycles) ─
    # Raul's methodology accumulates deciseconds over days, not just one cycle.
    # Pull the running totals from InfluxDB so the scorer can blend them in.
    try:
        cumulative_ds = persist.query_cumulative_deciseconds(window_days=7)
        if cumulative_ds:
            logging.info(f"  cumulative_ds: {len(cumulative_ds)} level keys loaded from Influx")
    except Exception as e:
        logging.warning(f"  cumulative_ds query failed (non-fatal): {e}")
        cumulative_ds = {}

    # ─── Compute signal + top_n first (needed for local/sheets output) ────
    signal_dict = engine.top_signal(cumulative_ds=cumulative_ds)
    top_n_list = engine.top_n(top_n_count, cumulative_ds=cumulative_ds)

    # ─── Local file writer (always, before Influx so output is never blocked) ─
    try:
        local_writer.write_cycle(
            signal_dict, top_n_list, engine.system_states,
            regime_label=current_regime_label, ts=cycle_ts,
            candle_cache=engine.candle_cache,
        )
    except Exception as e:
        logging.warning(f"LocalWriter cycle failed: {e}")

    # ─── Sheets writer (optional) ────────────────────────────────────────
    if sheets_writer.enabled:
        try:
            sheets_writer.write_cycle(
                signal_dict, top_n_list, engine.system_states,
                regime_label=current_regime_label, ts=cycle_ts,
            )
        except Exception as e:
            logging.warning(f"SheetsWriter cycle failed: {e}")

    # ─── Influx writes (after local output — Influx issues won't block files) ─
    try:
        for (ticker, tf), df in engine.candle_cache.items():
            if not df.empty:
                persist.write_candles(ticker, tf, df)

        all_hits = []
        for entry in engine.store.all():
            all_hits.extend(entry.hits)
        persist.write_hits(all_hits)

        if signal_dict is not None:
            persist.write_signal(signal_dict, ts=cycle_ts)
        persist.write_top_n(top_n_list, ts=cycle_ts)
        persist.write_system_states(engine.system_states, ts=cycle_ts)
    except Exception as e:
        logging.warning(f"InfluxDB write failed (non-fatal): {e}")

    # ─── Regime refit + current label ─────────────────────────────────────
    if fit_regime and HMM_AVAILABLE:
        try:
            spy = engine.candle_cache.get(("SPY", "1d"))
            uvxy = engine.candle_cache.get(("UVXY", "1d"))
            smh = engine.candle_cache.get(("SMH", "1d"))
            if spy is None or len(spy) == 0:
                spy = client.fetch_bars("SPY", "1d", 999)
            if uvxy is None or len(uvxy) == 0:
                uvxy = client.fetch_bars("UVXY", "1d", 999)
            if smh is None or len(smh) == 0:
                smh = client.fetch_bars("SMH", "1d", 999)

            features = build_features(spy, uvxy, smh)
            if len(features) >= 50:
                model = fit_regimes(features, n_states=3, n_iter=100)
                regimes = predict_regimes(model, features)
                persist.write_regimes(regimes)
                # Get the most recent regime label
                if len(regimes) > 0:
                    current_regime_label = str(regimes["regime_label"].iloc[-1])
                logging.info(f"  regime refit: {current_regime_label}")
        except Exception as e:
            logging.warning(f"Regime refit failed: {e}")

    persist.flush()

    # ─── Terminal UI (always, last so it overlays everything) ─────────────
    scan_time = time.monotonic() - cycle_start
    if terminal_ui is not None:
        try:
            terminal_ui.render(
                signal_dict, top_n_list, engine.system_states,
                regime_label=current_regime_label,
                cycle_count=cycle_count,
                last_scan=cycle_ts,
            )
        except Exception as e:
            logging.warning(f"TerminalUI render failed: {e}")

    if signal_dict is not None:
        logging.info(f"  top: {signal_dict['ticker']} {signal_dict['timeframe']} "
                     f"hits={signal_dict['hit_count']} "
                     f"conv={signal_dict['convergence']['score']}")

    # Persist cache to disk so restarts don't cold-start
    save_candle_cache(engine.candle_cache)

    return current_regime_label, engine.candle_cache


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # Silence Webull SDK internal loggers — they dump full request blobs on
    # every INVALID_SYMBOL / 4xx, making logs unreadable.
    for _noisy in ("webull", "webull.core.client", "webull.core.auth",
                   "urllib3", "urllib3.connectionpool"):
        _lg = logging.getLogger(_noisy)
        _lg.setLevel(logging.CRITICAL)
        _lg.propagate = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    source = os.environ.get("ENGINE_SOURCE", "mock")
    universe_tier = os.environ.get("ENGINE_UNIVERSE", "tier1")
    interval = int(os.environ.get("ENGINE_INTERVAL_SECONDS", "300"))
    timeframes = os.environ.get("ENGINE_TIMEFRAMES", "5m,15m,30m,1h,1d").split(",")
    top_n_count = int(os.environ.get("ENGINE_TOP_N", "50"))
    regime_every = int(os.environ.get("ENGINE_REGIME_EVERY", "12"))  # cycles
    backtest_every = int(os.environ.get("ENGINE_BACKTEST_EVERY", "0"))  # 0 = disabled
    terminal_enabled = os.environ.get("TERMINAL_UI", "true").lower() in ("true", "1", "yes")
    output_dir = os.environ.get("LOCAL_OUTPUT_DIR", "./output")

    # ── Hit detection settings ─────────────────────────────────────────────
    hit_mode      = os.environ.get("HIT_MODE", "exact")          # exact | wick | both
    hit_tolerance = float(os.environ.get("HIT_TOLERANCE", "0.0"))  # dollars

    # ── Sub-minute streaming settings ──────────────────────────────────────
    stream_enabled  = os.environ.get("STREAM_ENABLED", "false").lower() in ("true", "1", "yes")
    stream_tfs_raw  = os.environ.get("STREAM_TIMEFRAMES", "1s,5s,15s,30s")
    stream_tfs      = [t.strip() for t in stream_tfs_raw.split(",") if t.strip()]

    client = make_client(source)
    universe = build_universe(universe_tier)
    cfg = EngineConfig(
        universe=universe,
        timeframes=timeframes,
        lookback=int(os.environ.get("ENGINE_LOOKBACK", "130")),
        hit_mode=hit_mode,
        hit_tolerance=hit_tolerance,
        stream_timeframes=stream_tfs if stream_enabled else [],
        refresh_bars=int(os.environ.get("ENGINE_REFRESH_BARS", "20")),
        min_tf_minutes=int(os.environ.get("ENGINE_MIN_TF_MINUTES", "15")),
    )

    # ── Start streaming client (optional) ──────────────────────────────────
    stream_client = None
    if stream_enabled:
        logging.info(
            f"Streaming enabled: tfs={stream_tfs}, "
            f"subscribing {len(universe)} symbols..."
        )
        stream_client = WebullStreamClient(
            symbols=universe,
            timeframes=stream_tfs,
        )
        stream_client.start()
        logging.info("Stream client started. Waiting 10s for connection...")
        time.sleep(10)
    else:
        logging.info("Streaming disabled (STREAM_ENABLED=false).")

    # Instantiate all writers + UI
    persist = InfluxPersistence()
    local_writer = LocalWriter(output_dir=output_dir)
    sheets_writer = SheetsWriter()
    terminal_ui = TerminalUI() if terminal_enabled else None

    if not persist.enabled:
        logging.warning("InfluxDB persistence disabled — running engine but not writing to Influx")
    if not sheets_writer.enabled:
        logging.info("Google Sheets writer not configured — using local files only")
    else:
        logging.info(f"Google Sheets writer connected to sheet {sheets_writer.sheet_id[:8]}...")
    logging.info(f"Local writer output dir: {local_writer.output_dir}")

    logging.info(
        f"Daemon starting: source={source} universe={universe_tier} "
        f"({len(cfg.universe)} tickers) tfs={timeframes} "
        f"hit_mode={hit_mode} tol={hit_tolerance} "
        f"stream={stream_enabled} top_n={top_n_count} interval={interval}s"
    )

    cycle_count = 0
    current_regime_label: Optional[str] = None
    # Load cache from disk — if a previous run saved it, cycle 1 becomes incremental
    logging.info("Loading candle cache from disk...")
    persistent_cache: dict = load_candle_cache()
    try:
        while not SHUTDOWN:
            start = time.monotonic()
            try:
                fit_regime = (cycle_count % regime_every == 0)
                current_regime_label, persistent_cache = run_cycle(
                    client, cfg, persist, local_writer, sheets_writer, terminal_ui,
                    cycle_count=cycle_count,
                    current_regime_label=current_regime_label,
                    top_n_count=top_n_count,
                    fit_regime=fit_regime,
                    stream_client=stream_client,
                    persistent_cache=persistent_cache,
                )
            except Exception as e:
                logging.exception(f"Cycle failed: {e}")
                persistent_cache = {}  # reset cache on crash to avoid stale data

            # ── Auto-backtest ──────────────────────────────────────────────
            if backtest_every > 0 and cycle_count > 0 and cycle_count % backtest_every == 0:
                try:
                    import run_backtest
                    logging.info(f"Running auto-backtest (cycle {cycle_count})...")
                    run_backtest.run(method="walk_forward", horizon_bars=5, top_n=100)
                except Exception as e:
                    logging.warning(f"Auto-backtest failed: {e}")

            cycle_count += 1
            elapsed = time.monotonic() - start
            sleep_for = max(0, interval - elapsed)
            logging.info(f"Cycle {cycle_count} done in {elapsed:.1f}s, "
                         f"sleeping {sleep_for:.0f}s")

            # Sleep in 1s chunks so SIGTERM is responsive
            slept = 0.0
            while slept < sleep_for and not SHUTDOWN:
                time.sleep(min(1.0, sleep_for - slept))
                slept += 1.0
    finally:
        if stream_client is not None:
            stream_client.stop()
        persist.close()
        logging.info("Daemon stopped cleanly")


if __name__ == "__main__":
    main()
