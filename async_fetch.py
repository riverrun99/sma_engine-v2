"""
async_fetch.py — concurrent data fetching with token-bucket rate budgeting.

Webull OpenAPI rate limit is 60 calls/minute per app key. At 2k tickers ×
5 timeframes = 10,000 calls per scan, synchronous fetching takes ~167 minutes
(2.8 hours) at the rate ceiling. This module fetches concurrently within the
rate envelope, prioritizing Tier 1 critical tickers and using leftover budget
for Tier 2/3.

Token-bucket algorithm: tokens regenerate at a fixed rate (60/min = 1/sec).
Each request consumes one token. If no tokens available, request waits.
Concurrency is bounded by a semaphore so we don't open 10k connections.

Usage:
    fetcher = AsyncFetcher(client=webull_client, max_concurrent=20)
    results = asyncio.run(fetcher.fetch_all(requests))
    # results is dict {(ticker, tf): DataFrame}
"""

from __future__ import annotations

import asyncio
import time
import logging
from dataclasses import dataclass
from typing import Optional
import pandas as pd

from engine import DataClient, MockClient


@dataclass
class FetchRequest:
    ticker: str
    timeframe: str
    count: int
    priority: int = 1  # 1 = highest, 3 = lowest


class TokenBucket:
    """Asyncio-safe token bucket rate limiter.

    capacity:    max tokens that can accumulate (burst limit)
    refill_rate: tokens added per second (60/min = 1.0)
    """

    def __init__(self, capacity: int = 60, refill_rate: float = 1.0):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: int = 1) -> None:
        """Block until n tokens are available, then consume them."""
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
                self.last_refill = now

                if self.tokens >= n:
                    self.tokens -= n
                    return

                # Not enough tokens — compute wait time
                deficit = n - self.tokens
                wait = deficit / self.refill_rate

            # Sleep outside the lock so other coroutines can check
            await asyncio.sleep(min(wait, 1.0))


class AsyncFetcher:
    """Concurrent fetcher with rate budgeting and priority queueing."""

    def __init__(
        self,
        client: DataClient,
        max_concurrent: int = 20,
        rate_limit: int = 60,
        rate_window_seconds: int = 60,
    ):
        self.client = client
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.bucket = TokenBucket(
            capacity=rate_limit,
            refill_rate=rate_limit / rate_window_seconds,
        )
        self.stats = {"completed": 0, "failed": 0, "total": 0}

    async def _fetch_one(self, req: FetchRequest) -> tuple[tuple[str, str], pd.DataFrame]:
        async with self.semaphore:
            await self.bucket.acquire(1)
            try:
                # client.fetch_bars is sync — run in thread pool to avoid blocking
                loop = asyncio.get_event_loop()
                df = await loop.run_in_executor(
                    None, self.client.fetch_bars, req.ticker, req.timeframe, req.count
                )
                self.stats["completed"] += 1
                if self.stats["completed"] % 100 == 0:
                    logging.info(f"  fetched {self.stats['completed']}/{self.stats['total']}")
                return (req.ticker, req.timeframe), df
            except Exception as e:
                self.stats["failed"] += 1
                logging.warning(f"fetch failed for {req.ticker} {req.timeframe}: {e}")
                return (req.ticker, req.timeframe), pd.DataFrame()

    async def fetch_all(self, requests: list[FetchRequest]) -> dict[tuple[str, str], pd.DataFrame]:
        """Fetch all requests concurrently, sorted by priority (1 first)."""
        self.stats["total"] = len(requests)
        self.stats["completed"] = 0
        self.stats["failed"] = 0

        # Sort by priority so high-priority requests start first
        # (asyncio.gather doesn't guarantee start order but with semaphore,
        #  earlier-submitted tasks get the first slots)
        sorted_reqs = sorted(requests, key=lambda r: r.priority)

        start = time.monotonic()
        tasks = [self._fetch_one(r) for r in sorted_reqs]
        results = await asyncio.gather(*tasks)
        elapsed = time.monotonic() - start

        logging.info(f"AsyncFetcher: {self.stats['completed']} ok, "
                     f"{self.stats['failed']} failed in {elapsed:.1f}s "
                     f"({self.stats['completed']/max(elapsed,0.01):.1f} req/s)")

        return dict(results)


def build_requests(
    universe_tier1: list[str],
    universe_tier2: list[str],
    universe_tier3: list[str],
    timeframes: list[str],
    candle_count: int = 999,
) -> list[FetchRequest]:
    """Construct a prioritized request list per the blueprint's tiered scanning.

    Tier 1: every cycle on all timeframes (priority 1)
    Tier 2: only 15m+ timeframes (priority 2)
    Tier 3: only 1h+ timeframes (priority 3)
    """
    LONG_TFS = {"15m", "20m", "30m", "1h", "2h", "4h", "1d", "1w", "1mo"}
    HOUR_PLUS = {"1h", "2h", "4h", "1d", "1w", "1mo"}

    reqs: list[FetchRequest] = []
    for ticker in universe_tier1:
        for tf in timeframes:
            reqs.append(FetchRequest(ticker, tf, candle_count, priority=1))
    for ticker in universe_tier2:
        for tf in timeframes:
            if tf in LONG_TFS:
                reqs.append(FetchRequest(ticker, tf, candle_count, priority=2))
    for ticker in universe_tier3:
        for tf in timeframes:
            if tf in HOUR_PLUS:
                reqs.append(FetchRequest(ticker, tf, candle_count, priority=3))
    return reqs
