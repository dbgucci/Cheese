"""Intraday momentum: the rule set with the strongest published evidence.

Zarattini, Aziz & Barbon (2024) tested this on SPY from 2007 to early 2024 and
reported 19.6% annualised at a Sharpe of 1.33 **net of costs**. It is the only
short-horizon strategy found in the research for this project that survives
publication, out-of-sample testing and realistic transaction costs at once.

The idea in one line: price spends most of the day inside a band around the
open, and leaving that band is information rather than noise.

The band is not a fixed percentage. Its width for a given minute is the
average distance price had travelled from the open *by that same minute* over
the last fortnight, so the threshold at 10:00 is naturally tighter than the
one at 15:30 -- a 0.3% move an hour into the session is unusual, the same
move six hours in is ordinary. A fixed band would be too loose in the morning
and too tight in the afternoon, and would take almost all of its trades late.

What this is not: it is not scalping. Positions are held for hours and closed
at the session end. That is deliberate. The evidence for holding minutes is
uniformly negative once costs are charged -- Mesfin (2026) tested fourteen
families of intraday signals on Nasdaq futures and none cleared a two-point
round trip -- and ``costs.py`` measures whether this broker's spreads leave
room for the horizon actually being traded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import Optional, Protocol

import numpy as np
import pandas as pd

from .execution import BUY, SELL

FLAT = 0


@dataclass
class Intent:
    """What the strategy wants, before risk or execution has a say."""

    direction: int = FLAT
    stop_loss: Optional[float] = None
    reason: str = ""
    boundary: Optional[float] = None
    take_profit: Optional[float] = None

    def __bool__(self) -> bool:
        return self.direction != FLAT


@dataclass
class MomentumConfig:
    lookback_days: int = 14
    band_multiple: float = 1.0        # widen to trade less, narrow to trade more
    session_open: time = time(13, 30)  # US cash open in UTC
    session_close: time = time(20, 0)
    evaluate_every_minutes: int = 30   # the paper checks on the half hour
    min_band_points: float = 0.0       # never trade a band narrower than the spread
    max_trades_per_session: int = 2


def session_slice(df: pd.DataFrame, day: pd.Timestamp,
                  open_t: time, close_t: time) -> pd.DataFrame:
    """One day's bars, from the session open to the session close."""
    lo = pd.Timestamp.combine(day.date(), open_t).tz_localize(df.index.tz)
    hi = pd.Timestamp.combine(day.date(), close_t).tz_localize(df.index.tz)
    return df[(df.index >= lo) & (df.index <= hi)]


def move_profile(
    df: pd.DataFrame, cfg: MomentumConfig, upto: pd.Timestamp
) -> dict[int, float]:
    """Average |move from the open| by minute-of-session, over recent days.

    This is the band's shape. Built only from days strictly before ``upto``,
    which is the whole reason it can be used live: a profile that included
    today would be reading the answer off the same bar it is meant to
    predict.
    """
    days = sorted({ts.date() for ts in df.index if ts < upto})[-cfg.lookback_days:]
    buckets: dict[int, list[float]] = {}
    for d in days:
        day_bars = session_slice(df, pd.Timestamp(d, tz=df.index.tz),
                                 cfg.session_open, cfg.session_close)
        if day_bars.empty:
            continue
        open_px = float(day_bars["open"].iloc[0])
        if open_px <= 0:
            continue
        start = day_bars.index[0]
        for ts, close in day_bars["close"].items():
            minute = int((ts - start).total_seconds() // 60)
            buckets.setdefault(minute, []).append(abs(float(close) - open_px) / open_px)
    return {m: float(np.mean(v)) for m, v in buckets.items() if v}


def _band(profile: dict[int, float], minute: int) -> float:
    """The profile at this minute, falling back to the nearest measured one."""
    if minute in profile:
        return profile[minute]
    if not profile:
        return 0.0
    nearest = min(profile, key=lambda m: abs(m - minute))
    return profile[nearest]


def vwap(day_bars: pd.DataFrame) -> float:
    """Session VWAP, used as the trailing stop rather than as a signal."""
    vol = day_bars.get("tick_volume")
    typical = (day_bars["high"] + day_bars["low"] + day_bars["close"]) / 3.0
    if vol is None or float(vol.sum()) <= 0:
        return float(typical.mean())
    return float((typical * vol).sum() / vol.sum())


class Strategy(Protocol):
    def evaluate(self, df: pd.DataFrame, now: pd.Timestamp,
                 trades_today: int) -> Intent: ...


class IntradayMomentum:
    """Boundaries from the open; a breach in either direction is the trade."""

    name = "intraday_momentum"

    def __init__(self, config: Optional[MomentumConfig] = None, point: float = 1.0):
        self.cfg = config or MomentumConfig()
        self.point = point
        self._profile: dict[int, float] = {}
        self._profile_day = None

    # ------------------------------------------------------------------
    def bounds(self, df: pd.DataFrame, now: pd.Timestamp) -> Optional[tuple[float, float, float]]:
        """(lower, upper, band_width_in_points) for this moment, or None."""
        day = pd.Timestamp(now.date(), tz=df.index.tz)
        if self._profile_day != day.date():
            self._profile = move_profile(df, self.cfg, day)
            self._profile_day = day.date()
        if not self._profile:
            return None

        today = session_slice(df, day, self.cfg.session_open, self.cfg.session_close)
        today = today[today.index <= now]
        if today.empty:
            return None

        open_px = float(today["open"].iloc[0])
        minute = int((now - today.index[0]).total_seconds() // 60)
        sigma = _band(self._profile, minute) * self.cfg.band_multiple

        # Gap adjustment: an overnight gap means the open is not the day's
        # reference point, so the band is anchored to whichever of the open
        # and the previous close is further out on each side.
        before = df[df.index < today.index[0]]
        prev_close = float(before["close"].iloc[-1]) if len(before) else open_px
        upper = max(open_px, prev_close) * (1 + sigma)
        lower = min(open_px, prev_close) * (1 - sigma)
        width = (upper - lower) / self.point
        return lower, upper, width

    # ------------------------------------------------------------------
    def evaluate(self, df: pd.DataFrame, now: pd.Timestamp, trades_today: int = 0) -> Intent:
        cfg = self.cfg
        if trades_today >= cfg.max_trades_per_session:
            return Intent(reason=f"{trades_today} trades already this session")

        b = self.bounds(df, now)
        if b is None:
            return Intent(reason="not enough history for the band")
        lower, upper, width = b

        if width < cfg.min_band_points:
            return Intent(reason=f"band is {width:.1f}pt wide, narrower than the "
                                 f"{cfg.min_band_points:.1f}pt minimum")

        day = pd.Timestamp(now.date(), tz=df.index.tz)
        today = session_slice(df, day, cfg.session_open, cfg.session_close)
        today = today[today.index <= now]
        minute = int((now - today.index[0]).total_seconds() // 60)
        if cfg.evaluate_every_minutes > 1 and minute % cfg.evaluate_every_minutes != 0:
            return Intent(reason=f"not an evaluation minute ({minute})")

        close = float(today["close"].iloc[-1])
        mid = vwap(today)

        if close > upper:
            # The stop is the far side of the band or VWAP, whichever is
            # closer to price -- so a trade that immediately fails is cut
            # rather than held down to the opposite boundary.
            stop = max(lower, mid)
            if stop >= close:
                return Intent(reason="VWAP is above price on a long breakout")
            return Intent(BUY, stop, f"closed {close:.5f} above the "
                                     f"{upper:.5f} upper boundary", upper)
        if close < lower:
            stop = min(upper, mid)
            if stop <= close:
                return Intent(reason="VWAP is below price on a short breakout")
            return Intent(SELL, stop, f"closed {close:.5f} below the "
                                      f"{lower:.5f} lower boundary", lower)
        return Intent(reason=f"inside the band ({lower:.5f} - {upper:.5f})")

    # ------------------------------------------------------------------
    def trailing_stop(self, df: pd.DataFrame, now: pd.Timestamp,
                      direction: int) -> Optional[float]:
        """Where the stop should be now, given how the session has developed.

        The paper trails on the tighter of VWAP and the band, which ratchets
        a winning position's stop up behind it without reacting to every bar.
        """
        b = self.bounds(df, now)
        if b is None:
            return None
        lower, upper, _ = b
        day = pd.Timestamp(now.date(), tz=df.index.tz)
        today = session_slice(df, day, self.cfg.session_open, self.cfg.session_close)
        today = today[today.index <= now]
        if today.empty:
            return None
        mid = vwap(today)
        return max(lower, mid) if direction == BUY else min(upper, mid)


@dataclass
class ORBConfig:
    range_minutes: int = 5
    session_open: time = time(13, 30)
    session_close: time = time(20, 0)
    stop_at_range_opposite: bool = True
    max_trades_per_session: int = 1


class OpeningRangeBreakout:
    """Trade the first break of the opening range.

    Second-best evidenced of the two. Zarattini & Aziz found a 5-minute range
    best on a stock universe, and a peer-reviewed study on index futures
    (2003-2013) found roughly 8% a year at p<0.03 -- real but modest, and a
    2026 falsification study on Nasdaq micro futures could not reproduce it
    net of costs. Included so the two can be compared on this broker's own
    data rather than argued about.
    """

    name = "opening_range_breakout"

    def __init__(self, config: Optional[ORBConfig] = None, point: float = 1.0):
        self.cfg = config or ORBConfig()
        self.point = point

    def opening_range(self, df: pd.DataFrame, now: pd.Timestamp) -> Optional[tuple[float, float]]:
        day = pd.Timestamp(now.date(), tz=df.index.tz)
        today = session_slice(df, day, self.cfg.session_open, self.cfg.session_close)
        if today.empty:
            return None
        window = today.iloc[:self.cfg.range_minutes]
        if len(window) < self.cfg.range_minutes:
            return None
        return float(window["low"].min()), float(window["high"].max())

    def evaluate(self, df: pd.DataFrame, now: pd.Timestamp, trades_today: int = 0) -> Intent:
        if trades_today >= self.cfg.max_trades_per_session:
            return Intent(reason="the opening range is traded once per session")
        rng = self.opening_range(df, now)
        if rng is None:
            return Intent(reason="the opening range is not complete yet")
        low, high = rng

        day = pd.Timestamp(now.date(), tz=df.index.tz)
        today = session_slice(df, day, self.cfg.session_open, self.cfg.session_close)
        today = today[today.index <= now]
        if len(today) <= self.cfg.range_minutes:
            return Intent(reason="still inside the opening range window")
        close = float(today["close"].iloc[-1])

        if close > high:
            return Intent(BUY, low if self.cfg.stop_at_range_opposite else high,
                          f"broke the {low:.5f}-{high:.5f} opening range upward", high)
        if close < low:
            return Intent(SELL, high if self.cfg.stop_at_range_opposite else low,
                          f"broke the {low:.5f}-{high:.5f} opening range downward", low)
        return Intent(reason=f"inside the opening range ({low:.5f}-{high:.5f})")

    def trailing_stop(self, df, now, direction):
        rng = self.opening_range(df, now)
        if rng is None:
            return None
        low, high = rng
        return low if direction == BUY else high


STRATEGIES = {
    IntradayMomentum.name: IntradayMomentum,
    OpeningRangeBreakout.name: OpeningRangeBreakout,
}


# ---------------------------------------------------------------------------
# Pivot breakout-and-retest
# ---------------------------------------------------------------------------
@dataclass
class PivotConfig:
    """Defaults come from the sweep, not from the product being copied.

    On 60 days of Nasdaq 5-minute bars a 1.0 ATR stop appeared in nine of the
    fourteen best parameter sets and the ADX filter appeared in none of the top
    two, so momentum filtering is off by default here even though the bot this
    reconstructs advertises it. That evidence is one quarter of one instrument
    and roughly the ninety-first percentile of a no-edge null, which is a
    reason to prefer these numbers over the alternatives and not a reason to
    trust them.
    """

    stop_atr: float = 1.0
    atr_period: int = 14
    retest_bars: int = 12
    target_skip: int = 0          # 0 aims at the next level, 1 at the one beyond
    min_rr: float = 1.5           # refuse a target closer than this many R
    adx_min: float = 0.0          # 0 disables the momentum filter
    session_open: time = time(13, 30)
    session_close: time = time(20, 0)
    evaluate_every_minutes: int = 5
    max_trades_per_session: int = 3
    min_bars: int = 120


class PivotRetest:
    """Break a daily pivot level, wait for the retest, trade the hold.

    The reconstruction of "QT Sniper Auto Bot" from
    :mod:`cheese_signals.research.pivot_retest`, wired to the live executor.
    The rules are identical so that what runs is what was measured; the only
    difference is that a backtest knows the bar closed and this does not, so
    every decision is taken on ``df`` truncated at ``now``.

    Deliberately stateless. The runner evaluates on a timer rather than once
    per bar, so remembering which levels were broken between calls would make
    behaviour depend on when the loop happened to fire. Breaks are re-derived
    from the last ``retest_bars`` each time instead.
    """

    name = "pivot_retest"

    def __init__(self, config: Optional[PivotConfig] = None, point: float = 1.0):
        self.cfg = config or PivotConfig()
        self.point = point

    def _ladder(self, df: pd.DataFrame, now: pd.Timestamp):
        from .. import pivots
        levels = pivots.daily_levels(pivots.to_daily(df))
        if levels.empty:
            return None
        key = pd.Timestamp(now).normalize()
        if key not in levels.index:
            return None
        return pivots.ladder(levels.loc[key])

    def evaluate(self, df: pd.DataFrame, now: pd.Timestamp,
                 trades_today: int) -> Intent:
        from .. import indicators as ind

        if trades_today >= self.cfg.max_trades_per_session:
            return Intent(reason=f"{trades_today} trades already today")

        df = df[df.index <= now]
        if len(df) < self.cfg.min_bars:
            return Intent(reason=f"only {len(df)} bars of history")

        lad = self._ladder(df, now)
        if lad is None or lad.size == 0:
            return Intent(reason="no pivot levels for today yet")

        atr = float(ind.atr(df["high"], df["low"], df["close"],
                            self.cfg.atr_period).iloc[-1])
        if not (atr > 0) or pd.isna(atr):
            return Intent(reason="no ATR yet")

        if self.cfg.adx_min > 0:
            adx = float(ind.adx(df["high"], df["low"], df["close"]).iloc[-1])
            if pd.isna(adx) or adx < self.cfg.adx_min:
                return Intent(reason=f"ADX {adx:.1f} below {self.cfg.adx_min}")

        c = df["close"].to_numpy(dtype=float)
        h = df["high"].to_numpy(dtype=float)
        l = df["low"].to_numpy(dtype=float)
        i = len(df) - 1

        broken: dict[float, int] = {}
        for k in range(max(1, i - self.cfg.retest_bars), i + 1):
            for lvl in lad:
                if c[k - 1] <= lvl < c[k]:
                    broken[float(lvl)] = BUY
                elif c[k - 1] >= lvl > c[k]:
                    broken[float(lvl)] = SELL

        for lvl, direction in broken.items():
            if direction == BUY:
                if not (l[i] <= lvl and c[i] > lvl):
                    continue
                above = lad[lad > c[i]]
                if len(above) <= self.cfg.target_skip:
                    continue
                target = float(above[self.cfg.target_skip])
                stop = lvl - self.cfg.stop_atr * atr
            else:
                if not (h[i] >= lvl and c[i] < lvl):
                    continue
                below = lad[lad < c[i]]
                if len(below) <= self.cfg.target_skip:
                    continue
                target = float(below[-1 - self.cfg.target_skip])
                stop = lvl + self.cfg.stop_atr * atr

            risk = abs(c[i] - stop)
            if risk <= 0:
                continue
            rr = abs(target - c[i]) / risk
            if rr < self.cfg.min_rr:
                continue
            side = "long" if direction == BUY else "short"
            return Intent(
                direction=direction,
                stop_loss=stop,
                take_profit=target,
                boundary=lvl,
                reason=(f"{side} retest of {lvl:.2f}, stop {stop:.2f}, "
                        f"target {target:.2f} ({rr:.1f}R)"),
            )
        return Intent(reason="no level retested")
