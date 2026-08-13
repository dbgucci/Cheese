"""Bar features for mining, and the one label they are mined against.

Two rules govern this file, and breaking either is how a backtest starts
lying:

**No lookahead.** Every feature at bar *i* is computed from bars <= *i*.
Only ``label`` looks forward, by exactly one bar, because that is the trade:
enter at the close of bar *i*, settle at the close of bar *i+1*.

**No full-sample statistics.** Bucketing a feature by its percentile over the
whole dataset leaks the future into the past -- bar 10 gets told where it
sits relative to bar 10,000. So continuous features are bucketed with edges
fit on the training split alone (``fit_bins``) and then applied unchanged to
validation and test (``apply_bins``). It is a small detail that quietly
inflates a great many published backtests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from ..indicators import atr, ema, rsi

# Candle direction constants (match the engine's UP/DOWN/FLAT convention).
UP, DOWN, FLAT = 1, -1, 0


def label(df: pd.DataFrame) -> pd.Series:
    """Outcome of a 1-bar binary option entered at this bar's close.

    +1 the next close is higher (a CALL wins), -1 lower (a PUT wins), 0 the
    next close is identical -- a refund on Pocket Option, not a loss. The last
    bar has no outcome and is NaN.
    """
    nxt = df["close"].shift(-1)
    delta = nxt - df["close"]
    out = pd.Series(np.sign(delta), index=df.index, dtype="float64")
    out[delta.isna()] = np.nan
    return out


def compute(df: pd.DataFrame) -> pd.DataFrame:
    """Backward-looking features for every bar.

    Names are grouped by family so the miner can report which *kind* of
    structure a surviving rule came from, rather than an opaque column name.
    """
    close, high, low, open_ = df["close"], df["high"], df["low"], df["open"]
    out = pd.DataFrame(index=df.index)

    # --- shape of the current candle -----------------------------------
    rng = (high - low).replace(0.0, np.nan)
    out["body_frac"] = (close - open_) / rng
    out["upper_wick"] = (high - np.maximum(open_, close)) / rng
    out["lower_wick"] = (np.minimum(open_, close) - low) / rng
    out["range_bp"] = rng / close * 10_000.0  # basis points, comparable across pairs

    # --- returns and momentum ------------------------------------------
    ret = close.pct_change()
    out["ret_bp"] = ret * 10_000.0
    vol = ret.rolling(60, min_periods=20).std()
    out["ret_z"] = ret / vol.replace(0.0, np.nan)

    for lag in (1, 2, 3):
        out[f"ret_lag{lag}_bp"] = out["ret_bp"].shift(lag)

    # --- direction and streaks -----------------------------------------
    direction = np.sign(close.diff())
    out["direction"] = direction
    out["streak"] = _signed_streak(direction)

    # --- indicator state ------------------------------------------------
    out["rsi14"] = rsi(close, 14)
    atr14 = atr(high, low, close, 14)
    out["atr_bp"] = atr14 / close * 10_000.0
    out["ema20_dist"] = (close - ema(close, 20)) / atr14.replace(0.0, np.nan)

    # Trailing volatility percentile: where does this bar's range sit
    # against the last 4 hours? Rolling, never full-sample.
    out["vol_pctile"] = (
        out["range_bp"].rolling(240, min_periods=60).rank(pct=True)
    )

    # --- clock ----------------------------------------------------------
    idx = df.index
    out["hour"] = idx.hour
    out["minute"] = idx.minute
    out["minute_mod5"] = idx.minute % 5
    out["minute_mod15"] = idx.minute % 15
    out["dow"] = idx.dayofweek
    out["is_weekend"] = (idx.dayofweek >= 5).astype(int)

    return out


def _signed_streak(direction: pd.Series) -> pd.Series:
    """Length of the current run of same-direction closes, signed.

    +3 means three consecutive higher closes ending at this bar; -2 means two
    consecutive lower closes. Flat bars reset the run to 0. Computed with a
    plain loop over a numpy array -- a vectorised version of a
    reset-on-change counter is unreadable and this runs once per asset.
    """
    values = direction.to_numpy(dtype="float64", na_value=0.0)
    out = np.zeros(len(values))
    run = 0.0
    for i, sign in enumerate(values):
        if sign == 0 or np.isnan(sign):
            run = 0.0
        elif run != 0.0 and np.sign(run) == sign:
            run += sign
        else:
            run = sign
        out[i] = run
    return pd.Series(out, index=direction.index)


# ---------------------------------------------------------------------------
# Bucketing, fit on train only
# ---------------------------------------------------------------------------

# Features that are already discrete: bucket them by value, not by quantile.
CATEGORICAL = {
    "direction",
    "streak",
    "hour",
    "minute",
    "minute_mod5",
    "minute_mod15",
    "dow",
    "is_weekend",
}


@dataclass
class BinSpec:
    """Frozen bucket edges for one continuous feature."""

    feature: str
    edges: np.ndarray
    labels: list[str]


def fit_bins(features: pd.DataFrame, n_bins: int = 5) -> dict[str, BinSpec]:
    """Learn quantile edges from the training split only."""
    specs: dict[str, BinSpec] = {}

    for name in features.columns:
        if name in CATEGORICAL:
            continue
        series = features[name].replace([np.inf, -np.inf], np.nan).dropna()
        if len(series) < n_bins * 20:
            continue

        quantiles = np.linspace(0.0, 1.0, n_bins + 1)
        edges = np.unique(np.quantile(series, quantiles))
        if len(edges) < 3:
            continue  # too degenerate to split meaningfully

        # Open the outer edges so out-of-sample extremes still land in a bin.
        edges[0], edges[-1] = -np.inf, np.inf
        labels = [f"q{i + 1}" for i in range(len(edges) - 1)]
        specs[name] = BinSpec(feature=name, edges=edges, labels=labels)

    return specs


def apply_bins(features: pd.DataFrame, specs: dict[str, BinSpec]) -> pd.DataFrame:
    """Apply frozen edges to any split. Produces string-valued bucket columns."""
    out = pd.DataFrame(index=features.index)

    for name in features.columns:
        if name in CATEGORICAL:
            out[name] = features[name].astype("Int64").astype("string")
            continue
        spec = specs.get(name)
        if spec is None:
            continue
        binned = pd.cut(
            features[name].replace([np.inf, -np.inf], np.nan),
            bins=spec.edges,
            labels=spec.labels,
        )
        out[name] = binned.astype("string")

    return out


def clip_streak(features: pd.DataFrame, limit: int = 5) -> pd.DataFrame:
    """Cap streak length so rare deep runs do not become one-sample buckets."""
    out = features.copy()
    if "streak" in out.columns:
        out["streak"] = out["streak"].clip(-limit, limit)
    return out
