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

    @property
    def side(self) -> str:
        return "BUY" if self.direction == BUY else "SELL"

    @property
    def headline(self) -> str:
        stage = "BREAK" if self.kind == BREAK else "RETEST"
        return f"{stage}  {self.symbol}  {self.side}"

    @property
    def cost_multiple(self) -> float:
        return self.range_points / self.cost_points if self.cost_points > 0 else 0.0

    def _price(self, value: float) -> str:
        return f"{value:.{self.digits}f}"

    def format(self) -> str:
        """The alert, written to be read on a phone in five seconds."""
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
        lines += [
            "",
            f"{self.at:%H:%M} UTC  ·  {self.session_label} open "
            f"{self.session_open:%H:%M}  ·  flat by {self.flat_by:%H:%M}",
        ]
        return "\n".join(lines)

    def one_line(self) -> str:
        return (f"{self.at:%H:%M} {self.kind.upper():6s} {self.symbol} {self.side} "
                f"@ {self._price(self.entry)} stop {self._price(self.stop)} "
                f"target {self._price(self.target)} ({self.range_points:.0f}pt range)")


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

    def validate(self) -> list[str]:
        out = list(self.orb.validate())
        if not self.symbols:
            out.append("no symbols configured, so there is nothing to watch")
        if self.poll_seconds <= 0:
            out.append("poll_seconds must be positive")
        return out


ARMED = "armed"           # range marked, waiting for a break
BROKEN = "broken"         # break alerted, waiting for the retest
RETESTED = "retested"     # both alerts sent; done for the day
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

    @property
    def waiting_for_retest(self) -> bool:
        return self.state == BROKEN

    @property
    def done(self) -> bool:
        return self.state in (RETESTED, FAILED, SKIPPED)


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
        self.notes: list[str] = []
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
        return orb.as_utc_index(self.clock.frame_to_utc(raw)).sort_index()

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
        self.signals += found
        return found

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
                if watch.done:
                    break
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
        return self._signal(
            RETEST, watch, rng, direction, ts, entry=level, stop=stop,
            target=target, spec=spec, day=day, open_at=open_at, digits=digits,
            reason=(f"came back to the {level:.{digits}f} level and held it "
                    f"(broke at {watch.broke_at:%H:%M} UTC)"),
            cfg=cfg)

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
        )

    # ----------------------------------------------------------------- loop
    def run(self, on_signal: Callable[[Signal], None],
            cycles: Optional[int] = None) -> None:      # pragma: no cover
        done = 0
        while cycles is None or done < cycles:
            for signal in self.cycle():
                on_signal(signal)
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

        bot.run(emit, cycles=1 if args.once else None)
    finally:
        try:
            source.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(main())
