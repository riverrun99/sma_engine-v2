"""
regime.py — Hidden Markov Model regime detection for outfit conditioning.

Replaces the blueprint's binary rule "long outfits more reliable when VIX
negative" with empirical regime detection. Fit a Gaussian HMM on market
features; identify regimes; condition expected outfit performance on regime.

Features used for regime detection (one observation per timestep):
  1. SPY 20-day return (trend)
  2. SPY 20-day realized volatility (vol regime)
  3. UVXY level (proxy for VIX absolute)
  4. UVXY 5-day return (vol direction)
  5. SMH/SPY ratio (semi leadership / risk-on signal)

States: typically 3 regimes works well — {risk-on, neutral, risk-off}.
The HMM discovers what these mean from the data; we label them post-hoc
by inspecting the emission means.

Output: regime sequence over history, plus a regime-conditional summary
table showing how each outfit performs in each regime.

This is *additive* to the framework, not replacing it. Outfits fire as
before; regime tags get attached so the user knows "this signal fired in
regime 2, where this outfit historically had Sharpe 1.4."
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional

try:
    from hmmlearn.hmm import GaussianHMM
    HMM_AVAILABLE = True
except ImportError:
    HMM_AVAILABLE = False


@dataclass
class RegimeModel:
    n_states: int
    state_labels: dict[int, str] = field(default_factory=dict)
    emission_means: np.ndarray = field(default_factory=lambda: np.array([]))
    transition_matrix: np.ndarray = field(default_factory=lambda: np.array([]))
    fitted: bool = False
    _hmm: Optional[object] = None
    _scaler_mean: Optional[np.ndarray] = None
    _scaler_std: Optional[np.ndarray] = None
    _feature_names: list[str] = field(default_factory=list)


def build_features(
    spy_df: pd.DataFrame,
    uvxy_df: pd.DataFrame,
    smh_df: pd.DataFrame,
    lookback_returns: int = 20,
    lookback_vol: int = 20,
) -> pd.DataFrame:
    """Construct regime-detection feature matrix.

    All inputs are daily bars with at least a 'close' and 'timestamp' column.
    Outputs a DataFrame with timestamp + 5 features, dropping NaN rows.
    """
    # Align all three dataframes by timestamp (inner join)
    spy = spy_df[["timestamp", "close"]].rename(columns={"close": "spy"})
    uvxy = uvxy_df[["timestamp", "close"]].rename(columns={"close": "uvxy"})
    smh = smh_df[["timestamp", "close"]].rename(columns={"close": "smh"})

    df = spy.merge(uvxy, on="timestamp").merge(smh, on="timestamp")
    df = df.sort_values("timestamp").reset_index(drop=True)

    # Features
    df["spy_return"] = df["spy"].pct_change(lookback_returns)
    df["spy_vol"] = df["spy"].pct_change().rolling(lookback_vol).std() * np.sqrt(252)
    df["uvxy_level"] = np.log(df["uvxy"])  # log scale, UVXY has wide range
    df["uvxy_return"] = df["uvxy"].pct_change(5)
    df["smh_spy_ratio"] = df["smh"] / df["spy"]
    df["smh_spy_ratio_return"] = df["smh_spy_ratio"].pct_change(lookback_returns)

    feature_cols = ["spy_return", "spy_vol", "uvxy_level", "uvxy_return", "smh_spy_ratio_return"]
    out = df[["timestamp"] + feature_cols].dropna().reset_index(drop=True)
    return out


def fit_regimes(
    features_df: pd.DataFrame,
    n_states: int = 3,
    n_iter: int = 100,
    random_state: int = 42,
) -> RegimeModel:
    """Fit Gaussian HMM on the feature matrix."""
    if not HMM_AVAILABLE:
        raise ImportError(
            "hmmlearn not installed. Install with: pip install hmmlearn"
        )

    feature_cols = [c for c in features_df.columns if c != "timestamp"]
    X = features_df[feature_cols].to_numpy()

    # Standardize features (HMM convergence is sensitive to scale)
    mu = X.mean(axis=0)
    sigma = X.std(axis=0)
    sigma[sigma == 0] = 1.0  # avoid div by zero
    X_scaled = (X - mu) / sigma

    hmm = GaussianHMM(
        n_components=n_states,
        covariance_type="diag",
        n_iter=n_iter,
        random_state=random_state,
    )
    hmm.fit(X_scaled)

    # Label states by inspecting unscaled emission means
    emission_means_scaled = hmm.means_
    emission_means = emission_means_scaled * sigma + mu

    # Heuristic labeling using SPY return + UVXY level
    state_labels = _label_states(emission_means, feature_cols)

    return RegimeModel(
        n_states=n_states,
        state_labels=state_labels,
        emission_means=emission_means,
        transition_matrix=hmm.transmat_,
        fitted=True,
        _hmm=hmm,
        _scaler_mean=mu,
        _scaler_std=sigma,
        _feature_names=feature_cols,
    )


def _label_states(emission_means: np.ndarray, feature_names: list[str]) -> dict[int, str]:
    """Assign human-readable labels to discovered states.

    Heuristic: state with highest SPY return + lowest UVXY level = 'risk-on';
    lowest SPY return + highest UVXY level = 'risk-off'; middle = 'neutral'.
    """
    spy_idx = feature_names.index("spy_return") if "spy_return" in feature_names else 0
    uvxy_idx = feature_names.index("uvxy_level") if "uvxy_level" in feature_names else 2

    n_states = emission_means.shape[0]
    # Composite "risk-on" score
    scores = emission_means[:, spy_idx] - emission_means[:, uvxy_idx]
    order = np.argsort(scores)  # ascending: most risk-off first

    labels = {}
    if n_states == 2:
        labels[order[0]] = "risk-off"
        labels[order[1]] = "risk-on"
    elif n_states == 3:
        labels[order[0]] = "risk-off"
        labels[order[1]] = "neutral"
        labels[order[2]] = "risk-on"
    else:
        # Generic numeric labels for >3
        for rank, state_id in enumerate(order):
            labels[state_id] = f"regime-{rank}"
    return labels


def predict_regimes(model: RegimeModel, features_df: pd.DataFrame) -> pd.DataFrame:
    """Predict regime sequence for the given feature matrix.

    Returns features_df with added columns: 'regime', 'regime_label',
    'regime_proba_<i>' for each state.
    """
    if not model.fitted or model._hmm is None:
        raise RuntimeError("Model not fitted. Call fit_regimes first.")

    X = features_df[model._feature_names].to_numpy()
    X_scaled = (X - model._scaler_mean) / model._scaler_std

    states = model._hmm.predict(X_scaled)
    proba = model._hmm.predict_proba(X_scaled)

    out = features_df.copy()
    out["regime"] = states
    out["regime_label"] = [model.state_labels.get(s, f"state-{s}") for s in states]
    for i in range(model.n_states):
        out[f"regime_proba_{i}"] = proba[:, i]
    return out


def regime_at_timestamp(
    regime_df: pd.DataFrame,
    ts: pd.Timestamp,
) -> Optional[tuple[int, str]]:
    """Look up the regime active at a given timestamp.

    Returns (regime_id, regime_label) or None if outside the regime series.
    """
    if regime_df.empty:
        return None
    # Find the most recent regime observation at or before ts
    mask = regime_df["timestamp"] <= ts
    if not mask.any():
        return None
    last = regime_df[mask].iloc[-1]
    return int(last["regime"]), str(last["regime_label"])


def regime_conditional_summary(
    trades: list,  # list[backtest.Trade]
    regime_df: pd.DataFrame,
) -> pd.DataFrame:
    """Group trades by regime, compute per-regime performance metrics."""
    if not trades or regime_df.empty:
        return pd.DataFrame()

    rows = []
    for t in trades:
        rg = regime_at_timestamp(regime_df, t.entry_time)
        if rg is None:
            continue
        rows.append({
            "regime_id": rg[0],
            "regime_label": rg[1],
            "ticker": t.ticker,
            "outfit_id": t.outfit_id,
            "return_pct": t.return_pct,
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    summary = df.groupby(["regime_id", "regime_label"]).agg(
        n_trades=("return_pct", "count"),
        avg_return=("return_pct", "mean"),
        std_return=("return_pct", "std"),
        win_rate=("return_pct", lambda x: float((x > 0).mean())),
    ).reset_index()
    # Sharpe per regime (annualized rough estimate)
    summary["sharpe"] = summary["avg_return"] / summary["std_return"].replace(0, np.nan) * np.sqrt(252)
    summary["sharpe"] = summary["sharpe"].fillna(0.0)
    return summary


def render_regime_summary(summary: pd.DataFrame) -> str:
    """Format regime summary as a text table."""
    if summary.empty:
        return "  (no regime-conditional data available)"
    lines = ["─" * 70, "  REGIME-CONDITIONAL PERFORMANCE", "─" * 70]
    lines.append(f"  {'Regime':<12} {'N':>6} {'Avg Ret':>10} {'Std':>8} {'Win%':>7} {'Sharpe':>8}")
    lines.append("─" * 70)
    for _, row in summary.iterrows():
        lines.append(
            f"  {row['regime_label']:<12} {int(row['n_trades']):>6} "
            f"{row['avg_return']:>+10.3%} {row['std_return']:>8.3%} "
            f"{row['win_rate']:>7.1%} {row['sharpe']:>8.2f}"
        )
    lines.append("─" * 70)
    return "\n".join(lines)
