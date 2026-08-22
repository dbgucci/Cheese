"""Daily pivot levels: the classic ladder plus the Fibonacci-ratio one.

Reverse-engineered from the "Smart Pivot Points (Daily -- M1/M5/M15/M30)"
overlay in the QT Sniper screen recordings. Three levels were legible on the
NAS100 chart:

    Fib 38.2% R = 30164.87      Fib 61.8% R = 30223.53      R1 = 30207.10

Two unknowns (the pivot and the prior day's range) fall straight out of the
two Fibonacci levels, and the classic ``R1 = 2P - L`` then solves the prior
session to H=30181.30, L=29932.74, C=30095.72 -- a self-consistent daily bar
with a 0.83% range, which is an ordinary day on that index. That the third
level lands exactly where those two predict is what identifies the formula
rather than merely fitting it.

The only thing that matters for correctness here is *which* session's data a
level is built from. A pivot for today uses yesterday's completed high, low
and close; using today's makes every level clairvoyant and every backtest
worthless. :func:`daily_levels` shifts for that, and the test suite checks it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# name -> (basis, coefficient) where basis is "range" for Fibonacci levels.
FIB_RATIOS = (0.382, 0.618, 1.000)


def levels_for(high: float, low: float, close: float) -> dict[str, float]:
    """The full ladder derived from one completed session."""
    p = (high + low + close) / 3.0
    rng = high - low
    out: dict[str, float] = {"P": p}
    # Classic: reflections of the prior range around the pivot.
    out["R1"] = 2 * p - low
    out["S1"] = 2 * p - high
    out["R2"] = p + rng
    out["S2"] = p - rng
    # Fibonacci: fractions of the prior range projected from the pivot.
    for r in FIB_RATIOS:
        tag = f"{r * 100:.1f}".rstrip("0").rstrip(".")
        out[f"R{tag}"] = p + r * rng
        out[f"S{tag}"] = p - r * rng
    return out


def daily_levels(daily: pd.DataFrame) -> pd.DataFrame:
    """Levels indexed by the day they apply to, built from the day before.

    ``daily`` is one row per session with high/low/close, indexed by date.
    """
    prev = daily.shift(1)
    rows = {}
    for ts, row in prev.iterrows():
        if row[["high", "low", "close"]].isna().any():
            continue
        rows[ts] = levels_for(float(row["high"]), float(row["low"]), float(row["close"]))
    return pd.DataFrame.from_dict(rows, orient="index").sort_index()


def to_daily(intraday: pd.DataFrame) -> pd.DataFrame:
    """Resample intraday bars into sessions. UTC days, which is what the feed uses."""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in intraday:
        agg["volume"] = "sum"
    return intraday.resample("1D").agg(agg).dropna(subset=["high", "low", "close"])


def align(intraday: pd.DataFrame, levels: pd.DataFrame) -> pd.DataFrame:
    """Attach each intraday bar to the level set in force that session."""
    key = intraday.index.normalize()
    return levels.reindex(key).set_index(intraday.index)


def ladder(levels_row: pd.Series) -> np.ndarray:
    """The session's levels as one sorted array, for nearest-level lookups."""
    vals = levels_row.to_numpy(dtype=float)
    vals = vals[np.isfinite(vals)]
    return np.sort(vals)
