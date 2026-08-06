"""The three selectable setups, each with its own editable parameters.

A *setup* decides the direction it wants to trade and why. A *trigger* (see
``triggers.py``) decides when to act. Keeping them separate means any setup can
be paired with any trigger, and both can be changed from Settings without a new
build.

``trend_continuation``  Trade with the trend: Heikin Ashi on the trend side of
                        the Keltner mid, price on the same side of the EMA 200.
                        Wants a pullback to resolve in the trend's favour.

``support_resistance``  Trade rejections at a level: price reaches a rolling
                        high/low band and turns away from it. Direction is
                        *away* from the level.

``reversal``            Trade exhaustion against the current move: an RSI
                        extreme with a Bollinger band tag. Direction opposes
                        the move that produced it.

These are genuinely different postures and can disagree with each other by
design. Only one runs at a time; the conflicts are documented in the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import indicators as ind
from . import triggers as trig
from .strategies import DOWN, FLAT, UP, Signal

SETUP_TREND = "trend_continuation"
SETUP_SR = "support_resistance"
SETUP_REVERSAL = "reversal"
SETUPS = (SETUP_TREND, SETUP_SR, SETUP_REVERSAL)

SETUP_HELP = {
    SETUP_TREND: (
        "Trades WITH the trend. Heikin Ashi on the trend side of the Keltner mid, "
        "price the same side of the EMA 200, entering when a pullback resolves."
    ),
    SETUP_SR: (
        "Trades rejections at a level. Price reaches a rolling high/low and turns "
        "away from it. Direction is away from the level."
    ),
    SETUP_REVERSAL: (
        "Trades exhaustion against the current move: RSI extreme plus a Bollinger "
        "band tag. Opposes the move that produced it."
    ),
}


@dataclass
class SetupConfig:
    """Every setup parameter, all editable from Settings."""

    kind: str = SETUP_TREND

    # trend_continuation
    ema_trend: int = 200
    keltner_ema: int = 20
    keltner_atr: int = 10
    keltner_mult: float = 1.0
    require_ha_alignment: bool = True

    # support_resistance
    sr_lookback: int = 30
    sr_touch_atr: float = 0.25       # how close to the level counts as a touch
    sr_reject_pct: float = 0.5       # wick beyond the level vs the bar range

    # reversal
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    bb_period: int = 20
    bb_std: float = 2.0

    # shared filters
    adx_min: float = 0.0
    adx_max: float = 100.0

    def min_bars(self) -> int:
        if self.kind == SETUP_TREND:
            return self.ema_trend + 20
        if self.kind == SETUP_SR:
            return self.sr_lookback + 40
        return max(self.bb_period, self.rsi_period) + 40


@dataclass
class Check:
    name: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        return f"[{'PASS' if self.passed else 'fail'}] {self.name}: {self.detail}"


def _bias_trend(df: pd.DataFrame, cfg: SetupConfig, checks: list[Check]) -> int:
    close, high, low = df["close"], df["high"], df["low"]
    ha = ind.heikin_ashi(df)
    kc = ind.keltner_channel(high, low, close, cfg.keltner_ema, cfg.keltner_atr, cfg.keltner_mult)
    ema = ind.ema(close, cfg.ema_trend)

    ha_close = float(ha["close"].iloc[-1]); mid = float(kc["mid"].iloc[-1])
    price = float(close.iloc[-1]); ema_now = float(ema.iloc[-1])
    if mid != mid or ema_now != ema_now:
        checks.append(Check("indicators", False, "not warmed up"))
        return FLAT

    kc_side = UP if ha_close > mid else DOWN
    ema_side = UP if price > ema_now else DOWN
    checks.append(Check("HA vs Keltner mid", True,
                        f"HA {ha_close:.5f} vs mid {mid:.5f} -> {'BUY' if kc_side == UP else 'SELL'}"))
    checks.append(Check("price vs EMA", True,
                        f"{price:.5f} vs EMA{cfg.ema_trend} {ema_now:.5f} -> "
                        f"{'BUY' if ema_side == UP else 'SELL'}"))

    if cfg.require_ha_alignment and kc_side != ema_side:
        checks.append(Check("trend agreement", False, "Keltner and EMA disagree"))
        return FLAT
    checks.append(Check("trend agreement", True, "aligned"))
    return ema_side if not cfg.require_ha_alignment else kc_side


def _bias_sr(df: pd.DataFrame, cfg: SetupConfig, checks: list[Check]) -> int:
    high, low, close = df["high"], df["low"], df["close"]
    atr_now = float(ind.atr(high, low, close).iloc[-1])
    if atr_now != atr_now or atr_now <= 0:
        checks.append(Check("indicators", False, "ATR not ready"))
        return FLAT

    window_h = high.iloc[-(cfg.sr_lookback + 1):-1]
    window_l = low.iloc[-(cfg.sr_lookback + 1):-1]
    resistance = float(window_h.max()); support = float(window_l.min())

    o = float(df["open"].iloc[-1]); h = float(high.iloc[-1])
    l = float(low.iloc[-1]); c = float(close.iloc[-1])
    rng = max(h - l, 1e-12)
    tol = cfg.sr_touch_atr * atr_now

    at_res = h >= resistance - tol
    at_sup = l <= support + tol
    upper_wick = (h - max(o, c)) / rng
    lower_wick = (min(o, c) - l) / rng

    checks.append(Check("level in range", at_res or at_sup,
                        f"resistance {resistance:.5f} / support {support:.5f}, "
                        f"bar high {h:.5f} low {l:.5f}"))
    if at_res and upper_wick >= cfg.sr_reject_pct:
        checks.append(Check("rejection", True, f"upper wick {upper_wick:.0%} of range at resistance"))
        return DOWN
    if at_sup and lower_wick >= cfg.sr_reject_pct:
        checks.append(Check("rejection", True, f"lower wick {lower_wick:.0%} of range at support"))
        return UP
    checks.append(Check("rejection", False,
                        f"no rejection wick (upper {upper_wick:.0%}, lower {lower_wick:.0%}, "
                        f"need {cfg.sr_reject_pct:.0%})"))
    return FLAT


def _bias_reversal(df: pd.DataFrame, cfg: SetupConfig, checks: list[Check]) -> int:
    close = df["close"]
    rsi = ind.rsi(close, cfg.rsi_period)
    bb = ind.bollinger_bands(close, cfg.bb_period, cfg.bb_std)
    rsi_now = float(rsi.iloc[-1]); pct_b = float(bb["pct_b"].iloc[-1])
    if rsi_now != rsi_now or pct_b != pct_b:
        checks.append(Check("indicators", False, "not warmed up"))
        return FLAT

    checks.append(Check("RSI", True, f"{rsi_now:.1f} "
                                     f"(oversold <{cfg.rsi_oversold:.0f}, overbought >{cfg.rsi_overbought:.0f})"))
    checks.append(Check("Bollinger %B", True, f"{pct_b:.2f}"))

    if rsi_now <= cfg.rsi_oversold and pct_b <= 0.10:
        checks.append(Check("exhaustion", True, "oversold at the lower band -> BUY"))
        return UP
    if rsi_now >= cfg.rsi_overbought and pct_b >= 0.90:
        checks.append(Check("exhaustion", True, "overbought at the upper band -> SELL"))
        return DOWN
    checks.append(Check("exhaustion", False, "no RSI extreme with a band tag"))
    return FLAT


def evaluate(
    df: pd.DataFrame,
    setup: SetupConfig,
    trigger: trig.TriggerConfig,
) -> tuple[Signal, list[Check]]:
    """Run a setup and its trigger, returning the signal and every condition."""
    checks: list[Check] = []
    need = setup.min_bars()
    if len(df) < need:
        checks.append(Check("history", False, f"{len(df)} candles, need {need}"))
        return Signal(FLAT, 0.0, "insufficient history"), checks
    checks.append(Check("history", True, f"{len(df)} candles"))

    high, low, close = df["high"], df["low"], df["close"]

    if setup.adx_min > 0.0 or setup.adx_max < 100.0:
        adx_now = float(ind.adx(high, low, close).iloc[-1])
        ok = adx_now == adx_now and setup.adx_min <= adx_now <= setup.adx_max
        checks.append(Check("ADX band", ok,
                            f"ADX {adx_now:.1f} vs band {setup.adx_min:.0f}-{setup.adx_max:.0f}"))
        if not ok:
            return Signal(FLAT, 0.0, "outside the ADX band"), checks

    if setup.kind == SETUP_TREND:
        bias = _bias_trend(df, setup, checks)
    elif setup.kind == SETUP_SR:
        bias = _bias_sr(df, setup, checks)
    elif setup.kind == SETUP_REVERSAL:
        bias = _bias_reversal(df, setup, checks)
    else:
        checks.append(Check("setup", False, f"unknown setup {setup.kind!r}"))
        return Signal(FLAT, 0.0, "unknown setup"), checks

    if bias == FLAT:
        checks.append(Check("result", False, "no directional setup"))
        return Signal(FLAT, 0.0, "no setup"), checks

    result = trig.evaluate(df, bias, trigger)
    checks.append(Check(f"trigger ({trigger.kind})", result.fired, result.detail))
    if not result.fired:
        return Signal(FLAT, 0.0, f"setup present but {trigger.kind} trigger not met"), checks

    score = _score(df, bias, setup)
    checks.append(Check("result", True, f"{'BUY' if bias == UP else 'SELL'} at {score:.2f}"))
    return (
        Signal(bias, score, f"{setup.kind} + {trigger.kind}: {result.detail}",
               [setup.kind], meta={"level": result.level, "event": True}),
        checks,
    )


def _score(df: pd.DataFrame, bias: int, cfg: SetupConfig) -> float:
    """Confidence in 0..1. **Not a validated filter -- do not gate on it.**

    ADX used to be an input here, added for trend setups on the reasoning that
    a stronger trend is a better continuation. Measured over 815 live trades
    it is the opposite: ADX rank-correlates -0.08 with winning, and splitting
    at 25 gives 55.4% below versus 44.1% above (Fisher p=0.002). Adding it
    made the score anti-calibrated, which is exactly what three successive
    journals showed -- the lowest-confidence bucket kept winning most.

    ADX is a *regime filter*, not a confidence input, and it already has one:
    ``adx_min``/``adx_max`` in Settings. So it is gone from here rather than
    inverted, because inverting it would be fitting a sign to two days of
    data.

    That leaves candle body, which measured +/-0.00 against outcome -- no
    demonstrated information either. The score is kept because it is recorded
    per signal and the Analytics buckets are how it will eventually be
    validated or replaced, but nothing should be filtered on it until those
    buckets show a monotonic gradient.
    """
    high, low, close = df["high"], df["low"], df["close"]
    atr_now = float(ind.atr(high, low, close).iloc[-1])
    if atr_now != atr_now or atr_now <= 0:
        return 0.6

    o = float(df["open"].iloc[-1]); c = float(close.iloc[-1])
    body = abs(c - o) / atr_now
    weight = 0.25 if cfg.kind == SETUP_SR else 0.20
    return float(min(max(0.55 + weight * min(body, 1.5) / 1.5, 0.0), 1.0))
