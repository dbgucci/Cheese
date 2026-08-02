"""Heikin Ashi trend-continuation strategy and adaptive expiry selection.

The rules, taken directly from the chart setup being modelled:

    BUY  when Heikin Ashi closes above the Keltner mid-line (EMA 20),
             price is above the EMA 200,
             and a confirmed down-fractal (period 7) sits below.

    SELL when Heikin Ashi closes below the Keltner mid-line,
             price is below the EMA 200,
             and a confirmed up-fractal sits above.

Why this differs from ``liquidity_sweep``
-----------------------------------------
``liquidity_sweep`` is a *reversal* setup: it bets against the current move.
On a trending synthetic feed that means repeatedly stepping in front of the
trend, which is exactly what the logged results showed it doing. This
strategy is the opposite posture -- it only trades *with* the established
direction, and requires the slow trend (EMA 200), the fast trend (Keltner
mid), and market structure (fractal) to agree before doing so.

Two honesty constraints are baked in:

* Fractals are read from :func:`indicators.fractals`, which reports each
  fractal at the bar it could first be **confirmed**, not the bar it is
  centred on. A chart draws a fractal three bars in the past; a live bot
  cannot act on it until those three bars exist.
* Heikin Ashi is used only to read direction. Every entry, exit and outcome
  is priced from the **real** close, because that is what the option settles
  against.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import indicators as ind
from .strategies import DOWN, FLAT, UP, Signal

# Defaults mirroring the platform setup.
EMA_TREND = 200
KELTNER_EMA = 20
KELTNER_ATR = 10
KELTNER_MULT = 1.0
FRACTAL_PERIOD = 7

MIN_BARS = EMA_TREND + 20


def _ha_run_length(ha: pd.DataFrame) -> int:
    """How many consecutive Heikin Ashi candles share the current colour."""
    bull = (ha["close"] > ha["open"]).to_numpy()
    if len(bull) == 0:
        return 0
    current = bull[-1]
    run = 0
    for value in bull[::-1]:
        if value != current:
            break
        run += 1
    return run


def trend_continuation(
    df: pd.DataFrame,
    ema_trend: int = EMA_TREND,
    keltner_ema: int = KELTNER_EMA,
    keltner_atr: int = KELTNER_ATR,
    keltner_mult: float = KELTNER_MULT,
    fractal_period: int = FRACTAL_PERIOD,
    fractal_max_age: int = 2,
) -> Signal:
    """Score the most recent closed candle. Returns FLAT when nothing lines up.

    **The fractal is the trigger; the trend checks are filters.** That split
    matters. Heikin Ashi sits above the Keltner mid-line for essentially the
    whole of a sustained trend, so treating the Keltner cross as the trigger
    produces almost no signals during exactly the conditions this strategy
    exists to trade. A newly confirmed fractal is a discrete event that marks
    a completed pullback, which is the moment worth entering.

    ``fractal_max_age`` allows a fractal confirmed a bar or two ago to still
    count, since demanding the exact bar throws away otherwise valid setups
    for no reason.
    """
    if len(df) < MIN_BARS:
        return Signal(FLAT, 0.0, "insufficient history")

    close, high, low = df["close"], df["high"], df["low"]

    ha = ind.heikin_ashi(df)
    kc = ind.keltner_channel(
        high, low, close, ema_period=keltner_ema, atr_period=keltner_atr,
        multiplier=keltner_mult,
    )
    ema200 = ind.ema(close, ema_trend)
    frac = ind.fractals(high, low, period=fractal_period)
    atr_now = float(ind.atr(high, low, close).iloc[-1])

    ha_close_now = float(ha["close"].iloc[-1])
    mid_now = float(kc["mid"].iloc[-1])
    price_now = float(close.iloc[-1])
    ema_now = float(ema200.iloc[-1])

    if any(np.isnan(v) for v in (mid_now, ema_now, atr_now)) or atr_now <= 0:
        return Signal(FLAT, 0.0, "indicators not warmed up")

    above_kc = ha_close_now > mid_now
    below_kc = ha_close_now < mid_now
    above_ema = price_now > ema_now
    below_ema = price_now < ema_now

    # Most recent confirmed fractals, and how many bars ago they were known.
    up_idx = np.flatnonzero(frac["up"].to_numpy())
    down_idx = np.flatnonzero(frac["down"].to_numpy())
    last_up = int(up_idx[-1]) if len(up_idx) else None
    last_down = int(down_idx[-1]) if len(down_idx) else None
    bars = len(df) - 1

    ema_distance = abs(price_now - ema_now) / atr_now
    slope = (ema_now - float(ema200.iloc[-6])) / atr_now if len(df) > 6 else 0.0
    run = _ha_run_length(ha)

    def score(direction_ok_distance: float, slope_aligned: bool) -> float:
        s = 0.55
        s += 0.15 * min(direction_ok_distance / 2.0, 1.0)   # room above/below EMA200
        s += 0.15 if slope_aligned else -0.10               # EMA200 pointing our way
        s += 0.10 * min(run / 4.0, 1.0)                     # HA momentum
        return float(min(max(s, 0.0), 1.0))

    # Trigger: a fractal confirmed on this bar (or within fractal_max_age).
    up_fresh = last_up is not None and (bars - last_up) <= fractal_max_age
    down_fresh = last_down is not None and (bars - last_down) <= fractal_max_age

    if down_fresh and above_kc and above_ema:
        age = bars - last_down
        return Signal(
            UP,
            score(ema_distance, slope > 0),
            f"HA closed above Keltner mid, price {ema_distance:.1f} ATR above EMA200, "
            f"confirmed down-fractal {age} bars ago at {frac['down_price'].iloc[last_down]:.5f}",
            ["trend_continuation"],
            meta={"level": float(frac["down_price"].iloc[last_down]), "event": True},
        )

    if up_fresh and below_kc and below_ema:
        age = bars - last_up
        return Signal(
            DOWN,
            score(ema_distance, slope < 0),
            f"HA closed below Keltner mid, price {ema_distance:.1f} ATR below EMA200, "
            f"confirmed up-fractal {age} bars ago at {frac['up_price'].iloc[last_up]:.5f}",
            ["trend_continuation"],
            meta={"level": float(frac["up_price"].iloc[last_up]), "event": True},
        )

    return Signal(FLAT, 0.0, "no trend-continuation setup")


# ----------------------------- adaptive expiry -----------------------------
@dataclass
class ExpiryAdvice:
    minutes: int
    reason: str


def recommend_expiry(
    df: pd.DataFrame,
    direction: int,
    min_minutes: int = 1,
    max_minutes: int = 5,
) -> ExpiryAdvice:
    """Suggest how many minutes to hold, from how long moves have been lasting.

    The logic is deliberately simple and measurable: estimate the typical
    length of a directional run in the recent data, and hold for a fraction of
    it. A long expiry on a market that reverses every two candles just gives
    the move time to come back.

    This is a *prior*, not a truth. ``cheese-signals backtest --sweep-expiry``
    reports realised win rate for every expiry from 1 to 5 minutes on your own
    data, and that is what should settle the question.
    """
    if len(df) < 60:
        return ExpiryAdvice(min_minutes, "not enough history; using the shortest expiry")

    ha = ind.heikin_ashi(df)
    bull = (ha["close"] > ha["open"]).to_numpy()

    # Lengths of consecutive same-colour runs over the recent window.
    runs, current = [], 1
    for i in range(1, len(bull)):
        if bull[i] == bull[i - 1]:
            current += 1
        else:
            runs.append(current)
            current = 1
    runs.append(current)
    recent = runs[-40:] if len(runs) > 40 else runs
    median_run = float(np.median(recent)) if recent else 1.0

    atr_series = ind.atr(df["high"], df["low"], df["close"])
    atr_now = float(atr_series.iloc[-1])
    atr_avg = float(atr_series.tail(60).mean())
    volatile = atr_avg > 0 and atr_now > atr_avg * 1.3

    # Hold for roughly half a typical run: long enough for the move to
    # develop, short enough to exit before the usual reversal.
    target = int(round(median_run / 2.0))
    if volatile:
        target -= 1  # fast markets retrace sooner
    minutes = max(min_minutes, min(max_minutes, target))

    note = (
        f"typical directional run is {median_run:.0f} candles"
        + (", volatility elevated" if volatile else "")
    )
    return ExpiryAdvice(minutes, note)
