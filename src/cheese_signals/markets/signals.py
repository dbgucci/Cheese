"""ORB signals: break, then retest. Two alerts per setup.

Signals only. Nothing here places, modifies or closes an order, and there is no
code path that could -- which is why this module is smaller and safer than
``autobot.py``. It needs no algo-trading permission and no live account, and it
cannot cost anything if it is wrong.

The sequence it watches, per instrument per day
-----------------------------------------------
1. **Mark the range.** The high and low of the first 15 minutes after that
   market's own open -- 09:30 New York for the US indices, 08:00 London for
   gold, silver and FX. In the exchange's clock, so it is right in both
   summer and winter.
2. **BREAK.** A bar *closes* beyond the high or the low. First alert.
3. **RETEST.** Price comes back to the level it broke and holds it: a bar trades
   down to the level (within a tolerance) but still closes on the breakout side.
   Second alert, usually the better entry.

A break that closes back *inside* the range is not waiting for its retest -- it
has failed, and the setup is marked dead rather than left armed. Treating a
failed break as "still pending" is how a retest alert arrives at the start of a
reversal.

The rules themselves are not reimplemented here. ``orb.py`` owns them, and it is
the version with the walk-forward backtest behind it, so a signal is the same
event the backtest counted. A signal module with its own private copy of the
rules is one that quietly disagrees with its own evidence.

What a signal contains, and why
-------------------------------
"BUY XAUUSD" alone is not actionable: without the stop there is no way to size
it, and without the range there is no way to judge whether the setup was worth
taking. So each alert carries entry, stop, target, the range it broke, and how
that range compares to the spread -- the last being the number that decides
whether the trade can pay for itself.

Duplicates
----------
A break stays broken: price sits above the range all morning, so a naive poll
re-detects it every twenty seconds and sends forty identical alerts. Each stage
fires once per instrument per session, tracked by the state machine below.

Did it work? The paper record
-----------------------------
The retest is the entry, so from there the alert can be scored: follow the bars
until either the published stop or the published target is touched, and record
win or loss. That turns a stream of suggestions into something with a hit rate
attached, which is the only way to find out whether the alerts are worth acting
on -- and it costs nothing, because no position exists.

Three rules keep the record honest, and they are the same ones
``orb_backtest`` argues for at length:

* **The entry bar cannot resolve the trade.** The fill is at the retest level
  somewhere inside that minute, and OHLC does not say when. Letting the same bar
  hit the target is a one-bar lookahead worth a lot of imaginary profit.
* **A bar containing both the stop and the target counts as a loss.** Which came
  first is unknowable from OHLC, and optimism here is invisible in the output.
  Those trades are flagged as ambiguous so their share can be checked.
* **The result is reported gross and net.** Levels are tested exactly as the
  alert published them -- what the chart shows -- and the round-trip cost from
  the range's own spread is then deducted for the net figure. A short is the
  weak spot: MetaTrader bars are the bid and a short exits on the ask, so its
  stop is genuinely nearer than the chart shows. The backtester prices both
  sides of the book; this record deducts the cost instead and says so.

A paper result is not a fill. It assumes the retest limit filled at the level,
which a fast market may not have done at all.
"""

from __future__ import annotations

import argparse
import time as time_mod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional, Protocol

import pandas as pd

from . import orb
from .clock import BarClock, measure_server_offset, session_for
from .execution import BUY, SELL
from .mt5_bridge import M1

# How much history a cycle needs: this session, plus the days behind it that the
# average-daily-range filter measures against.
LOOKBACK_DAYS = 21


class BarSource(Protocol):
    """Anything that can return 1-minute bars. Deliberately tiny.

    ``MT5Feed`` already satisfies this, and so does ``ReplayFeed``, which is
    what the tests use. Keeping the requirement to one method is what lets the
    same bot run against a broker terminal, a CSV, or a free data API without
    knowing which.
    """

    def history(self, symbol: str, timeframe: str, start: datetime,
                end: datetime) -> pd.DataFrame: ...


BREAK = "break"
RETEST = "retest"

WIN = "win"
LOSS = "loss"
FLAT = "flat"        # neither level was touched before the session's flat-by time


def reference(symbol: str, day: date, kind: str) -> str:
    """A short handle for one stage of one setup, e.g. ``US30-0813-RETEST``.

    So a subscriber can ask about a specific alert, and so the result message
    can name the entry it belongs to instead of leaving the reader to match up
    prices from memory. Deterministic, not a counter: the same setup produces
    the same reference on any machine that saw it.
    """
    return f"{symbol}-{day:%m%d}-{kind.upper()}"


@dataclass(frozen=True)
class Signal:
    """One stage of one setup, with everything needed to act on or judge it."""

    kind: str                     # BREAK or RETEST
    symbol: str
    direction: int
    at: datetime
    entry: float
    stop: float
    target: float
    range_low: float
    range_high: float
    range_points: float
    risk_points: float
    reward_points: float
    cost_points: float
    session_label: str
    session_open: datetime
    flat_by: datetime
    reason: str
    digits: int = 5
    # Everything below is for saying *when*, to a reader whose clock is unknown.
    # ``session_tz`` is the market's own zone, ``chart_offset_minutes`` is how
    # far the broker's clock -- the one drawn on a MetaTrader chart -- runs ahead
    # of UTC, and ``reader_tz`` is an optional zone the alerts are addressed to.
    session_tz: str = "UTC"
    session_key: str = ""
    range_minutes: int = 15
    chart_offset_minutes: int = 0
    reader_tz: str = ""
    ref: str = ""                 # e.g. US30-0813-RETEST, quotable in a message

    @property
    def side(self) -> str:
        return "BUY" if self.direction == BUY else "SELL"

    @property
    def headline(self) -> str:
        stage = "BREAK" if self.kind == BREAK else "RETEST"
        return f"{stage}  {self.symbol}  {self.side}"

    @property
    def chart_at(self) -> datetime:
        """The candle to look at, stamped the way the platform stamps it."""
        from .clock import chart_time

        return chart_time(self.at, self.chart_offset_minutes)

    def when_lines(self) -> list[str]:
        from .clock import SESSIONS, when_lines

        spec = SESSIONS.get(self.session_key)
        if spec is None:
            from .clock import SessionSpec
            from datetime import time as _time

            spec = SessionSpec("custom", self.session_tz, _time(0, 0),
                               _time(23, 59), self.session_label)
        return when_lines(self.at, spec, self.chart_offset_minutes, self.reader_tz)

    @property
    def cost_multiple(self) -> float:
        return self.range_points / self.cost_points if self.cost_points > 0 else 0.0

    def _price(self, value: float) -> str:
        return f"{value:.{self.digits}f}"

    def format(self) -> str:
        """The alert, written to be read on a phone in five seconds.

        The prices come first because they are what someone acts on. The times
        come last and take four lines rather than one, because the reader is in
        an unknown zone on an unknown platform and a single time is a guess about
        both.
        """
        lines = [
            self.headline,
            "",
            f"Entry   {self._price(self.entry)}",
            f"Stop    {self._price(self.stop)}   ({self.risk_points:.0f} pts)",
            f"Target  {self._price(self.target)}   ({self.reward_points:.0f} pts)",
            "",
            f"Range   {self._price(self.range_low)} - {self._price(self.range_high)}"
            f"  ({self.range_points:.0f} pts)",
        ]
        if self.cost_points > 0:
            lines.append(f"Spread  {self.cost_points:.0f} pts  "
                         f"(range is {self.cost_multiple:.1f}x it)")
        lines += ["", "WHEN"] + [f"  {line}" for line in self.when_lines()]
        lines.append(f"  {self.session_summary()}")
        if self.ref:
            lines += ["", f"Ref {self.ref}"]
        return "\n".join(lines)

    def session_summary(self) -> str:
        from .clock import SESSIONS, session_window

        spec = SESSIONS.get(self.session_key)
        if spec is None:
            return (f"{self.session_label}: flat by {self.flat_by:%H:%M} UTC")
        return session_window(spec, spec.session_date(self.at),
                              self.range_minutes, self.flat_by)

    def one_line(self) -> str:
        return (f"{self.at:%H:%M} {self.kind.upper():6s} {self.symbol} {self.side} "
                f"@ {self._price(self.entry)} stop {self._price(self.stop)} "
                f"target {self._price(self.target)} ({self.range_points:.0f}pt range)")


@dataclass(frozen=True)
class Outcome:
    """How a retest alert would have finished, judged on its published levels.

    ``r_gross`` is the move in multiples of the risk taken: +2.0 for a target hit
    at a 2R target, -1.0 for a stop. ``r_net`` deducts one round-trip cost, so it
    is the number to believe. Both are reported because their difference is the
    whole argument about whether a setup is worth taking: a 250-point range that
    pays 2R gross keeps almost all of it, and a 40-point one does not.
    """

    symbol: str
    direction: int
    result: str                   # WIN, LOSS or FLAT
    entry: float
    stop: float
    target: float
    exit_price: float
    opened_at: datetime
    closed_at: datetime
    risk_points: float
    points: float                 # signed, in points, gross
    cost_points: float
    r_gross: float
    r_net: float
    session_label: str
    reason: str
    ambiguous: bool = False       # one bar held both levels; scored as a loss
    digits: int = 5
    session_tz: str = "UTC"
    session_key: str = ""
    chart_offset_minutes: int = 0
    reader_tz: str = ""
    ref: str = ""                 # the retest alert this is the answer to

    @property
    def side(self) -> str:
        return "BUY" if self.direction == BUY else "SELL"

    @property
    def minutes_held(self) -> float:
        return (self.closed_at - self.opened_at).total_seconds() / 60.0

    @property
    def headline(self) -> str:
        return (f"{self.result.upper()}  {self.symbol}  {self.side}  "
                f"{self.r_net:+.2f}R")

    def _price(self, value: float) -> str:
        return f"{value:.{self.digits}f}"

    def when_lines(self) -> list[str]:
        from .clock import SESSIONS, when_lines

        spec = SESSIONS.get(self.session_key)
        if spec is None:
            return [f"{self.closed_at:%a %d %b %Y}", f"{self.closed_at:%H:%M} UTC"]
        return when_lines(self.closed_at, spec, self.chart_offset_minutes,
                          self.reader_tz)

    def format(self) -> str:
        lines = [
            self.headline,
            "",
            f"Entry   {self._price(self.entry)}",
            f"Exit    {self._price(self.exit_price)}   ({self.points:+.0f} pts)",
            f"Result  {self.r_net:+.2f}R net   ({self.r_gross:+.2f}R before the "
            f"{self.cost_points:.0f}pt round trip)",
            "",
            f"{self.reason}",
            f"Held {self.minutes_held:.0f} min  ·  {self.session_label}",
            "",
            "CLOSED",
        ]
        lines += [f"  {line}" for line in self.when_lines()]
        if self.ambiguous:
            lines += ["", "One bar held both the stop and the target; scored as a "
                          "loss because OHLC cannot say which came first."]
        if self.ref:
            lines += ["", f"Ref {self.ref}"]
        return "\n".join(lines)

    def one_line(self) -> str:
        return (f"{self.closed_at:%H:%M} {self.result.upper():5s} {self.symbol} "
                f"{self.side} exit {self._price(self.exit_price)} "
                f"{self.r_net:+.2f}R net ({self.r_gross:+.2f}R gross)")


@dataclass
class PaperTrade:
    """A retest alert being followed to its stop or target. Nothing is placed."""

    symbol: str
    direction: int
    entry: float
    stop: float
    target: float
    opened_at: datetime
    flat_by: datetime
    risk_points: float
    cost_points: float
    point: float
    session_label: str
    digits: int = 5
    session_tz: str = "UTC"
    session_key: str = ""
    chart_offset_minutes: int = 0
    reader_tz: str = ""
    ref: str = ""
    outcome: Optional[Outcome] = None

    @property
    def closed(self) -> bool:
        return self.outcome is not None


@dataclass
class Tally:
    """The running record. Win rate on its own is not enough to judge alerts by:
    a 30% hit rate at 2R is profitable and a 60% one at 0.5R is not, so total R
    is carried alongside it."""

    wins: int = 0
    losses: int = 0
    flats: int = 0
    r_net: float = 0.0
    r_gross: float = 0.0
    ambiguous: int = 0

    @property
    def resolved(self) -> int:
        return self.wins + self.losses + self.flats

    @property
    def win_rate(self) -> float:
        """Flats excluded from the denominator: a trade that touched neither
        level did not win or lose, and counting it as a loss makes a quiet
        morning look like a bad strategy."""
        decided = self.wins + self.losses
        return self.wins / decided if decided else 0.0

    def add(self, outcome: Outcome) -> None:
        if outcome.result == WIN:
            self.wins += 1
        elif outcome.result == LOSS:
            self.losses += 1
        else:
            self.flats += 1
        self.r_net += outcome.r_net
        self.r_gross += outcome.r_gross
        self.ambiguous += int(outcome.ambiguous)

    def summary(self) -> str:
        if not self.resolved:
            return "no results yet"
        parts = [f"{self.wins}W / {self.losses}L"]
        if self.flats:
            parts.append(f"{self.flats} flat")
        parts.append(f"{self.win_rate * 100:.0f}%")
        parts.append(f"{self.r_net:+.2f}R net")
        return "  ·  ".join(parts)


@dataclass
class SignalConfig:
    symbols: list[str] = field(default_factory=list)
    orb: orb.OrbConfig = field(default_factory=orb.OrbConfig)
    sessions: dict[str, str] = field(default_factory=dict)
    poll_seconds: int = 20
    # 15 minutes for every instrument. The per-instrument adjustment that
    # ``autobot`` applies (30 for FX and metals, which open by liquidity
    # handover rather than by auction) is available but off, because "the first
    # 15 minutes of market open" is the rule being asked for.
    per_symbol_range_minutes: bool = False
    # How close a pullback has to get to the broken level to count as a retest,
    # as a fraction of the range width -- scale-free, so one number means the
    # same thing on gold and on EURUSD.
    retest_tolerance_fraction: float = 0.10
    # Filters can be relaxed for a signal bot: nothing is being risked, so a
    # marginal setup is information rather than a loss. They are on by default
    # anyway, because an alert for a range narrower than the spread is noise.
    apply_filters: bool = True
    # Follow each retest alert to its stop or target and record the result.
    # Off means the day ends at the retest, exactly as it did before this
    # existed -- which is the only reason the switch is here.
    track_outcomes: bool = True
    # An extra zone to print every time in, for whoever the alerts are addressed
    # to. Empty means UTC, the market's own clock and the broker's chart clock,
    # which is already three frames -- this is for when the audience is mostly in
    # a fourth. It is not the sender's machine zone: that would be right for one
    # person and misleading for everyone else on the channel.
    reader_timezone: str = ""

    def validate(self) -> list[str]:
        out = list(self.orb.validate())
        if not self.symbols:
            out.append("no symbols configured, so there is nothing to watch")
        if self.poll_seconds <= 0:
            out.append("poll_seconds must be positive")
        return out


ARMED = "armed"           # range marked, waiting for a break
BROKEN = "broken"         # break alerted, waiting for the retest
RETESTED = "retested"     # both alerts sent; the paper trade is now running
FAILED = "failed"         # the break closed back inside the range
SKIPPED = "skipped"       # the day did not qualify


@dataclass
class Watch:
    """What one instrument is doing today. The state machine's memory."""

    symbol: str
    session_date: date
    state: str = ARMED
    direction: int = 0
    range_: Optional[orb.OpeningRange] = None
    broke_at: Optional[datetime] = None
    reason: str = ""
    trade: Optional[PaperTrade] = None

    @property
    def waiting_for_retest(self) -> bool:
        return self.state == BROKEN

    @property
    def following(self) -> bool:
        """A retest fired and its result is not known yet."""
        return self.trade is not None and not self.trade.closed

    @property
    def done(self) -> bool:
        if self.state in (FAILED, SKIPPED):
            return True
        # RETESTED used to end the day outright. It still does when outcomes are
        # not being tracked -- but with a paper trade running there is one more
        # thing to find out, so the day is not over until it resolves.
        if self.state == RETESTED:
            return not self.following
        return False


class SignalBot:
    """Polls a bar source and walks each instrument through break then retest."""

    def __init__(
        self,
        source: BarSource,
        config: Optional[SignalConfig] = None,
        clock: Optional[BarClock] = None,
        specs: Optional[dict[str, object]] = None,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self.source = source
        self.config = config or SignalConfig()
        problems = self.config.validate()
        if problems:
            raise ValueError("contradictory signal config: " + "; ".join(problems))
        self.clock = clock or BarClock()
        self._specs = specs or {}
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.watches: dict[tuple[str, date], Watch] = {}
        self.signals: list[Signal] = []
        self.outcomes: list[Outcome] = []
        self.tally = Tally()
        self._taken = 0
        self.notes: list[str] = []
        self.last_bars: dict[str, pd.DataFrame] = {}
        self._said: dict[str, str] = {}

    # ------------------------------------------------------------- plumbing
    def calibrate(self, symbol: Optional[str] = None,
                  real_now: Optional[datetime] = None) -> BarClock:
        """Measure the data source's clock offset, if it has one to measure.

        A source that serves real UTC needs no correction; a broker terminal
        almost certainly does, and an uncorrected offset moves every session
        boundary by hours. Sources without a ``server_time`` are assumed to be
        UTC, which is stated rather than silently hoped for.
        """
        getter = getattr(self.source, "server_time", None)
        if getter is None:
            self.clock = BarClock(0)
            self._note("the data source has no clock to measure; assuming its "
                       "bar times are already UTC")
            return self.clock
        target = symbol or self.config.symbols[0]
        self.clock = BarClock(measure_server_offset(getter(target), real_now))
        self._note(self.clock.describe())
        return self.clock

    def _note(self, text: str) -> None:
        self.notes.append(f"[{self._now():%H:%M:%S}] {text}")
        if len(self.notes) > 500:
            del self.notes[:250]

    def _say(self, symbol: str, text: str) -> None:
        """Record a reason, but only when it changes.

        A standing condition -- the range too narrow, the window closed -- would
        otherwise repeat every poll and bury the signals.
        """
        if self._said.get(symbol) == text:
            return
        self._said[symbol] = text
        self._note(f"{symbol}: {text}")

    def _point_and_digits(self, symbol: str) -> tuple[float, int]:
        spec = self._specs.get(symbol)
        if spec is None:
            getter = getattr(self.source, "spec", None)
            if getter is not None:
                try:
                    spec = getter(symbol)
                    self._specs[symbol] = spec
                except (KeyError, RuntimeError):
                    spec = None
        if spec is None:
            return 1.0, 5
        return float(getattr(spec, "point", 1.0)), int(getattr(spec, "digits", 5))

    def _bars(self, symbol: str, now: datetime) -> pd.DataFrame:
        start = self.clock.to_server(now - timedelta(days=LOOKBACK_DAYS))
        raw = self.source.history(symbol, M1, start, self.clock.to_server(now))
        bars = orb.as_utc_index(self.clock.frame_to_utc(raw)).sort_index()
        # Kept so a caller can draw the setup that produced an alert. Only the
        # last two sessions: the twenty-one days fetched for the average-daily-
        # range filter are of no use to a chart and would hold ten thousand rows
        # per instrument in memory all day.
        self.last_bars[symbol] = bars.tail(2 * 1440)
        return bars

    def _cfg_for(self, symbol: str) -> orb.OrbConfig:
        if not self.config.per_symbol_range_minutes:
            return self.config.orb
        from dataclasses import replace

        return replace(self.config.orb,
                       range_minutes=orb.suggest_range_minutes(symbol))

    # ---------------------------------------------------------------- cycle
    def cycle(self, now: Optional[datetime] = None) -> list[Signal]:
        """One pass over every instrument. Returns only the new alerts."""
        now = now or self._now()
        found: list[Signal] = []
        for symbol in self.config.symbols:
            try:
                found += self._check(symbol, now)
            except (KeyError, RuntimeError, ValueError) as exc:
                self._say(symbol, f"could not check: {exc}")
        self._sweep_stale(now)
        self.signals += found
        return found

    def _sweep_stale(self, now: datetime) -> None:
        """Close out any trade left open by a session that is well over.

        Normally the bar at the cut-off arrives and settles the trade. If the
        feed stops before it -- a Friday close, a holiday, a connection dropped
        over the evening -- the watch would sit in "following" for a day that has
        ended, and its result would never be recorded at all. Worse, once the
        session date rolls over, nothing looks at that watch again.

        Closed at the entry, because that is what "neither level was touched" is
        worth, with a reason that does not pretend to know more than that. Run
        after the per-symbol checks so the settlement that can see bars always
        gets first refusal, and an hour late so it cannot race it.
        """
        for watch in self.watches.values():
            if not watch.following:
                continue
            trade = watch.trade
            if now < trade.flat_by + timedelta(hours=1):
                continue
            self._close_trade(
                watch, trade.flat_by, trade.entry, FLAT,
                "the session ended and the feed stopped before either level "
                "was touched")

    def _watch(self, symbol: str, day: date) -> Watch:
        key = (symbol, day)
        if key not in self.watches:
            self.watches[key] = Watch(symbol=symbol, session_date=day)
        return self.watches[key]

    def _check(self, symbol: str, now: datetime) -> list[Signal]:
        """Advance one instrument's state machine, emitting any new alerts.

        A list rather than a single signal, because one poll can legitimately
        produce both stages: if the bot starts late, or a poll is missed, the
        break and its retest may both be sitting in the bars already, and
        dropping one of them would leave the state machine stuck.
        """
        try:
            spec = session_for(symbol, self.config.sessions.get(symbol))
        except KeyError as exc:
            self._say(symbol, str(exc))
            return []

        cfg = self._cfg_for(symbol)
        day = spec.session_date(now)
        if not spec.is_open_weekday(day):
            return []

        watch = self._watch(symbol, day)
        if watch.done:
            return []

        open_at = spec.open_utc(day)
        range_end = open_at + timedelta(minutes=cfg.range_minutes)
        if now < range_end:
            return []                          # the range is still forming

        point, digits = self._point_and_digits(symbol)
        bars = self._bars(symbol, now)
        if bars.empty:
            self._say(symbol, "no bars came back")
            return []

        rng = watch.range_
        if rng is None:
            rng = orb.build_range(bars, symbol, spec, day, cfg, point)
            if rng is None:
                self._say(symbol, f"no bars inside the {open_at:%H:%M}-"
                                  f"{range_end:%H:%M} UTC opening range")
                return []
            if self.config.apply_filters:
                adr = orb.average_daily_range_points(
                    bars, spec, point, cfg.adr_days, day)
                verdict = orb.check_range(rng, cfg, adr)
                if not verdict:
                    watch.state = SKIPPED
                    watch.reason = verdict.reason
                    self._say(symbol, verdict.reason)
                    return []
            watch.range_ = rng
            self._say(symbol, f"range marked {rng.low:.{digits}f}-{rng.high:.{digits}f} "
                              f"({rng.width_points:.0f}pt); watching for a break")

        # A running paper trade is the only thing left for the day: the entry
        # window is finished, and the deadline check below would otherwise mark
        # the day skipped from underneath it.
        if watch.following:
            self._settle(watch, bars, now)
            return []

        deadline = orb.entry_deadline(rng, cfg, spec)
        if now > deadline and not watch.waiting_for_retest:
            watch.state = SKIPPED
            watch.reason = "the window closed with no break"
            self._say(symbol, watch.reason)
            return []

        # Only *closed* bars, and only ones not already walked past. A bar in
        # progress has no close, and treating its running price as one produces
        # alerts that vanish a minute later.
        minute = now.replace(second=0, microsecond=0)
        after = watch.broke_at or rng.end
        window = bars[(bars.index >= after) & (bars.index < minute)]
        if watch.broke_at is not None:
            window = window[window.index > watch.broke_at]
        if window.empty:
            return []

        out: list[Signal] = []
        for ts, bar in window.iterrows():
            if watch.state == ARMED:
                signal = self._try_break(watch, rng, ts, bar, cfg, spec, day,
                                         open_at, digits)
                if signal is not None:
                    out.append(signal)
                continue

            if watch.state == BROKEN:
                signal = self._try_retest(watch, rng, ts, bar, cfg, spec, day,
                                          open_at, digits)
                if signal is not None:
                    out.append(signal)
                if watch.done or watch.following:
                    break

        # The retest may have fired several bars back -- a late start, or a
        # missed poll -- in which case its result is already in the history and
        # settling now is not lookahead, it is catching up.
        if watch.following:
            self._settle(watch, bars, now)
        return out

    def _try_break(self, watch: Watch, rng, ts, bar, cfg, spec, day,
                   open_at, digits) -> Optional[Signal]:
        plan = orb.breakout(rng, ts, bar, cfg)
        if plan is None:
            return None
        if self.config.apply_filters:
            ok = orb.check_plan(plan, cfg)
            if not ok:
                self._say(watch.symbol, ok.reason)
                return None
        watch.state = BROKEN
        watch.direction = plan.direction
        watch.broke_at = ts
        return self._signal(BREAK, watch, rng, plan.direction, ts,
                            entry=plan.entry, stop=plan.stop, target=plan.target,
                            spec=spec, day=day, open_at=open_at, digits=digits,
                            reason=plan.reason, cfg=cfg)

    def _try_retest(self, watch: Watch, rng, ts, bar, cfg, spec, day,
                    open_at, digits) -> Optional[Signal]:
        direction = watch.direction
        # Failure is checked first. A bar that closed back inside the range is
        # not a pullback waiting to be bought -- the break is over, and calling
        # it a retest is how the alert lands at the start of a reversal.
        if orb.break_failed(rng, direction, bar):
            watch.state = FAILED
            watch.reason = (f"the break closed back inside the range at "
                            f"{float(bar['close']):.{digits}f}")
            self._say(watch.symbol, watch.reason)
            return None

        if not orb.is_retest(rng, direction, bar,
                             self.config.retest_tolerance_fraction):
            return None

        # The retest entry is the level itself: that is the price the pullback
        # offers, and it is why this alert is worth waiting for.
        level = orb.broken_level(rng, direction)
        stop, target = orb.stop_and_target(rng, cfg, direction, level)
        if (direction == BUY and stop >= level) or (direction == SELL and stop <= level):
            return None
        watch.state = RETESTED
        signal = self._signal(
            RETEST, watch, rng, direction, ts, entry=level, stop=stop,
            target=target, spec=spec, day=day, open_at=open_at, digits=digits,
            reason=(f"came back to the {level:.{digits}f} level and held it "
                    f"(broke at {watch.broke_at:%H:%M} UTC)"),
            cfg=cfg)
        if self.config.track_outcomes:
            watch.trade = PaperTrade(
                symbol=watch.symbol, direction=direction, entry=signal.entry,
                stop=signal.stop, target=signal.target, opened_at=ts,
                flat_by=signal.flat_by, risk_points=signal.risk_points,
                cost_points=signal.cost_points, point=rng.point or 1.0,
                session_label=spec.label, digits=digits, session_tz=spec.tz,
                session_key=spec.key,
                chart_offset_minutes=self.clock.server_offset_minutes,
                reader_tz=self.config.reader_timezone, ref=signal.ref)
        return signal

    # ------------------------------------------------------------ the result
    def _settle(self, watch: Watch, bars: pd.DataFrame,
                now: datetime) -> Optional[Outcome]:
        """Walk the bars after the entry until a level is touched.

        Only closed bars, and never the entry bar: the fill happened somewhere
        inside that minute and OHLC does not say where, so letting it resolve
        the trade would be a bar of lookahead.
        """
        trade = watch.trade
        if trade is None or trade.closed:
            return None
        minute = now.replace(second=0, microsecond=0)
        window = bars[(bars.index > trade.opened_at) & (bars.index < minute)]

        for ts, bar in window.iterrows():
            # The flatten comes first: past that time the position no longer
            # exists, so a level touched later is not this trade's business. The
            # exit is that bar's open, which is the price at the flatten moment.
            if ts >= trade.flat_by:
                price = float(bar.get("open", bar["close"]))
                return self._close_trade(
                    watch, ts, price, FLAT,
                    "flattened at the session's cut-off with neither level touched")

            high, low = float(bar["high"]), float(bar["low"])
            if trade.direction == BUY:
                stop_hit, target_hit = low <= trade.stop, high >= trade.target
            else:
                stop_hit, target_hit = high >= trade.stop, low <= trade.target

            # A bar holding both is scored as a loss. Which came first is not in
            # the data, and guessing favourably is the single easiest way to
            # invent a win rate.
            ambiguous = bool(stop_hit and target_hit)
            if stop_hit:
                return self._close_trade(
                    watch, ts, trade.stop, LOSS,
                    f"the stop at {trade.stop:.{trade.digits}f} was touched",
                    ambiguous=ambiguous)
            if target_hit:
                return self._close_trade(
                    watch, ts, trade.target, WIN,
                    f"the target at {trade.target:.{trade.digits}f} was reached")

        # Bars stop for weekends, holidays and feed gaps, so the flat-by bar may
        # never arrive. Once the clock is past it, close on the last price there
        # was rather than following the trade into next week.
        #
        # The grace period is what makes this a fallback rather than the usual
        # path: the bar stamped at the cut-off has not closed yet when the clock
        # first passes it, and without the wait this fired one poll early and
        # exited at the previous bar's close instead of the cut-off price.
        if not window.empty and now >= trade.flat_by + timedelta(minutes=2):
            return self._close_trade(
                watch, window.index[-1], float(window["close"].iloc[-1]), FLAT,
                "the session ended with neither level touched")
        return None

    def _close_trade(self, watch: Watch, ts: datetime, exit_price: float,
                     result: str, reason: str,
                     ambiguous: bool = False) -> Outcome:
        trade = watch.trade
        sign = 1.0 if trade.direction == BUY else -1.0
        points = (exit_price - trade.entry) / trade.point * sign
        risk = trade.risk_points or 1.0
        outcome = Outcome(
            symbol=trade.symbol, direction=trade.direction, result=result,
            entry=trade.entry, stop=trade.stop, target=trade.target,
            exit_price=float(exit_price), opened_at=trade.opened_at,
            closed_at=ts, risk_points=trade.risk_points, points=points,
            cost_points=trade.cost_points,
            r_gross=points / risk,
            r_net=(points - trade.cost_points) / risk,
            session_label=trade.session_label, reason=reason,
            ambiguous=ambiguous, digits=trade.digits,
            session_tz=trade.session_tz, session_key=trade.session_key,
            chart_offset_minutes=trade.chart_offset_minutes,
            reader_tz=trade.reader_tz, ref=trade.ref,
        )
        trade.outcome = outcome
        self.outcomes.append(outcome)
        self.tally.add(outcome)
        self._note(f"{trade.symbol}: {result.upper()} — {reason} "
                   f"({outcome.r_net:+.2f}R net). Record: {self.tally.summary()}")
        # Cleared so a standing reason from earlier in the day does not suppress
        # tomorrow's first note about this instrument.
        self._said.pop(trade.symbol, None)
        return outcome

    def _signal(self, kind, watch, rng, direction, ts, *, entry, stop, target,
                spec, day, open_at, digits, reason, cfg) -> Signal:
        point = rng.point or 1.0
        return Signal(
            kind=kind, symbol=watch.symbol, direction=direction, at=ts,
            entry=float(entry), stop=float(stop), target=float(target),
            range_low=rng.low, range_high=rng.high,
            range_points=rng.width_points,
            risk_points=abs(entry - stop) / point,
            reward_points=abs(target - entry) / point,
            cost_points=rng.cost_points,
            session_label=spec.label, session_open=open_at,
            flat_by=spec.close_utc(day) - timedelta(
                minutes=cfg.flat_before_close_minutes),
            reason=reason, digits=digits,
            session_tz=spec.tz, session_key=spec.key,
            range_minutes=cfg.range_minutes,
            chart_offset_minutes=self.clock.server_offset_minutes,
            reader_tz=self.config.reader_timezone,
            ref=reference(watch.symbol, day, kind),
        )

    def take_outcomes(self) -> list[Outcome]:
        """Results recorded since the last call. Drained rather than returned by
        ``cycle`` so that adding this did not change what ``cycle`` returns."""
        fresh = self.outcomes[self._taken:]
        self._taken = len(self.outcomes)
        return fresh

    # ----------------------------------------------------------------- loop
    def run(self, on_signal: Callable[[Signal], None],
            cycles: Optional[int] = None,
            on_outcome: Optional[Callable[[Outcome], None]] = None
            ) -> None:      # pragma: no cover
        done = 0
        while cycles is None or done < cycles:
            for signal in self.cycle():
                on_signal(signal)
            if on_outcome is not None:
                for outcome in self.take_outcomes():
                    on_outcome(outcome)
            done += 1
            if cycles is None or done < cycles:
                time_mod.sleep(self.config.poll_seconds)


# --------------------------------------------------------------------------
# the CLI
# --------------------------------------------------------------------------
DEFAULT_SYMBOLS = ["XAUUSD", "XAGUSD", "US30", "SPX500", "NAS100",
                   "EURUSD", "GBPUSD", "USDJPY"]


def build_notifier(token: Optional[str], chat_id: Optional[str]):  # pragma: no cover
    import os

    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return None
    from ..notifiers import TelegramNotifier

    return TelegramNotifier(token, chat_id)


def main(argv: Optional[list[str]] = None) -> int:        # pragma: no cover
    ap = argparse.ArgumentParser(
        prog="python -m cheese_signals.markets.signals",
        description="Opening range breakout signals. Sends alerts; places nothing.")
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--range-minutes", type=int, default=15,
                    help="opening range length; FX and metals get 30 automatically")
    ap.add_argument("--target-r", type=float, default=2.0)
    ap.add_argument("--poll", type=int, default=20, help="seconds between checks")
    ap.add_argument("--no-filters", action="store_true",
                    help="alert on every break, even a range narrower than the spread")
    ap.add_argument("--telegram-token", default=None)
    ap.add_argument("--telegram-chat", default=None)
    ap.add_argument("--login", type=int, default=None)
    ap.add_argument("--password", default="")
    ap.add_argument("--server", default="")
    ap.add_argument("--terminal", default=None, help="path to terminal64.exe")
    ap.add_argument("--once", action="store_true", help="check once and exit")
    args = ap.parse_args(argv)

    from .mt5_bridge import MT5Feed
    from .survey import resolve

    # Read-only: MT5Feed cannot place an order, so this needs no algo-trading
    # permission and there is no path from here to the account.
    source = MT5Feed(login=args.login, password=args.password,
                     server=args.server, terminal_path=args.terminal)
    try:
        found, missing = resolve(source, args.symbols or DEFAULT_SYMBOLS)
        for m in missing:
            print(f"  ! not offered on this account: {m}")
        if not found:
            print("None of the requested instruments exist on this account.")
            return 1

        config = SignalConfig(
            symbols=sorted(found.values()),
            orb=orb.OrbConfig(range_minutes=args.range_minutes,
                              target_r=args.target_r),
            poll_seconds=args.poll,
            apply_filters=not args.no_filters,
        )
        bot = SignalBot(source, config)
        bot.calibrate()
        for note in bot.notes:
            print(note)

        notifier = build_notifier(args.telegram_token, args.telegram_chat)
        if notifier is None:
            print("No Telegram credentials, so alerts print here only. Set "
                  "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to get them on your phone.")
        else:
            ok, err = notifier.send_verbose("ORB signals: watching "
                                            + ", ".join(config.symbols))
            print("Telegram connected." if ok else f"Telegram failed: {err}")

        today = datetime.now(timezone.utc).date()
        print("\nWATCHING (times in UTC)")
        for symbol in config.symbols:
            try:
                spec = session_for(symbol)
            except KeyError:
                continue
            minutes = orb.suggest_range_minutes(symbol)
            open_at = spec.open_utc(today)
            print(f"  {symbol:12s} {spec.label:20s} range {open_at:%H:%M}-"
                  f"{open_at + timedelta(minutes=minutes):%H:%M}")
        print("\nCtrl+C to stop.\n")

        def emit(signal: Signal) -> None:
            print(signal.format())
            print()
            if notifier is not None:
                ok, err = notifier.send_verbose(signal.format())
                if not ok:
                    print(f"  ! Telegram: {err}")

        def emit_result(outcome: Outcome) -> None:
            print(outcome.format())
            print(f"Record: {bot.tally.summary()}")
            print()
            if notifier is not None:
                ok, err = notifier.send_verbose(
                    f"{outcome.format()}\n\nRecord: {bot.tally.summary()}")
                if not ok:
                    print(f"  ! Telegram: {err}")

        bot.run(emit, cycles=1 if args.once else None, on_outcome=emit_result)
    finally:
        try:
            source.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(main())
