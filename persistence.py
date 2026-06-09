"""
persistence.py — InfluxDB time-series persistence for the SMA engine.

Schema (5 measurements):

  candles
    tags:    ticker, timeframe
    fields:  open, high, low, close, volume

  hits
    tags:    ticker, timeframe, outfit_id, sma_period, ohlc_component
    fields:  price (float)

  signals
    tags:    ticker, timeframe, outfit_id
    fields:  entry_price, offset, hit_count, convergence_score (0-4),
             rank_score, ohlc_detection (0/1), candle_close (0/1),
             parm_price (0/1), time_series (0/1)

  system_states
    tags:    system_name, proxy
    fields:  state_numeric (1=positive, -1=negative, 0=unknown),
             fast_value, slow_value

  regimes
    tags:    regime_label
    fields:  regime_id, proba_0, proba_1, proba_2

All writes go through a batched WriteAPI with sane defaults. If InfluxDB is
unreachable, the writer logs and continues — the engine never blocks on the
persistence layer.

Configuration via env vars (with sensible docker-compose defaults):
  INFLUX_URL    (default: http://localhost:8086)
  INFLUX_TOKEN  (default: element47-dev-token)
  INFLUX_ORG    (default: element47)
  INFLUX_BUCKET (default: sma_engine)
"""

from __future__ import annotations

import os
import logging
from typing import Optional, Iterable
from datetime import datetime, timezone

import pandas as pd

try:
    from influxdb_client import InfluxDBClient, Point, WritePrecision
    from influxdb_client.client.write_api import SYNCHRONOUS, ASYNCHRONOUS
    INFLUX_AVAILABLE = True
except ImportError:
    INFLUX_AVAILABLE = False

from engine import HashMapEntry, Hit, SystemState


class InfluxPersistence:
    """Writes engine state to InfluxDB. Fails open — never blocks the engine."""

    def __init__(
        self,
        url: Optional[str] = None,
        token: Optional[str] = None,
        org: Optional[str] = None,
        bucket: Optional[str] = None,
        enabled: bool = True,
    ):
        self.url = url or os.environ.get("INFLUX_URL", "http://localhost:8086")
        self.token = token or os.environ.get("INFLUX_TOKEN", "element47-dev-token")
        self.org = org or os.environ.get("INFLUX_ORG", "element47")
        self.bucket = bucket or os.environ.get("INFLUX_BUCKET", "sma_engine")
        self.enabled = enabled and INFLUX_AVAILABLE
        self._client = None
        self._write_api = None
        self._query_api = None
        self._connected = False

        if self.enabled:
            self._connect()

    def _connect(self) -> None:
        try:
            self._client = InfluxDBClient(url=self.url, token=self.token, org=self.org, timeout=60_000)
            health = self._client.ping()
            if not health:
                logging.warning(f"InfluxDB at {self.url} is not healthy — persistence disabled")
                self.enabled = False
                return
            self._write_api = self._client.write_api(write_options=ASYNCHRONOUS)
            self._query_api = self._client.query_api()
            self._connected = True
            logging.info(f"InfluxDB connected: {self.url}/{self.bucket}")
        except Exception as e:
            logging.warning(f"InfluxDB connection failed ({e}) — persistence disabled")
            self.enabled = False

    def write_candles(self, ticker: str, timeframe: str, df: pd.DataFrame) -> None:
        """Write a batch of OHLC bars."""
        if not self._connected or df.empty:
            return
        points = []
        for _, row in df.iterrows():
            p = (Point("candles")
                 .tag("ticker", ticker)
                 .tag("timeframe", timeframe)
                 .field("open", float(row["open"]))
                 .field("high", float(row["high"]))
                 .field("low", float(row["low"]))
                 .field("close", float(row["close"]))
                 .field("volume", int(row["volume"]))
                 .time(row["timestamp"], WritePrecision.S))
            points.append(p)
        try:
            self._write_api.write(bucket=self.bucket, org=self.org, record=points)
        except Exception as e:
            logging.warning(f"InfluxDB write_candles failed: {e}")

    def write_hits(self, hits: Iterable[Hit]) -> None:
        """Write hit records including sma_value and deciseconds for time-series scoring."""
        if not self._connected:
            return
        points = []
        for h in hits:
            p = (Point("hits")
                 .tag("ticker", h.ticker)
                 .tag("timeframe", h.timeframe)
                 .tag("outfit_id", str(h.outfit_id))
                 .tag("sma_period", str(h.sma_period))
                 .tag("ohlc_component", h.ohlc_component)
                 .field("price", h.price)
                 .field("sma_value", float(h.sma_value))      # the "parm" — actual SMA price
                 .field("deciseconds", float(h.deciseconds))  # candle duration weight
                 .time(h.timestamp, WritePrecision.S))
            points.append(p)
        if not points:
            return
        try:
            # Batch in chunks of 5000 to avoid oversized requests
            for i in range(0, len(points), 5000):
                self._write_api.write(bucket=self.bucket, org=self.org,
                                      record=points[i:i+5000])
        except Exception as e:
            logging.warning(f"InfluxDB write_hits failed: {e}")

    def write_signal(self, signal: dict, ts: Optional[datetime] = None) -> None:
        """Write the top signal output from a scan cycle."""
        if not self._connected or not signal:
            return
        ts = ts or datetime.now(timezone.utc)
        conv = signal.get("convergence", {})
        # Parse "n/4" score string
        score_str = conv.get("score", "0/4")
        try:
            score_num = int(score_str.split("/")[0])
        except (ValueError, IndexError):
            score_num = 0
        try:
            p = (Point("signals")
                 .tag("ticker", signal["ticker"])
                 .tag("timeframe", signal["timeframe"])
                 .tag("outfit_id", str(signal["outfit_id"]))
                 .field("entry_price", float(signal["entry_price"]))
                 .field("offset", float(signal.get("offset_applied", 0.0)))
                 .field("hit_count", int(signal["hit_count"]))
                 .field("convergence_score", score_num)
                 .field("rank_score", float(signal.get("rank_score", 0)))
                 .field("ohlc_detection", int(bool(conv.get("ohlc_detection", False))))
                 .field("candle_close", int(bool(conv.get("candle_close", False))))
                 .field("parm_price", int(bool(conv.get("parm_price", False))))
                 .field("time_series", int(bool(conv.get("time_series", False))))
                 .time(ts, WritePrecision.S))
            self._write_api.write(bucket=self.bucket, org=self.org, record=p)
        except Exception as e:
            logging.warning(f"InfluxDB write_signal failed: {e}")

    def write_system_states(self, states: list[SystemState],
                            ts: Optional[datetime] = None) -> None:
        """Write system monitor snapshot."""
        if not self._connected:
            return
        ts = ts or datetime.now(timezone.utc)
        state_to_num = {"positive": 1, "negative": -1, "unknown": 0}
        points = []
        for s in states:
            p = (Point("system_states")
                 .tag("system_name", s.name)
                 .tag("proxy", s.proxy)
                 .field("state_numeric", state_to_num.get(s.state, 0))
                 .field("fast_value", float(s.fast_value) if s.fast_value is not None else 0.0)
                 .field("slow_value", float(s.slow_value) if s.slow_value is not None else 0.0)
                 .time(ts, WritePrecision.S))
            points.append(p)
        try:
            self._write_api.write(bucket=self.bucket, org=self.org, record=points)
        except Exception as e:
            logging.warning(f"InfluxDB write_system_states failed: {e}")

    def write_regimes(self, regime_df: pd.DataFrame) -> None:
        """Write HMM regime sequence. Expects df with timestamp, regime,
        regime_label, regime_proba_* columns."""
        if not self._connected or regime_df.empty:
            return
        proba_cols = [c for c in regime_df.columns if c.startswith("regime_proba_")]
        points = []
        for _, row in regime_df.iterrows():
            p = (Point("regimes")
                 .tag("regime_label", str(row["regime_label"]))
                 .field("regime_id", int(row["regime"])))
            for pc in proba_cols:
                idx = pc.replace("regime_proba_", "")
                p = p.field(f"proba_{idx}", float(row[pc]))
            p = p.time(row["timestamp"], WritePrecision.S)
            points.append(p)
        try:
            for i in range(0, len(points), 5000):
                self._write_api.write(bucket=self.bucket, org=self.org,
                                      record=points[i:i+5000])
        except Exception as e:
            logging.warning(f"InfluxDB write_regimes failed: {e}")

    def write_top_n(self, top_n: list[dict], ts: Optional[datetime] = None) -> None:
        """Write each entry in top-N with its rank field."""
        if not self._connected or not top_n:
            return
        ts = ts or datetime.now(timezone.utc)
        points = []
        for entry in top_n:
            try:
                p = (Point("top_n")
                     .tag("ticker", entry["ticker"])
                     .tag("timeframe", entry["timeframe"])
                     .tag("outfit_id", str(entry["outfit_id"]))
                     .field("rank", int(entry["rank"]))
                     .field("hit_count", int(entry["hit_count"]))
                     .field("rank_score", float(entry["rank_score"]))
                     .field("entry_price", float(entry["entry_price"]))
                     .time(ts, WritePrecision.S))
                points.append(p)
            except (KeyError, ValueError) as e:
                logging.warning(f"Skipping malformed top_n entry: {e}")
        if points:
            try:
                self._write_api.write(bucket=self.bucket, org=self.org, record=points)
            except Exception as e:
                logging.warning(f"InfluxDB write_top_n failed: {e}")

    def query_cumulative_deciseconds(self, window_days: int = 7) -> dict:
        """
        Query InfluxDB for cumulative deciseconds per [ticker|TF|outfit_id|sma_period]
        over the last `window_days` days.

        Returns a dict keyed by (ticker, timeframe, outfit_id_str, sma_period_str)
        → total_deciseconds (float).

        This is the time-series persistence layer: deciseconds accumulate across
        cycles and sessions, so levels that price has visited repeatedly over days
        score higher than levels hit heavily in a single cycle.

        Returns empty dict if InfluxDB is unavailable or query fails.
        """
        if not self._connected:
            return {}
        flux = f"""
from(bucket: "{self.bucket}")
  |> range(start: -{window_days}d)
  |> filter(fn: (r) => r._measurement == "hits" and r._field == "deciseconds")
  |> group(columns: ["ticker", "timeframe", "outfit_id", "sma_period"])
  |> sum(column: "_value")
"""
        try:
            tables = self._query_api.query(flux, org=self.org)
            result = {}
            for table in tables:
                for record in table.records:
                    key = (
                        record.values.get("ticker", ""),
                        record.values.get("timeframe", ""),
                        record.values.get("outfit_id", ""),
                        record.values.get("sma_period", ""),
                    )
                    result[key] = float(record.get_value() or 0.0)
            return result
        except Exception as e:
            logging.warning(f"InfluxDB cumulative deciseconds query failed: {e}")
            return {}

    def flush(self) -> None:
        """Force-flush any pending async writes."""
        if self._write_api is not None:
            try:
                self._write_api.flush()
            except Exception:
                pass

    def close(self) -> None:
        self.flush()
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._connected = False


# Convenience: a no-op writer for when persistence is disabled but the calling
# code wants to call .write_*() unconditionally.
class NullPersistence:
    enabled = False
    def write_candles(self, *args, **kwargs): pass
    def write_hits(self, *args, **kwargs): pass
    def write_signal(self, *args, **kwargs): pass
    def write_system_states(self, *args, **kwargs): pass
    def write_regimes(self, *args, **kwargs): pass
    def write_top_n(self, *args, **kwargs): pass
    def query_cumulative_deciseconds(self, *args, **kwargs): return {}
    def flush(self): pass
    def close(self): pass
