"""Individual strategies, each scoring one candle and returning a Signal.

The point of splitting these apart (rather than one "buy when RSI < 30"
script) is that each strategy is only reliable in a specific market regime:

- ``trend_following``   -> works when the market is actually trending (ADX high)
- ``mean_reversion``    -> works when the market is range-bound (ADX low)
- ``price_action``      -> works at structural support/resistance regardless of regime

Fired independently, each one racks up false signals in the regime it
wasn't designed for. The confluence engine in ``confluence.py`` uses the
ADX regime filter to decide which of these to even listen to, then
requires agreement from more than one before calling it a signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from . import indicators as ind

UP = 1
DOWN = -1
FLAT = 0


@dataclass
class Signal:
    direction: int  # UP, DOWN, or FLAT
    score: float  # 0..1 confidence within this single strategy
    reason: str
    tags: list[str] = field(default_factory=list)

    @property
    def is_actionable(self) -> bool:
        return self.direction != FLAT and self.score > 0


def _last(series: pd.Series, i: int = -1) -> float:
    return float(series.iloc[i])


def trend_following(df: pd.DataFrame, adx_min: float = 20.0) -> Signal:
    """EMA(9)/EMA(21) cross confirmed by MACD histogram, gated by ADX trend strength."""
    close, high, low = df["close"], df["high"], df["low"]

    ema_fast = ind.ema(close, 9)
    ema_slow = ind.ema(close, 21)
    macd_df = ind.macd(close)
    adx = ind.adx(high, low, close)

    if len(df) < 30 or pd.isna(adx.iloc[-1]):
        return Signal(FLAT, 0.0, "insufficient history")

    adx_now = _last(adx)
    if adx_now < adx_min:
        return Signal(FLAT, 0.0, f"no trend (ADX {adx_now:.1f} < {adx_min})")

    fast_now, fast_prev = _last(ema_fast), _last(ema_fast, -2)
    slow_now, slow_prev = _last(ema_slow), _last(ema_slow, -2)
    hist_now = _last(macd_df["hist"])

    crossed_up = fast_prev <= slow_prev and fast_now > slow_now
    crossed_down = fast_prev >= slow_prev and fast_now < slow_now
    aligned_up = fast_now > slow_now and hist_now > 0
    aligned_down = fast_now < slow_now and hist_now < 0

    strength = min(adx_now / 50.0, 1.0)

    if crossed_up:
        return Signal(UP, min(0.6 + strength * 0.4, 1.0), f"EMA9/21 bull cross, ADX {adx_now:.1f}", ["trend"])
    if crossed_down:
        return Signal(DOWN, min(0.6 + strength * 0.4, 1.0), f"EMA9/21 bear cross, ADX {adx_now:.1f}", ["trend"])
    if aligned_up:
        return Signal(UP, min(0.4 + strength * 0.3, 0.85), f"trend continuation up, ADX {adx_now:.1f}", ["trend"])
    if aligned_down:
        return Signal(DOWN, min(0.4 + strength * 0.3, 0.85), f"trend continuation down, ADX {adx_now:.1f}", ["trend"])
    return Signal(FLAT, 0.0, "no EMA/MACD agreement")


def mean_reversion(df: pd.DataFrame, adx_max: float = 20.0) -> Signal:
    """RSI + Bollinger %B extremes confirmed by Stochastic K/D cross, gated to ranging markets."""
    close, high, low = df["close"], df["high"], df["low"]

    if len(df) < 30:
        return Signal(FLAT, 0.0, "insufficient history")

    adx_now = _last(ind.adx(high, low, close))
    if pd.isna(adx_now):
        return Signal(FLAT, 0.0, "insufficient history")
    if adx_now > adx_max:
        return Signal(FLAT, 0.0, f"trending, not ranging (ADX {adx_now:.1f} > {adx_max})")

    rsi_now = _last(ind.rsi(close))
    bb = ind.bollinger_bands(close)
    pct_b_now = _last(bb["pct_b"])
    stoch = ind.stochastic(high, low, close)
    k_now, k_prev = _last(stoch["k"]), _last(stoch["k"], -2)
    d_now, d_prev = _last(stoch["d"]), _last(stoch["d"], -2)

    if any(pd.isna(v) for v in (rsi_now, pct_b_now, k_now, d_now)):
        return Signal(FLAT, 0.0, "insufficient history")

    stoch_cross_up = k_prev <= d_prev and k_now > d_now and k_now < 30
    stoch_cross_down = k_prev >= d_prev and k_now < d_now and k_now > 70

    oversold = rsi_now < 30 and pct_b_now < 0.05
    overbought = rsi_now > 70 and pct_b_now > 0.95

    if oversold and stoch_cross_up:
        score = min(0.5 + (30 - rsi_now) / 60.0, 1.0)
        return Signal(UP, score, f"oversold reversal: RSI {rsi_now:.1f}, %B {pct_b_now:.2f}, stoch cross", ["mean_reversion"])
    if overbought and stoch_cross_down:
        score = min(0.5 + (rsi_now - 70) / 60.0, 1.0)
        return Signal(DOWN, score, f"overbought reversal: RSI {rsi_now:.1f}, %B {pct_b_now:.2f}, stoch cross", ["mean_reversion"])
    return Signal(FLAT, 0.0, "no extreme + confirmation")


def price_action(df: pd.DataFrame, lookback: int = 20) -> Signal:
    """Engulfing / pin-bar rejection candles at rolling support or resistance."""
    if len(df) < lookback + 2:
        return Signal(FLAT, 0.0, "insufficient history")

    o, h, l, c = df["open"], df["high"], df["low"], df["close"]

    support = l.iloc[-(lookback + 1):-1].min()
    resistance = h.iloc[-(lookback + 1):-1].max()

    o1, h1, l1, c1 = o.iloc[-2], h.iloc[-2], l.iloc[-2], c.iloc[-2]
    o0, h0, l0, c0 = o.iloc[-1], h.iloc[-1], l.iloc[-1], c.iloc[-1]

    body0 = abs(c0 - o0)
    range0 = max(h0 - l0, 1e-9)
    lower_wick = min(o0, c0) - l0
    upper_wick = h0 - max(o0, c0)

    near_support = l0 <= support * 1.0015
    near_resistance = h0 >= resistance * 0.9985

    bullish_engulf = c1 < o1 and c0 > o0 and c0 >= o1 and o0 <= c1
    bearish_engulf = c1 > o1 and c0 < o0 and c0 <= o1 and o0 >= c1
    bullish_pin = near_support and lower_wick > body0 * 2 and lower_wick > range0 * 0.5
    bearish_pin = near_resistance and upper_wick > body0 * 2 and upper_wick > range0 * 0.5

    if near_support and (bullish_engulf or bullish_pin):
        tag = "engulfing" if bullish_engulf else "pin bar"
        return Signal(UP, 0.65, f"bullish {tag} at support {support:.5f}", ["price_action"])
    if near_resistance and (bearish_engulf or bearish_pin):
        tag = "engulfing" if bearish_engulf else "pin bar"
        return Signal(DOWN, 0.65, f"bearish {tag} at resistance {resistance:.5f}", ["price_action"])
    return Signal(FLAT, 0.0, "no rejection pattern at S/R")


def higher_timeframe_bias(df_htf: pd.DataFrame) -> int:
    """Directional bias from a higher timeframe: EMA50 vs EMA200 slope, used as a trade filter."""
    close = df_htf["close"]
    if len(df_htf) < 210:
        return FLAT
    fast = ind.ema(close, 50)
    slow = ind.ema(close, 200)
    if _last(fast) > _last(slow):
        return UP
    if _last(fast) < _last(slow):
        return DOWN
    return FLAT
