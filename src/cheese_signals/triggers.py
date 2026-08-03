"""Entry triggers: what decides *when* to act, once a setup is otherwise valid.

Separating the trigger from the setup matters because they have different
requirements. A setup may lag -- identifying a trend or a level from history is
fine. A **trigger must not**, because every candle of lag is a candle of the
move you no longer capture, and on a one-minute expiry there are very few to
spare.

Three triggers, from slowest to fastest:

``fractal``   A confirmed period-N fractal. Structurally the slowest: a fractal
              centred on bar *i* cannot be known until bar *i + wing*, so it is
              three bars late by construction on the default period of 7. Kept
              because it is what the charting platform draws.

``bos``       Break of structure. Uses the (lagging) fractal only to locate the
              level, then fires the moment the current candle *closes through*
              it. The level is allowed to lag; the break is detected on the bar
              you are standing on, so this adds no lag of its own.

``momentum``  Pure current-bar thrust: the close finishes in the top or bottom
              fraction of the bar's range, on a bar wider than an ATR multiple.
              Zero lag by construction, and correspondingly the noisiest.

Faster is not automatically better -- a faster trigger fires on more noise. The
point of making these selectable is that the trade-off is measurable on your
own data rather than argued about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from . import indicators as ind
from .strategies import DOWN, FLAT, UP

TRIGGER_FRACTAL = "fractal"
TRIGGER_BOS = "bos"
TRIGGER_MOMENTUM = "momentum"
TRIGGERS = (TRIGGER_FRACTAL, TRIGGER_BOS, TRIGGER_MOMENTUM)

TRIGGER_HELP = {
    TRIGGER_FRACTAL: (
        "Waits for a confirmed fractal. Slowest: a period-7 fractal is only "
        "knowable 3 candles after it forms, so entries are structurally late."
    ),
    TRIGGER_BOS: (
        "Break of structure. Uses the fractal to find the level, then fires the "
        "candle that closes through it — no extra lag. Usually the best balance."
    ),
    TRIGGER_MOMENTUM: (
        "Current-bar thrust only: a wide bar closing near its extreme. Fastest "
        "and noisiest; expect many more signals."
    ),
}


@dataclass
class TriggerResult:
    fired: bool
    direction: int = FLAT
    detail: str = ""
    level: Optional[float] = None

    def __bool__(self) -> bool:
        return self.fired


@dataclass
class TriggerConfig:
    """Everything a trigger can be tuned by, all user-editable."""

    kind: str = TRIGGER_BOS
    fractal_period: int = 7
    fractal_max_age: int = 5
    # bos
    bos_lookback: int = 30          # how far back to look for the swing to break
    bos_buffer_atr: float = 0.0     # require the break to clear the level by this much ATR
    # momentum
    momentum_close_pct: float = 0.70   # close must be in this top/bottom fraction of range
    momentum_range_atr: float = 0.80   # bar range must exceed this many ATR


def _confirmed_fractals(df: pd.DataFrame, period: int):
    f = ind.fractals(df["high"], df["low"], period=period)
    ups = np.flatnonzero(f["up"].to_numpy())
    downs = np.flatnonzero(f["down"].to_numpy())
    return f, ups, downs


def fractal_trigger(df: pd.DataFrame, bias: int, cfg: TriggerConfig) -> TriggerResult:
    """Fire when a fractal in the pullback direction was confirmed recently."""
    f, ups, downs = _confirmed_fractals(df, cfg.fractal_period)
    bars = len(df) - 1
    # An uptrend pulls back to a swing LOW, so a BUY waits on a down-fractal.
    idx = downs if bias == UP else ups
    label = "down-fractal (swing low)" if bias == UP else "up-fractal (swing high)"
    if len(idx) == 0:
        return TriggerResult(False, detail=f"no confirmed {label} in range")
    age = bars - int(idx[-1])
    price_col = "down_price" if bias == UP else "up_price"
    level = float(f[price_col].iloc[int(idx[-1])])
    if age > cfg.fractal_max_age:
        return TriggerResult(
            False, detail=f"last {label} was {age} bars ago, older than the "
                          f"{cfg.fractal_max_age}-bar window", level=level,
        )
    return TriggerResult(True, bias, f"confirmed {label} {age} bars ago at {level:.5f}", level)


def bos_trigger(df: pd.DataFrame, bias: int, cfg: TriggerConfig) -> TriggerResult:
    """Fire when this candle closes through the last swing in the trend direction.

    For a BUY the relevant level is the most recent swing *high* -- the top of
    the pullback. Closing above it says the pullback is over and the trend has
    resumed, and that is known on the current bar rather than three bars later.
    """
    f, ups, downs = _confirmed_fractals(df, cfg.fractal_period)
    close = float(df["close"].iloc[-1])
    prev_close = float(df["close"].iloc[-2])
    atr_now = float(ind.atr(df["high"], df["low"], df["close"]).iloc[-1])
    buffer = cfg.bos_buffer_atr * (atr_now if atr_now == atr_now else 0.0)
    bars = len(df) - 1

    idx = ups if bias == UP else downs
    col = "up_price" if bias == UP else "down_price"
    label = "swing high" if bias == UP else "swing low"

    # Only swings inside the lookback are still structurally relevant.
    recent = [i for i in idx if bars - int(i) <= cfg.bos_lookback]
    if not recent:
        return TriggerResult(False, detail=f"no {label} within {cfg.bos_lookback} bars to break")

    level = float(f[col].iloc[int(recent[-1])])
    if bias == UP:
        broke = close > level + buffer and prev_close <= level + buffer
        cmp_txt = f"close {close:.5f} > {label} {level:.5f}"
    else:
        broke = close < level - buffer and prev_close >= level - buffer
        cmp_txt = f"close {close:.5f} < {label} {level:.5f}"

    if not broke:
        return TriggerResult(False, detail=f"no break yet ({cmp_txt} not satisfied)", level=level)
    return TriggerResult(True, bias, f"broke structure: {cmp_txt}", level)


def momentum_trigger(df: pd.DataFrame, bias: int, cfg: TriggerConfig) -> TriggerResult:
    """Fire on a wide current bar closing near its extreme, in the trend direction."""
    o = float(df["open"].iloc[-1]); h = float(df["high"].iloc[-1])
    l = float(df["low"].iloc[-1]);  c = float(df["close"].iloc[-1])
    rng = h - l
    if rng <= 0:
        return TriggerResult(False, detail="zero-range bar")

    atr_now = float(ind.atr(df["high"], df["low"], df["close"]).iloc[-1])
    if atr_now != atr_now or atr_now <= 0:
        return TriggerResult(False, detail="ATR not ready")

    wide = rng >= cfg.momentum_range_atr * atr_now
    pos = (c - l) / rng          # 1.0 = closed at the high, 0.0 = at the low
    aligned = pos >= cfg.momentum_close_pct if bias == UP else pos <= (1 - cfg.momentum_close_pct)

    if not wide:
        return TriggerResult(
            False, detail=f"bar range {rng / atr_now:.2f} ATR below the "
                          f"{cfg.momentum_range_atr:.2f} ATR minimum")
    if not aligned:
        return TriggerResult(
            False, detail=f"close sits at {pos:.0%} of the bar range, "
                          f"not in the required {cfg.momentum_close_pct:.0%} extreme")
    return TriggerResult(
        True, bias,
        f"momentum bar: {rng / atr_now:.2f} ATR wide, closed at {pos:.0%} of range",
        c,
    )


def evaluate(df: pd.DataFrame, bias: int, cfg: TriggerConfig) -> TriggerResult:
    """Run the configured trigger. ``bias`` is the direction the setup wants."""
    if bias == FLAT:
        return TriggerResult(False, detail="no directional bias")
    if cfg.kind == TRIGGER_FRACTAL:
        return fractal_trigger(df, bias, cfg)
    if cfg.kind == TRIGGER_BOS:
        return bos_trigger(df, bias, cfg)
    if cfg.kind == TRIGGER_MOMENTUM:
        return momentum_trigger(df, bias, cfg)
    return TriggerResult(False, detail=f"unknown trigger {cfg.kind!r}")
