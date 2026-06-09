# =============================================================================
# STREAM_CLIENT.PY — MQTT Sub-Minute Streaming via Webull DataStreamingClient
#
# Ported from March engine's webull_stream.py v2.
# Credentials are read from environment variables (never hardcoded).
#
# External interface:
#   client = WebullStreamClient(symbols=["SPY", "QQQ", ...])
#   client.start()          # connects in background thread
#   client.get_candles("SPY", "5s")  → pd.DataFrame (OHLCV)
#   client.stop()
#
# Sub-minute timeframes aggregated: 1s, 5s, 15s, 30s
# (Controlled by STREAM_TIMEFRAMES env var in daemon.py)
# =============================================================================

from __future__ import annotations

import os
import time
import uuid
import logging
import threading
import pandas as pd
from collections import defaultdict

logger = logging.getLogger(__name__)

# Try to import the Webull SDK streaming client
try:
    from webull.data.data_streaming_client import DataStreamingClient
    from webull.data.common.category import Category
    from webull.data.common.subscribe_type import SubscribeType
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False
    logger.warning(
        "DataStreamingClient not found. "
        "Run: pip install --upgrade webull-openapi-python-sdk"
    )

# Log first N raw quote messages so we can verify SDK field names in prod
_RAW_LOG_LIMIT = 5
_raw_log_count = 0

# Bucket size in seconds for each sub-minute timeframe
TF_SECONDS: dict[str, int] = {
    "1s":  1,
    "5s":  5,
    "15s": 15,
    "30s": 30,
}


# =============================================================================
# Candle Aggregator — builds OHLCV candles from raw tick prices
# =============================================================================

class CandleAggregator:
    """
    Aggregates incoming tick prices into OHLC candles for each
    (symbol, timeframe) pair using bucket-based time windows.

    Thread-safe: a single lock guards both _current and candles.
    """

    def __init__(self, timeframes: list[str]):
        self.timeframes = timeframes
        self.candles:  dict[tuple, list] = defaultdict(list)   # {(sym, tf): [candle_dict, ...]}
        self._current: dict[tuple, dict] = {}                  # {(sym, tf): current_open_candle}
        self._lock = threading.Lock()

    def process_tick(self, symbol: str, price: float, timestamp: float) -> None:
        """Ingest one tick and advance all timeframe candles."""
        with self._lock:
            for tf in self.timeframes:
                tf_sec  = TF_SECONDS.get(tf, 1)
                bucket  = int(timestamp // tf_sec) * tf_sec
                key     = (symbol, tf)
                current = self._current.get(key)

                if current is None or current["bucket"] != bucket:
                    # Seal the old candle if one exists
                    if current is not None:
                        self.candles[key].append({
                            "timestamp": current["bucket"],
                            "open":      current["open"],
                            "high":      current["high"],
                            "low":       current["low"],
                            "close":     current["close"],
                            "volume":    current["volume"],
                        })
                        # Cap history to avoid unbounded growth
                        if len(self.candles[key]) > 999:
                            self.candles[key] = self.candles[key][-999:]

                    # Open a fresh candle for this bucket
                    self._current[key] = {
                        "bucket": bucket,
                        "open":   price,
                        "high":   price,
                        "low":    price,
                        "close":  price,
                        "volume": 0,
                    }
                else:
                    current["high"]  = max(current["high"], price)
                    current["low"]   = min(current["low"],  price)
                    current["close"] = price

    def get_candles(self, symbol: str, timeframe: str) -> pd.DataFrame:
        """Return completed (sealed) candles for a symbol/timeframe as DataFrame."""
        with self._lock:
            data = self.candles.get((symbol, timeframe), [])
            return pd.DataFrame(data) if data else pd.DataFrame()


# =============================================================================
# Streaming Client
# =============================================================================

class WebullStreamClient:
    """
    Connects to the Webull streaming API using the official
    DataStreamingClient SDK and feeds ticks into CandleAggregator.

    Credentials are read from environment variables:
        WEBULL_APP_KEY, WEBULL_APP_SECRET

    Optional env vars:
        STREAM_HOST   — MQTT host (default: wss.webullfintech.com)
        WEBULL_API_HOST — REST/HTTP host for SDK init (default: api.webullfintech.com)
    """

    def __init__(
        self,
        symbols: list[str],
        timeframes: list[str] | None = None,
        on_candle_update: callable | None = None,
    ):
        self.symbols          = symbols
        self.timeframes       = timeframes or ["1s", "5s", "15s", "30s"]
        self.session_id       = uuid.uuid4().hex    # SDK expects hex string
        self.aggregator       = CandleAggregator(self.timeframes)
        self.on_candle_update = on_candle_update
        self._client          = None
        self._connected       = False

    # ------------------------------------------------------------------
    # SDK Callbacks
    # ------------------------------------------------------------------

    def _on_connect_success(self, client, api_client, session_id):
        """Called by SDK when MQTT connection is established."""
        logger.info(f"[stream] Connected — session {session_id}")
        self._connected = True

        # Subscribe all symbols as US_STOCK.
        # Webull streaming accepts ETFs under US_STOCK category too.
        sub_types = [SubscribeType.TICK.name]

        def subscribe_in_chunks(symbols: list[str], category: str, chunk_size: int = 100):
            for i in range(0, len(symbols), chunk_size):
                chunk = symbols[i : i + chunk_size]
                try:
                    client.subscribe(chunk, category, sub_types)
                    logger.info(
                        f"[stream] Subscribed {len(chunk)} symbols "
                        f"(batch {i // chunk_size + 1}) → {category}"
                    )
                except Exception as e:
                    logger.error(f"[stream] Subscription error (batch {i // chunk_size + 1}): {e}")

        try:
            subscribe_in_chunks(self.symbols, Category.US_STOCK.name)
        except Exception as e:
            logger.error(f"[stream] Subscription failed: {e}")

    def _on_subscribe_success(self, client, api_client, session_id):
        logger.info(f"[stream] Subscription confirmed — session {session_id}")

    def _on_quotes_message(self, client, topic, quotes):
        """
        Called by SDK for every incoming market data message.

        Topic → payload shape:
          tick     → { basic{symbol, timestamp}, time, price, volume, side }
          snapshot → { basic{symbol, ...}, trade_time, price, open, high, low, ... }
          quote    → { basic{symbol, ...}, asks[], bids[] }
        """
        global _raw_log_count
        try:
            # Log first N raw messages to confirm SDK field names in production
            if _raw_log_count < _RAW_LOG_LIMIT:
                logger.info(f"[stream RAW #{_raw_log_count + 1}] topic={topic} quotes={quotes}")
                _raw_log_count += 1

            items = quotes if isinstance(quotes, list) else [quotes]

            for q in items:
                if not isinstance(q, dict):
                    continue

                basic  = q.get("basic") or {}
                symbol = basic.get("symbol") or q.get("symbol") or q.get("ticker")
                if not symbol:
                    continue

                # --- Extract price and timestamp by topic ---
                if topic == "tick":
                    price = q.get("price")
                    ts    = q.get("time") or basic.get("timestamp")

                elif topic == "snapshot":
                    price = q.get("price")
                    ts    = q.get("trade_time") or basic.get("timestamp")

                elif topic == "quote":
                    asks = q.get("asks", [])
                    bids = q.get("bids", [])
                    if asks and bids:
                        try:
                            price = (float(asks[0].get("price", 0)) +
                                     float(bids[0].get("price", 0))) / 2
                        except (TypeError, ValueError):
                            price = None
                    else:
                        price = None
                    ts = basic.get("timestamp")

                else:
                    continue  # echo/notice/unknown — skip

                if not price:
                    continue

                try:
                    price = float(price)
                    ts    = float(ts) if ts else time.time()
                    # Webull timestamps may arrive in milliseconds
                    if ts > 1e12:
                        ts /= 1000.0
                except (TypeError, ValueError):
                    continue

                self.aggregator.process_tick(symbol, price, ts)
                if self.on_candle_update:
                    self.on_candle_update(symbol)

        except Exception as e:
            logger.error(f"[stream] Error processing quote message: {e}")

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Connect and start the background streaming thread."""
        if not _SDK_AVAILABLE:
            logger.warning(
                "[stream] DataStreamingClient not available — streaming disabled. "
                "Install: pip install --upgrade webull-openapi-python-sdk"
            )
            return

        app_key    = os.environ.get("WEBULL_APP_KEY", "")
        app_secret = os.environ.get("WEBULL_APP_SECRET", "")
        stream_host = os.environ.get("STREAM_HOST", "wss.webullfintech.com")
        api_host    = os.environ.get("WEBULL_API_HOST", "api.webullfintech.com")

        if not (app_key and app_secret):
            logger.error("[stream] WEBULL_APP_KEY/SECRET not set — streaming disabled.")
            return

        try:
            self._client = DataStreamingClient(
                app_key,
                app_secret,
                "us",
                self.session_id,
                http_host=api_host,
                mqtt_host=stream_host,
            )

            self._client.on_connect_success   = self._on_connect_success
            self._client.on_subscribe_success = self._on_subscribe_success
            self._client.on_quotes_message    = self._on_quotes_message

            # Async mode — runs in background thread, main loop unblocked
            self._client.connect_and_loop_start()
            logger.info("[stream] DataStreamingClient started (background thread).")

        except Exception as e:
            logger.error(f"[stream] Failed to start DataStreamingClient: {e}")
            self._client = None

    def stop(self) -> None:
        """Unsubscribe and stop the streaming thread cleanly."""
        if self._client:
            try:
                self._client.unsubscribe(unsubscribe_all=True)
                self._client.loop_stop()
                logger.info("[stream] Stream client stopped.")
            except Exception as e:
                logger.warning(f"[stream] Stop error (non-fatal): {e}")
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_candles(self, symbol: str, timeframe: str) -> pd.DataFrame:
        """Return aggregated sub-minute candles for a symbol/timeframe."""
        return self.aggregator.get_candles(symbol, timeframe)
