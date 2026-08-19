"""Opening range breakout: the rules, as pure functions over bars.

The idea, and the reason it is not obviously nonsense
----------------------------------------------------
An exchange opening is one of the few moments in a trading day with a real
mechanism behind it. Orders accumulated overnight -- from time zones that were
awake while this market was not, from funds that mark against the open, from
retail queued before the bell -- are all released into a few minutes of
concentrated flow. The high and low of that flurry are the day's first agreed
boundaries, and price leaving them means the overnight balance did not hold.

That is a genuine mechanism, which is what separates this from the Pocket
Option work in the rest of this repository. There, the setups had to be
justified as *shapes* on a synthetic feed because no order flow existed to
appeal to. Here the appeal is legitimate -- but it is still only an appeal,
and the numbers in ``orb_backtest`` are what decide the matter.

What actually kills opening-range bots
--------------------------------------
Not the entry rule. Four other things, each of which is a filter or a limit
in ``OrbConfig`` rather than a comment:

1. **Range days.** Price crosses the range boundary, reverses, crosses the
   other one, reverses again. An unlimited bot takes six losing trades in the
   session. ``max_trades_per_session`` defaults to 1 for this reason: the
   first breakout is the one the mechanism argues for, and the fourth is just
   a bot paying spread to watch a range hold.
2. **A range too narrow to pay for itself.** On a quiet morning the first
   fifteen minutes span barely more than the spread. The breakout is then
   pure noise, and every trade is a coin flip charged a commission. This is
   where the cost wall from ``costs.py`` re-enters: the range has to be a
   multiple of the round-trip cost before the day is tradeable at all.
3. **A range so wide the move is already over.** After a gap or an overnight
   release the first fifteen minutes can cover most of a normal day's travel.
   Breaking out of that range means entering at the end of the move with a
   stop the width of the day.
4. **Holding into the close.** A CFD charges swap overnight and gaps over it.
   Every position is flat before the session ends, without exception.

Bars are stamped by **open** time, matching ``mt5_bridge.normalise``, and
every timestamp here is real UTC -- ``clock.BarClock`` has already corrected
the broker's server offset before any of this runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Mapping, Optional

import pandas as pd

from .clock import SessionSpec
from .execution import BUY, SELL

# Entry modes.
#
#   "close"  the breakout is a bar *closing* beyond the level. Costs the tail
#            of that bar, and refuses every wick that pokes through and comes
#            back -- which on an opening range is most of them.
#   "touch"  a resting stop order at the level. Gets in earlier and takes
#            every one of those wicks. Backtests better than it trades,
#            because a stop order at the open fills with slippage that
#            historical OHLC cannot show.
ENTRY_CLOSE = "close"
ENTRY_TOUCH = "touch"

# Stop placements.
#
#   "range_opposite"  the far side of the opening range. The textbook rule,
#                     and the least fitted: it is where the premise "the
#                     overnight balance broke" is definitively wrong.
#   "range_fraction"  a fraction of the range width back inside it. Tighter,
#                     so a bigger position for the same risk, at the cost of
#                     being stopped out by moves the premise survives.
#   "atr"             a volatility multiple, independent of the range width.
STOP_RANGE_OPPOSITE = "range_opposite"
STOP_RANGE_FRACTION = "range_fraction"
STOP_ATR = "atr"


@dataclass(frozen=True)
class OrbConfig:
    """Every rule, in one place, with defaults that are deliberately dull.

    The defaults are the textbook version of the strategy: a 15-minute range,
    a close-through entry, the stop at the opposite side, two units of reward
    per unit of risk, one trade a day. Nothing here has been tuned to a
    backtest, which is the only honest starting point -- tuning is what
    ``orb_backtest`` is for, and what a walk-forward split is for after that.
    """

    # --- the range
    range_minutes: int = 15
    min_range_bar_fraction: float = 0.8   # of the bars the range should contain

    # --- entry
    entry_mode: str = ENTRY_CLOSE
    entry_buffer_points: float = 0.0      # extra travel past the level required
    entry_window_minutes: int = 120       # no new entries later than this after the range
    max_trades_per_session: int = 1        # a hard cap on entries, re-entries included
    one_direction_per_session: bool = True  # once it breaks up, never trade it short today

    # --- exits
    stop_mode: str = STOP_RANGE_OPPOSITE
    stop_fraction: float = 0.5            # for "range_fraction"; 0.5 is the range mid
    stop_atr_multiple: float = 1.0        # for "atr"
    target_r: float = 2.0                 # take profit, in multiples of the stop distance
    breakeven_at_r: Optional[float] = 1.0  # move the stop to entry once this far ahead
    flat_before_close_minutes: int = 10

    # --- the day filters
    min_range_cost_multiple: float = 3.0   # range width vs the round-trip cost
    min_target_cost_multiple: float = 3.0  # the reward aimed at, vs the same cost
    min_range_adr_fraction: float = 0.05   # range width vs the average daily range
    max_range_adr_fraction: float = 0.60
    adr_days: int = 14

    def validate(self) -> list[str]:
        """Settings that contradict each other, named rather than obeyed.

        Copied in spirit from ``settings.py`` on the Pocket Option side, where
        a silently impossible configuration cost real time to find: a bot that
        never trades looks identical to a bot whose filters are inverted.
        """
        out = []
        if self.entry_mode not in (ENTRY_CLOSE, ENTRY_TOUCH):
            out.append(f"entry_mode '{self.entry_mode}' is not a mode")
        if self.stop_mode not in (STOP_RANGE_OPPOSITE, STOP_RANGE_FRACTION, STOP_ATR):
            out.append(f"stop_mode '{self.stop_mode}' is not a mode")
        if self.range_minutes < 1:
            out.append("range_minutes below 1 leaves no range to break out of")
        if self.min_range_adr_fraction >= self.max_range_adr_fraction:
            out.append(
                f"the range filter is inverted: minimum "
                f"{self.min_range_adr_fraction:.0%} of the average daily range is not "
                f"below the maximum {self.max_range_adr_fraction:.0%}, so no day can "
                f"ever qualify"
            )
        if self.target_r <= 0:
            out.append("target_r at or below 0 means the take-profit is at or behind entry")
        if self.breakeven_at_r is not None and self.breakeven_at_r >= self.target_r:
            out.append(
                f"breakeven_at_r {self.breakeven_at_r} is not below target_r "
                f"{self.target_r}, so the stop would only move at the moment the "
                f"trade closes anyway"
            )
        if self.stop_mode == STOP_RANGE_FRACTION and not 0 < self.stop_fraction <= 1:
            out.append("stop_fraction must be inside (0, 1] to sit within the range")
        if self.max_trades_per_session < 1:
            out.append("max_trades_per_session below 1 disables the strategy entirely")
        if self.entry_window_minutes <= 0:
            out.append("entry_window_minutes at or below 0 closes the window before "
                       "the range does, so nothing can ever trigger")
        return out

    def barred_after(self, direction: int) -> set[int]:
        """Which directions are off the table once ``direction`` has traded.

        The whipsaw protection, stated once. With
        ``one_direction_per_session`` set -- the default -- a session that
        broke upward is never traded short, however convincing the downside
        break looks later. That rule costs the occasional good reversal and
        removes the range day that otherwise takes four losses in a row.
        """
        return {-direction} if self.one_direction_per_session else set()


@dataclass(frozen=True)
class OpeningRange:
    """The first ``range_minutes`` of one session, on one instrument."""

    symbol: str
    session_date: date
    start: datetime          # inclusive, real UTC
    end: datetime            # exclusive: the first bar that may trigger
    high: float
    low: float
    bars: int
    expected_bars: int
    point: float
    cost_points: float       # median spread over the range bars, plus commission

    @property
    def width(self) -> float:
        return self.high - self.low

    @property
    def width_points(self) -> float:
        return self.width / self.point if self.point else 0.0

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2.0

    def describe(self) -> str:
        return (f"{self.symbol} {self.session_date} range "
                f"{self.low:.5f}-{self.high:.5f} ({self.width_points:.0f}pt) "
                f"from {self.bars}/{self.expected_bars} bars")


@dataclass(frozen=True)
class Check:
    """A yes/no with the reason attached whichever way it went."""

    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class TradePlan:
    """A decided trade, before any broker has seen it."""

    symbol: str
    direction: int
    entry: float
    stop: float
    target: float
    at: datetime
    reason: str
    range_: OpeningRange

    @property
    def side(self) -> str:
        return "BUY" if self.direction == BUY else "SELL"

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def risk_points(self) -> float:
        p = self.range_.point
        return self.risk / p if p else 0.0

    @property
    def reward_points(self) -> float:
        p = self.range_.point
        return abs(self.target - self.entry) / p if p else 0.0

    def describe(self) -> str:
        return (f"{self.symbol} {self.side} @ {self.entry:.5f} "
                f"stop {self.stop:.5f} ({self.risk_points:.0f}pt) "
                f"target {self.target:.5f} -- {self.reason}")


# --------------------------------------------------------------------------
# slicing a session out of a frame of bars
# --------------------------------------------------------------------------
def as_utc_index(bars: pd.DataFrame) -> pd.DataFrame:
    index = pd.DatetimeIndex(bars.index)
    if index.tz is None:
        return bars.set_axis(index.tz_localize("UTC"), axis=0)
    if str(index.tz) != "UTC":
        return bars.set_axis(index.tz_convert("UTC"), axis=0)
    return bars


def session_slice(
    bars: pd.DataFrame, spec: SessionSpec, day: date
) -> pd.DataFrame:
    """The bars belonging to one session, open inclusive, close exclusive."""
    bars = as_utc_index(bars)
    start, end = spec.open_utc(day), spec.close_utc(day)
    return bars[(bars.index >= start) & (bars.index < end)]


def _session_stamps(bars: pd.DataFrame, spec: SessionSpec) -> tuple[pd.DataFrame, "pd.Series"]:
    """Bars restricted to session hours, tagged with the session day they belong to.

    Only the hours the strategy could actually trade. An index CFD quotes
    almost around the clock, so its 24-hour high-to-low includes a whole
    overnight session that no opening-range trade will ever be in, and using
    that as the yardstick overstates the move available by a wide margin.
    """
    bars = as_utc_index(bars)
    if bars.empty:
        return bars, pd.Series(dtype="object")

    local = bars.index.tz_convert(spec.tz)
    minute_of_day = local.hour * 60 + local.minute
    open_min = spec.open_time.hour * 60 + spec.open_time.minute
    close_min = spec.close_time.hour * 60 + spec.close_time.minute

    if close_min > open_min:
        inside = (minute_of_day >= open_min) & (minute_of_day < close_min)
        days = local.normalize()
    else:
        # A session that wraps midnight belongs to the day it opened on.
        inside = (minute_of_day >= open_min) | (minute_of_day < close_min)
        days = local.normalize().where(minute_of_day >= open_min,
                                       local.normalize() - pd.Timedelta(days=1))

    tags = pd.Series([d.date() for d in days], index=bars.index)
    weekday = tags.map(spec.is_open_weekday)
    keep = pd.Series(inside, index=bars.index) & weekday
    return bars[keep.to_numpy()], tags[keep.to_numpy()]


def session_ranges(bars: pd.DataFrame, spec: SessionSpec) -> pd.DataFrame:
    """High, low and range of every session in a frame, keyed by session day.

    Computed once for a whole history so the daily-range filter costs one pass
    rather than one pass per day being decided.
    """
    inside, tags = _session_stamps(bars, spec)
    if inside.empty:
        return pd.DataFrame(columns=["high", "low", "range"], dtype=float)
    grouped = inside.assign(_day=tags.to_numpy()).groupby("_day")
    out = grouped.agg(high=("high", "max"), low=("low", "min")).astype(float)
    out["range"] = out["high"] - out["low"]
    out.index.name = "session_date"
    return out.sort_index()


def session_dates(bars: pd.DataFrame, spec: SessionSpec) -> list[date]:
    """Every trading day present in a frame, in the exchange's own calendar."""
    return list(session_ranges(bars, spec).index)


def adr_before(daily: pd.DataFrame, days: int, point: float) -> pd.Series:
    """Average daily range *available before* each session, in points.

    The shift is the whole point of the function. Averaging the last ``days``
    sessions inclusive would let the filter see the range of the very day it
    is deciding whether to trade, which is lookahead of the cheapest and most
    flattering kind -- and it is invisible in the results, because a
    lookahead-contaminated filter simply looks like a good filter.
    """
    if daily.empty or days <= 0 or not point:
        return pd.Series(dtype=float)
    return (daily["range"] / point).rolling(days, min_periods=1).mean().shift(1)


def average_daily_range_points(
    bars: pd.DataFrame,
    spec: SessionSpec,
    point: float,
    days: int,
    before: date,
) -> float:
    """Mean session range over the ``days`` sessions strictly before ``before``.

    The single-day form, used by the live runner. ``before`` is exclusive for
    the same reason ``adr_before`` shifts.
    """
    daily = session_ranges(bars, spec)
    if daily.empty or days <= 0 or not point:
        return 0.0
    earlier = daily[daily.index < before]["range"].tail(days)
    return float(earlier.mean() / point) if len(earlier) else 0.0


def _cost_points(window: pd.DataFrame, commission_points: float,
                 fallback_spread_points: float) -> float:
    """What a round trip costs, from the spreads actually quoted in the range.

    Measured on the range's own bars rather than on a long-run average,
    because the number that matters is the one being charged this morning. A
    widened spread is itself a reason not to trade the day, and averaging it
    away hides exactly that.

    **A column of zeros is missing data, not free trading.** Some feeds simply
    do not populate the per-bar spread, and reading that as zero does more than
    flatter the arithmetic: ``min_range_cost_multiple`` is a ratio of range to
    cost, so a cost of zero passes every range, however narrow. A live record
    showed exactly this -- four FX alerts with three-to-five pip stops, ranges
    that could not have cleared a real spread, all waved through by a filter
    whose divisor had quietly become nothing. So zero falls back too.
    """
    if "spread" in window and not window["spread"].isna().all():
        spread = float(window["spread"].astype(float).median())
        if spread <= 0.0:
            spread = float(fallback_spread_points)
    else:
        spread = float(fallback_spread_points)
    return spread + commission_points


def build_range(
    bars: pd.DataFrame,
    symbol: str,
    spec: SessionSpec,
    day: date,
    cfg: OrbConfig,
    point: float,
    commission_points: float = 0.0,
    fallback_spread_points: float = 0.0,
    cost_points: Optional[float] = None,
) -> Optional[OpeningRange]:
    """The opening range for one session, or ``None`` if there were no bars.

    A missing session is normal -- exchange holidays are not in any calendar
    this module carries, and a holiday looks exactly like a day with no bars.
    Treating it as absence rather than as an error is how holidays are handled
    without maintaining a holiday table that would go stale.

    ``cost_points`` overrides the measured cost outright. Its only legitimate
    use is the zero-cost counterfactual in ``orb_backtest``: without it, that
    run would price its fills for free while its *filters* still saw the real
    spread, which is neither the strategy nor the counterfactual.
    """
    start = spec.open_utc(day)
    end = start + timedelta(minutes=cfg.range_minutes)
    bars = as_utc_index(bars)
    window = bars[(bars.index >= start) & (bars.index < end)]
    if window.empty:
        return None
    cost = (cost_points if cost_points is not None
            else _cost_points(window, commission_points, fallback_spread_points))
    return OpeningRange(
        symbol=symbol,
        session_date=day,
        start=start,
        end=end,
        high=float(window["high"].max()),
        low=float(window["low"].min()),
        bars=int(len(window)),
        expected_bars=int(cfg.range_minutes),
        point=point,
        cost_points=float(cost),
    )


# --------------------------------------------------------------------------
# is this day tradeable at all
# --------------------------------------------------------------------------
def check_range(rng: OpeningRange, cfg: OrbConfig, adr_points: float) -> Check:
    """Whether the day's range qualifies, and if not, exactly why not.

    Every refusal carries its numbers. "No trades today" without them is the
    single most common way a bot is found to have been broken for a fortnight.
    """
    if rng.bars < cfg.min_range_bar_fraction * rng.expected_bars:
        return Check(False, (
            f"only {rng.bars} of {rng.expected_bars} range bars present -- a gap "
            f"in the feed makes the high and low unreliable"))

    if rng.width <= 0:
        return Check(False, "the range has no width: every bar printed one price")

    wall = cfg.min_range_cost_multiple * rng.cost_points
    if rng.width_points < wall:
        return Check(False, (
            f"range {rng.width_points:.0f}pt is under {cfg.min_range_cost_multiple:.1f}x "
            f"the {rng.cost_points:.1f}pt round-trip cost -- the spread would take "
            f"more than the breakout is likely to give"))

    if adr_points > 0:
        fraction = rng.width_points / adr_points
        if fraction < cfg.min_range_adr_fraction:
            return Check(False, (
                f"range is {fraction:.1%} of the {adr_points:.0f}pt average daily "
                f"range, below the {cfg.min_range_adr_fraction:.0%} floor -- too "
                f"quiet an open for the break to mean anything"))
        if fraction > cfg.max_range_adr_fraction:
            return Check(False, (
                f"range is {fraction:.1%} of the {adr_points:.0f}pt average daily "
                f"range, above the {cfg.max_range_adr_fraction:.0%} ceiling -- most "
                f"of a normal day's travel happened before the range even closed"))

    if rng.cost_points <= 0:
        return Check(True, f"range {rng.width_points:.0f}pt at no modelled cost")
    return Check(True, (f"range {rng.width_points:.0f}pt, "
                        f"{rng.width_points / rng.cost_points:.1f}x cost"))


def check_plan(plan: TradePlan, cfg: OrbConfig) -> Check:
    """Whether the trade the rules produced is worth its own cost."""
    if plan.risk_points <= 0:
        return Check(False, "the stop is at the entry: nothing to risk and nothing to size")
    wall = cfg.min_target_cost_multiple * plan.range_.cost_points
    if plan.reward_points < wall:
        return Check(False, (
            f"target is {plan.reward_points:.0f}pt against a "
            f"{plan.range_.cost_points:.1f}pt round trip, under the "
            f"{cfg.min_target_cost_multiple:.1f}x floor"))
    return Check(True)


# --------------------------------------------------------------------------
# the entry
# --------------------------------------------------------------------------
def _levels(rng: OpeningRange, cfg: OrbConfig) -> tuple[float, float]:
    buffer = cfg.entry_buffer_points * rng.point
    return rng.high + buffer, rng.low - buffer


def stop_and_target(
    rng: OpeningRange, cfg: OrbConfig, direction: int, entry: float,
    atr_points: float = 0.0,
) -> tuple[float, float]:
    """Where the trade is wrong, and where it is finished."""
    if cfg.stop_mode == STOP_RANGE_OPPOSITE:
        stop = rng.low if direction == BUY else rng.high
    elif cfg.stop_mode == STOP_RANGE_FRACTION:
        back = rng.width * cfg.stop_fraction
        stop = rng.high - back if direction == BUY else rng.low + back
    else:
        distance = cfg.stop_atr_multiple * atr_points * rng.point
        stop = entry - distance if direction == BUY else entry + distance

    risk = abs(entry - stop)
    target = entry + cfg.target_r * risk * (1 if direction == BUY else -1)
    return stop, target


def entry_deadline(rng: OpeningRange, cfg: OrbConfig, spec: SessionSpec) -> datetime:
    """The last moment a new position may be opened today.

    Whichever comes first: the entry window expiring, or the run-up to the
    close. A breakout twenty minutes before the bell has no time to travel
    two units of risk, and it will be flattened at the close either way --
    so taking it is paying the spread for a coin flip.
    """
    window_end = rng.end + timedelta(minutes=cfg.entry_window_minutes)
    flat = spec.close_utc(rng.session_date) - timedelta(
        minutes=cfg.flat_before_close_minutes)
    return min(window_end, flat)


def breakout(
    rng: OpeningRange,
    bar_ts: datetime,
    bar: Mapping[str, float],
    cfg: OrbConfig,
    taken: Iterable[int] = (),
    atr_points: float = 0.0,
) -> Optional[TradePlan]:
    """Does this closed bar break the range, and in which direction.

    Pure and stateless on purpose: the live runner and the backtester both
    call this same function on the same bar shape, so the rules cannot drift
    apart between what was measured and what is traded. Everything stateful
    -- which directions are used up, whether the window has closed -- is the
    caller's, and is passed in.
    """
    if bar_ts < rng.end:
        return None                          # still inside the range itself

    taken = set(taken)
    upper, lower = _levels(rng, cfg)
    high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])

    if cfg.entry_mode == ENTRY_CLOSE:
        long_break, short_break = close > upper, close < lower
        entry_price = close
    else:
        long_break, short_break = high >= upper, low <= lower
        entry_price = None                   # the level itself; set below

    if long_break and short_break:
        # Both sides of the range were taken out inside one minute. Which came
        # first is not recoverable from a bar's OHLC, and guessing is how a
        # backtest awards itself the good half of every whipsaw.
        return None

    if long_break and BUY not in taken:
        direction, level = BUY, upper
    elif short_break and SELL not in taken:
        direction, level = SELL, lower
    else:
        return None

    entry = entry_price if entry_price is not None else level
    stop, target = stop_and_target(rng, cfg, direction, entry, atr_points)

    # A stop on the wrong side means the range and the entry disagree -- a
    # close-through entry that ran past the opposite side of the range, or an
    # ATR stop of zero. Refusing here keeps the impossible order away from
    # the executor, which would refuse it anyway but later and less clearly.
    if (direction == BUY and stop >= entry) or (direction == SELL and stop <= entry):
        return None

    side = "above" if direction == BUY else "below"
    verb = "closed" if cfg.entry_mode == ENTRY_CLOSE else "traded"
    return TradePlan(
        symbol=rng.symbol, direction=direction, entry=entry, stop=stop,
        target=target, at=bar_ts,
        reason=(f"{verb} {side} the {rng.width_points:.0f}pt opening range "
                f"({rng.bars}-bar {cfg.range_minutes}min range from "
                f"{rng.start:%H:%M} UTC)"),
        range_=rng,
    )


def broken_level(rng: OpeningRange, direction: int) -> float:
    """The level a break in ``direction`` went through, and must hold."""
    return rng.high if direction == BUY else rng.low


def is_retest(rng: OpeningRange, direction: int, bar: Mapping[str, float],
              tolerance_fraction: float = 0.10) -> bool:
    """Did price come back to the broken level and hold it on this bar.

    Two conditions, and both matter:

    * price *returned* to the level -- within a tolerance, because a retest that
      stops a tick short is still a retest, and demanding an exact touch means
      most of them are never detected;
    * the bar *closed on the breakout side* of the level. A bar that came back
      and closed through it is not a retest, it is the break failing, and
      calling that a retest is how a "buy the retest" rule ends up buying the
      start of a reversal.

    The tolerance is a fraction of the range width rather than a fixed number of
    points, so one setting means the same thing on gold and on EURUSD.
    """
    level = broken_level(rng, direction)
    slack = rng.width * tolerance_fraction
    close = float(bar["close"])
    if direction == BUY:
        return float(bar["low"]) <= level + slack and close > level
    return float(bar["high"]) >= level - slack and close < level


def break_failed(rng: OpeningRange, direction: int,
                 bar: Mapping[str, float]) -> bool:
    """Has this bar closed back inside the range, cancelling the break.

    Distinct from "not yet retested": a break that closes back inside is over,
    and continuing to wait for its retest means waiting for an event that can no
    longer mean what it meant.
    """
    level = broken_level(rng, direction)
    close = float(bar["close"])
    return close <= level if direction == BUY else close >= level


def breakeven_stop(plan: TradePlan, cfg: OrbConfig) -> Optional[float]:
    """The price at which the stop is moved to entry, or ``None`` if never.

    Worth being clear about what this does and does not do. It removes the
    loss from a trade that has already worked and then stalled, which is the
    common case. It also converts some eventual winners into scratches, by
    stopping them out at entry on a pullback they would have survived. Which
    effect dominates is instrument-specific and measurable -- so it is a
    setting, and ``orb_backtest`` reports both runs.
    """
    if cfg.breakeven_at_r is None:
        return None
    move = cfg.breakeven_at_r * plan.risk
    return plan.entry + move * (1 if plan.direction == BUY else -1)


INDEX_PREFIXES = (
    "US30", "DJ30", "WS30", "DOW", "SPX", "US500", "SP500", "NAS", "USTEC",
    "NDX", "US2000", "RUSSELL", "GER", "DE40", "DAX", "UK100", "FRA", "EU50",
    "STOXX", "JP225", "JPN225",
)


def is_index(symbol: str) -> bool:
    from .clock import base_name

    return base_name(symbol).startswith(INDEX_PREFIXES)


def opens_on_an_auction(symbol: str) -> bool:
    """Does this instrument have a real opening event to measure?

    The question the whole strategy rests on. An index or a single stock opens
    with an auction: a bell, a crossing price, and an order imbalance that takes
    a few minutes to clear. Spot FX and metals do not -- London "opens" as a
    gradual handover of liquidity from Asia, and no single minute is special.
    """
    from .clock import INSTRUMENT_SESSIONS, base_name

    name = base_name(symbol)
    if name.startswith(INDEX_PREFIXES):
        return True
    # A single stock is whatever is mapped to a cash-equity session and is not
    # one of the index CFDs. Asking the session table rather than keeping a
    # second list of tickers here, because two lists drift.
    return INSTRUMENT_SESSIONS.get(name) in ("us_cash", "comex") and \
        not name.startswith(("XAU", "XAG", "XPT", "XPD"))


def suggest_range_minutes(symbol: str) -> int:
    """A starting range length, with the reason rather than a lookup table.

    Instruments with an opening auction -- indices and single stocks -- get
    fifteen minutes, because that is roughly how long the auction's imbalance
    takes to clear.

    Spot FX and metals have no auction at all, so a fifteen-minute window on a
    slow liquidity handover produces a range too narrow to mean anything.
    Thirty minutes is the adjustment, and it is still the weakest case: this is
    why FX came out of the default watchlist.

    Returned as a suggestion the caller applies, never applied silently on top
    of a value someone chose -- a config that quietly disagrees with the
    config file is worse than a bad default.
    """
    return 15 if opens_on_an_auction(symbol) else 30
