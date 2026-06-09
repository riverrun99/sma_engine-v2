"""
backtest.py — backtesting harness with walk-forward and combinatorial purged CV.

Goal: validate that signals from the SMA outfit engine produce returns out of
sample, not just hit counts in sample.

Two CV schemes:
  1. Walk-forward: train on [t0, t1], test on [t1, t2], slide window forward.
     Standard for time series, no leakage.
  2. Combinatorial Purged CV (López de Prado, Advances in Financial Machine
     Learning, ch. 7): split history into N folds, train on combinations of
     folds, test on the held-out folds. Apply a "purge" gap between train and
     test windows to prevent label leakage from overlapping target horizons.

Signal evaluation:
  When the engine produces a top-ranked signal at time T (entry_price P),
  measure forward return at horizon h: r = (price_at_T+h - P) / P.
  Apply directional rule: outfit hits during VIX-negative regime → long bias.
  (For symmetric evaluation we also compute the magnitude of move and a
  signed Sharpe.)

Outputs:
  - Per-fold returns
  - Aggregate Sharpe ratio, hit rate, average return
  - Equity curve
  - Distribution of returns (mean, std, skew, max DD)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from itertools import combinations
from typing import Optional, Callable

from engine import (
    detect_hits, HashMapStore, OUTFITS, EngineConfig,
    rank_entries, best_offset,
)


@dataclass
class Trade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    ticker: str
    timeframe: str
    outfit_id: int
    entry_price: float
    exit_price: float
    horizon_bars: int
    return_pct: float
    direction: int  # +1 long, -1 short, 0 flat


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    n_trades: int = 0
    win_rate: float = 0.0
    avg_return: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    total_return: float = 0.0

    def summarize(self) -> str:
        lines = [
            "─" * 60,
            "  BACKTEST RESULTS",
            "─" * 60,
            f"  Trades:         {self.n_trades}",
            f"  Win rate:       {self.win_rate:.1%}",
            f"  Avg return:     {self.avg_return:+.3%}",
            f"  Total return:   {self.total_return:+.2%}",
            f"  Sharpe (annl):  {self.sharpe:.2f}",
            f"  Max drawdown:   {self.max_drawdown:.2%}",
            "─" * 60,
        ]
        return "\n".join(lines)


def _compute_metrics(trades: list[Trade], periods_per_year: float = 252.0) -> BacktestResult:
    """Standard performance metrics from a list of trades."""
    if not trades:
        return BacktestResult()

    returns = np.array([t.return_pct for t in trades])
    win_rate = float(np.mean(returns > 0))
    avg_return = float(np.mean(returns))

    # Sharpe: annualized. We treat each trade as one "period" — this is a
    # rough approximation. For a proper Sharpe, use returns sampled at fixed
    # intervals. Good enough for relative comparison.
    if returns.std() > 0:
        # If avg horizon is known, scale; here we just use trade-level
        sharpe = float(returns.mean() / returns.std() * np.sqrt(periods_per_year))
    else:
        sharpe = 0.0

    # Equity curve and max drawdown
    equity = np.cumprod(1 + returns)
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    max_dd = float(dd.min()) if len(dd) > 0 else 0.0
    total_return = float(equity[-1] - 1) if len(equity) > 0 else 0.0

    return BacktestResult(
        trades=trades,
        equity_curve=equity.tolist(),
        n_trades=len(trades),
        win_rate=win_rate,
        avg_return=avg_return,
        sharpe=sharpe,
        max_drawdown=max_dd,
        total_return=total_return,
    )


def evaluate_signal(
    df: pd.DataFrame,
    signal_idx: int,
    entry_price: float,
    horizon_bars: int,
    direction: int = 1,
) -> Optional[Trade]:
    """Compute the trade outcome for a signal fired at signal_idx.

    Entry at entry_price (post-offset) at signal_idx + 1 (next bar open).
    Exit at signal_idx + horizon_bars close.
    """
    entry_idx = signal_idx + 1
    exit_idx = signal_idx + horizon_bars

    if exit_idx >= len(df):
        return None

    actual_entry = float(df["open"].iloc[entry_idx])
    actual_exit = float(df["close"].iloc[exit_idx])
    if actual_entry == 0:
        return None
    return_pct = direction * (actual_exit - actual_entry) / actual_entry

    return Trade(
        entry_time=df["timestamp"].iloc[entry_idx],
        exit_time=df["timestamp"].iloc[exit_idx],
        ticker="",  # filled in by caller
        timeframe="",
        outfit_id=0,
        entry_price=actual_entry,
        exit_price=actual_exit,
        horizon_bars=horizon_bars,
        return_pct=return_pct,
        direction=direction,
    )


def walk_forward(
    df: pd.DataFrame,
    ticker: str,
    timeframe: str,
    outfit: dict,
    train_size: int = 500,
    test_size: int = 100,
    horizon_bars: int = 10,
    direction_fn: Optional[Callable] = None,
) -> BacktestResult:
    """Walk-forward backtest for one [ticker × tf × outfit].

    For each window:
      1. Train: bars [t0, t0+train_size]
         (used to compute SMAs — no fitting, but establishes the SMA values)
      2. Test: bars [t0+train_size, t0+train_size+test_size]
         For each bar in test window where a hit occurs, fire a trade.
      3. Slide window forward by test_size, repeat.

    direction_fn(df, idx) -> int (+1/-1/0). If None, defaults to long-only.
    """
    if direction_fn is None:
        direction_fn = lambda d, i: 1  # long-only default

    trades: list[Trade] = []
    n = len(df)
    if n < train_size + test_size + horizon_bars:
        return BacktestResult()

    t0 = 0
    while t0 + train_size + test_size + horizon_bars < n:
        # Use the training window's tail + test window for SMA computation
        # so SMAs at test bars are computed from genuinely past data
        window_end = t0 + train_size + test_size
        window_df = df.iloc[:window_end].reset_index(drop=True)

        # Detect hits within the test portion only
        all_hits = detect_hits(window_df, ticker, timeframe, outfit, lookback=test_size)

        # Filter to hits in the test window
        test_start_idx = t0 + train_size
        test_hits = [h for h in all_hits if h.bar_index >= test_start_idx]

        # Group hits by bar index — multiple SMA hits on same bar = one trade
        seen_bars = set()
        for h in test_hits:
            if h.bar_index in seen_bars:
                continue
            seen_bars.add(h.bar_index)

            direction = direction_fn(df, h.bar_index)
            if direction == 0:
                continue

            trade = evaluate_signal(df, h.bar_index, h.price, horizon_bars, direction)
            if trade is not None:
                trade.ticker = ticker
                trade.timeframe = timeframe
                trade.outfit_id = outfit["id"]
                trades.append(trade)

        t0 += test_size

    return _compute_metrics(trades)


def combinatorial_purged_cv(
    df: pd.DataFrame,
    ticker: str,
    timeframe: str,
    outfit: dict,
    n_folds: int = 6,
    n_test_folds: int = 2,
    purge_bars: int = 20,
    horizon_bars: int = 10,
    direction_fn: Optional[Callable] = None,
) -> BacktestResult:
    """Combinatorial Purged CV per López de Prado.

    Split history into n_folds equal segments. For each combination of
    n_test_folds held out as test, treat the rest as train. Apply a purge
    gap of purge_bars between train and test to prevent label leakage from
    overlapping forward-return horizons.

    With n_folds=6, n_test_folds=2 → C(6,2) = 15 distinct test combinations.
    """
    if direction_fn is None:
        direction_fn = lambda d, i: 1

    n = len(df)
    if n < n_folds * 50:
        return BacktestResult()

    fold_size = n // n_folds
    fold_boundaries = [(i * fold_size, (i + 1) * fold_size) for i in range(n_folds)]

    all_trades: list[Trade] = []

    for test_combo in combinations(range(n_folds), n_test_folds):
        test_folds = set(test_combo)
        # Build mask: True = test region (after purge), False = train/purged
        is_test = np.zeros(n, dtype=bool)
        for fold_id in test_folds:
            start, end = fold_boundaries[fold_id]
            # Purge: skip the first purge_bars of each test fold to prevent
            # leakage from training horizon overlapping into test
            test_start = max(start, start + purge_bars) if fold_id > 0 else start
            is_test[test_start:end] = True

        # Detect hits across the full series, then keep only test-region hits
        hits = detect_hits(df, ticker, timeframe, outfit, lookback=n)
        test_hits = [h for h in hits if is_test[h.bar_index]]

        seen_bars = set()
        for h in test_hits:
            if h.bar_index in seen_bars:
                continue
            seen_bars.add(h.bar_index)
            direction = direction_fn(df, h.bar_index)
            if direction == 0:
                continue
            trade = evaluate_signal(df, h.bar_index, h.price, horizon_bars, direction)
            if trade is not None:
                trade.ticker = ticker
                trade.timeframe = timeframe
                trade.outfit_id = outfit["id"]
                all_trades.append(trade)

    return _compute_metrics(all_trades)


def backtest_top_signals(
    candle_cache: dict[tuple[str, str], pd.DataFrame],
    top_entries: list,  # list[HashMapEntry]
    outfits_by_id: dict[int, dict],
    method: str = "walk_forward",
    horizon_bars: int = 10,
    **kwargs,
) -> dict[str, BacktestResult]:
    """Backtest each top-ranked entry. Returns {entry_key: BacktestResult}."""
    results: dict[str, BacktestResult] = {}
    for entry in top_entries:
        df = candle_cache.get((entry.ticker, entry.timeframe))
        if df is None or len(df) == 0:
            continue
        outfit = outfits_by_id.get(entry.outfit_id)
        if outfit is None:
            continue
        key = f"{entry.ticker}|{entry.timeframe}|{entry.outfit_id}"
        if method == "walk_forward":
            results[key] = walk_forward(
                df, entry.ticker, entry.timeframe, outfit,
                horizon_bars=horizon_bars, **kwargs,
            )
        elif method == "cpcv":
            results[key] = combinatorial_purged_cv(
                df, entry.ticker, entry.timeframe, outfit,
                horizon_bars=horizon_bars, **kwargs,
            )
        else:
            raise ValueError(f"Unknown method: {method}")
    return results
