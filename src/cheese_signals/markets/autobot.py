"""The autobot: one cycle of the opening-range strategy against a live account.

Where the pieces meet
---------------------
Everything here is wiring, and it is deliberately thin. The rules live in
``orb``, the money limits in ``guards``, the order mechanics in ``execution``,
the clock correction in ``clock``, and the question of whether an instrument is
worth trading at all in ``costs``. This module's only job is to call them in
the right order, and to make sure the *same* ``orb.breakout`` that produced the
backtest is the one deciding the live trade -- if the live path re-implements
the rules, the backtest measured a different strategy and nobody finds out
until the money is gone.

The order of operations in a cycle is not arbitrary
---------------------------------------------------
1. **Reconcile with the broker first.** What is open is whatever the broker
   says is open, asked fresh, never remembered. A position closed by a stop,
   by hand, or by the last process to run is a position this one must not
   believe in.
2. **Manage what exists before looking for anything new.** A bot that opens a
   trade in the same cycle it should have flattened one has its priorities
   backwards, and the flatten is the part that protects the account.
3. **Then, and only then, look for an entry.**

Time is taken from the broker's clock, corrected to real UTC once at the top
of the cycle, because a session strategy that trusts the local machine's clock
is one misconfigured VPS away from trading the wrong hour.

Nothing reaches the broker while ``ExecutionConfig.dry_run`` is set, and it is
set by default. ``--live`` is the only way to turn it off and it requires
typing a confirmation, because the two most expensive keystrokes in retail
algo trading are the ones that connect an untested bot to a funded account.
"""

from __future__ import annotations

import argparse
import time as time_mod
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

import pandas as pd

from . import orb
from .clock import BarClock, SessionSpec, measure_server_offset, session_for
from .execution import BUY, ExecutionConfig, Executor, Position
from .guards import Decision, GuardConfig, Guards
from .mt5_bridge import D1, M1, SymbolSpec

# How much M1 history a cycle needs: enough to cover the session so far plus
# the run-up to the open. The daily-range filter reads D1 bars instead, which
# is a few hundred rows rather than a few hundred thousand.
SESSION_LOOKBACK_HOURS = 26


@dataclass
class AutobotConfig:
    symbols: list[str] = field(default_factory=list)
    orb: orb.OrbConfig = field(default_factory=orb.OrbConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    guards: GuardConfig = field(default_factory=GuardConfig)
    sessions: dict[str, str] = field(default_factory=dict)   # symbol -> session key
    commission_points: dict[str, float] = field(default_factory=dict)
    poll_seconds: int = 20
    per_symbol_range_minutes: bool = True

    def validate(self) -> list[str]:
        """Contradictions that make the bot unable to do what it was told.

        These stop it starting. A configuration that cannot work should fail
        at startup and not at 14:30 on a Tuesday.
        """
        out = list(self.orb.validate())
        if not self.symbols:
            out.append("no symbols configured, so there is nothing to trade")
        if self.poll_seconds <= 0:
            out.append("poll_seconds must be positive")
        return out

    def warnings(self) -> list[str]:
        """Settings that are legal, surprising, and worth saying out loud.

        Distinct from ``validate`` on purpose: refusing to start over a risk
        setting the user may well have meant is its own kind of unhelpful, and
        the answer is to say so plainly and then obey.
        """
        out = []
        if self.guards.trading_hours_utc:
            out.append(
                "guards.trading_hours_utc is set, which second-guesses the session "
                "clock in whole UTC hours and will drift by an hour at every "
                "daylight-saving change -- consider leaving it unset and letting "
                "the session spec decide")
        if not self.execution.dry_run and self.execution.risk_fraction > 0.02:
            out.append(
                f"risking {self.execution.risk_fraction:.1%} of equity per trade on "
                f"a live account: the {self.guards.consecutive_losses_halt}-loss halt "
                f"would trigger about "
                f"{self.guards.consecutive_losses_halt * self.execution.risk_fraction:.0%} "
                f"down")
        if not self.execution.dry_run and self.execution.max_spread_points is None:
            out.append(
                "no max_spread_points configured, so the live spread gate is off and "
                "trades will be taken at whatever the spread happens to be -- run "
                "the cost survey and set a limit per instrument")
        return out


@dataclass
class CycleEvent:
    """One thing the bot did or declined to do, and why.

    The reasons are the product, not a side effect. "It didn't trade today"
    with no explanation is indistinguishable from a bot that has been broken
    for a fortnight, which is the single most common way an algo account
    quietly stops working.
    """

    at: datetime
    symbol: str
    kind: str                 # opened | closed | trailed | skipped | blocked | error
    detail: str

    def line(self) -> str:
        return f"[{self.at:%Y-%m-%d %H:%M:%S}] {self.symbol:10s} {self.kind:8s} {self.detail}"


class Autobot:
    """The opening-range strategy, running against one broker account."""

    def __init__(
        self,
        broker,                       # Broker + Trader: MT5Trader, or a fake
        config: Optional[AutobotConfig] = None,
        clock: Optional[BarClock] = None,
    ):
        self.broker = broker
        self.config = config or AutobotConfig()
        problems = self.config.validate()
        if problems:
            raise ValueError("contradictory autobot config: " + "; ".join(problems))
        self.clock = clock or BarClock()
        self.executor = Executor(broker, self.config.execution)
        self.guards = Guards(self.config.guards)
        self.events: list[CycleEvent] = []
        self._ranges: dict[tuple[str, date], orb.OpeningRange] = {}
        self._entries: dict[tuple[str, date], int] = {}
        self._barred: dict[tuple[str, date], set[int]] = {}
        self._moved: set[int] = set()          # tickets already moved to breakeven
        self._last_said: dict[str, str] = {}   # per symbol, to keep the log readable
        self._watching: dict[int, Position] = {}
        for warning in self.config.warnings():
            self._record(datetime.now(timezone.utc), "-", "warning", warning)

    # ------------------------------------------------------------- plumbing
    def calibrate(self, real_now: Optional[datetime] = None) -> BarClock:
        """Measure the broker's clock offset and keep it for the session.

        Called once at startup rather than per cycle: the offset changes only
        when the broker changes its server timezone, and re-measuring it every
        pass would let one late tick move every session boundary.

        ``real_now`` defaults to this machine's clock, which is the only thing
        the broker's timestamp can be compared *against* -- the offset is by
        definition the disagreement between the two.
        """
        symbol = self.config.symbols[0]
        server_now = self.broker.server_time(symbol)
        self.clock = BarClock(measure_server_offset(server_now, real_now))
        return self.clock

    def now(self) -> datetime:
        """Real UTC, from the broker's clock rather than this machine's."""
        try:
            server_now = self.broker.server_time(self.config.symbols[0])
        except (AttributeError, KeyError, RuntimeError):
            return datetime.now(timezone.utc)
        return self.clock.to_utc(server_now)

    def _spec(self, symbol: str) -> SymbolSpec:
        return self.executor.spec(symbol)

    def _session(self, symbol: str) -> SessionSpec:
        return session_for(symbol, self.config.sessions.get(symbol))

    def _cfg_for(self, symbol: str) -> orb.OrbConfig:
        if not self.config.per_symbol_range_minutes:
            return self.config.orb
        return replace(self.config.orb,
                       range_minutes=orb.suggest_range_minutes(symbol))

    def _record(self, now: datetime, symbol: str, kind: str, detail: str,
                once: bool = False) -> None:
        """Append an event, optionally only if it is not the last thing said.

        A cycle runs every twenty seconds, so a standing condition -- the day's
        loss limit reached, a range too narrow, a weekend -- would otherwise
        print the same line two hundred times an hour and bury the events that
        actually happened.
        """
        if once and self._last_said.get(symbol) == detail:
            return
        self._last_said[symbol] = detail
        event = CycleEvent(now, symbol, kind, detail)
        self.events.append(event)
        # Bounded, like diagnostics.py on the other side of this repo: a bot
        # left running for a month must not accumulate its own trace forever.
        if len(self.events) > 2000:
            del self.events[:1000]

    def _bars(self, symbol: str, now: datetime) -> pd.DataFrame:
        """M1 history covering this session, stamped in real UTC."""
        start = self.clock.to_server(now - timedelta(hours=SESSION_LOOKBACK_HOURS))
        raw = self.broker.history(symbol, M1, start, self.clock.to_server(now))
        return orb.as_utc_index(self.clock.frame_to_utc(raw)).sort_index()

    def _adr_points(self, symbol: str, now: datetime, day: date,
                    cfg: orb.OrbConfig, point: float) -> float:
        """Average daily range from D1 bars -- a few hundred rows, not a million.

        D1 bars span the whole trading day rather than only the session being
        traded, so this reads slightly high for an index. The alternative is
        pulling a month of M1 on every cycle, and a filter that is 20% loose is
        a better trade than a bot that spends its cycle downloading.
        """
        start = self.clock.to_server(now - timedelta(days=cfg.adr_days * 3 + 10))
        try:
            daily = self.broker.history(symbol, D1, start, self.clock.to_server(now))
        except (KeyError, RuntimeError):
            return 0.0
        daily = orb.as_utc_index(self.clock.frame_to_utc(daily)).sort_index()
        if daily.empty:
            return 0.0
        ranges = (daily["high"] - daily["low"]).astype(float)
        # Strictly before today: the day being decided must not be in its own
        # yardstick.
        ranges = ranges[ranges.index.date < day].tail(cfg.adr_days)
        return float(ranges.mean() / point) if len(ranges) and point else 0.0

    # ------------------------------------------------------------ one cycle
    def cycle(self, now: Optional[datetime] = None) -> list[CycleEvent]:
        """One full pass over every configured instrument.

        Returns only the events this cycle produced, so a caller can log or
        alert on them without re-reading the whole trace.
        """
        now = now or self.now()
        before = len(self.events)

        try:
            account = self.broker.account()
        except (RuntimeError, AttributeError) as exc:
            self._record(now, "-", "error", f"cannot read the account: {exc}")
            return self.events[before:]

        positions = self.executor.adopt()
        self._settle(now, positions)
        held = {p.symbol: p for p in positions}

        for symbol in self.config.symbols:
            try:
                self._manage(symbol, held.get(symbol), now, account.equity)
            except (KeyError, RuntimeError, ValueError) as exc:
                self._record(now, symbol, "error", f"managing: {exc}")

        # Re-read: a flatten above changed what is open, and the entry pass
        # below must not size a second position on top of one it just closed.
        held = {p.symbol: p for p in self.executor.adopt()}
        for symbol in self.config.symbols:
            if symbol in held:
                continue
            try:
                self._seek_entry(symbol, now, account.equity, len(held))
            except (KeyError, RuntimeError, ValueError) as exc:
                self._record(now, symbol, "error", f"seeking an entry: {exc}")

        return self.events[before:]

    # --------------------------------------------------------------- settle
    def _settle(self, now: datetime, positions: list[Position]) -> None:
        """Feed positions that have disappeared back into the circuit breakers.

        Without this the consecutive-loss halt and the daily loss limit's trade
        counter never see an outcome, so two of the guards that matter most sit
        permanently at zero -- a bot with the safety rails configured and not
        connected, which is worse than one with none because it reports that it
        has them.

        A position vanishes when its stop or target fills, when it is closed by
        hand, or when this bot closes it. Whichever it was, the broker's realised
        profit is the truth; ``deal_profit`` asks for it, and when the broker
        cannot answer, the last floating profit seen for that ticket stands in.
        That fallback is approximate in size but reliable in sign, and the sign
        is what the breakers switch on.
        """
        live = {p.ticket: p for p in positions}
        for ticket, last_seen in list(self._watching.items()):
            if ticket in live:
                continue
            profit = None
            getter = getattr(self.broker, "deal_profit", None)
            if getter is not None:
                try:
                    profit = getter(ticket)
                except (RuntimeError, KeyError):
                    profit = None
            if profit is None:
                profit = last_seen.profit
            self.guards.record_close(float(profit))
            self._moved.discard(ticket)
            del self._watching[ticket]
            self._record(now, last_seen.symbol, "settled",
                         f"ticket {ticket} closed at {float(profit):+.2f}")
        self._watching.update(live)

    # --------------------------------------------------------------- manage
    def _manage(self, symbol: str, position: Optional[Position],
                now: datetime, equity: float) -> None:
        if position is None:
            return
        spec = self._session(symbol)
        cfg = self._cfg_for(symbol)
        day = spec.session_date(now)

        flat_at = spec.close_utc(day) - timedelta(
            minutes=cfg.flat_before_close_minutes)
        must = self.guards.must_flatten(now, symbol, equity)
        if now >= flat_at:
            # The session deadline is minute-accurate and daylight-saving aware,
            # which the guards' whole-hour window cannot be, so it is decided
            # here rather than by configuring hours into the guards.
            must = Decision(True, f"{spec.label} close at "
                                  f"{spec.close_utc(day):%H:%M} UTC")

        if must:
            for result in self.executor.close_all(must.reason, symbol=symbol):
                self._record(now, symbol,
                             "closed" if result.ok else "error",
                             f"{must.reason}: {result.reason or 'closed'}")
            return

        # Breakeven. The stored range is what the position was opened against;
        # without it the risk distance is unknown and the stop is left alone.
        rng = self._ranges.get((symbol, day))
        if rng is None or cfg.breakeven_at_r is None or position.ticket in self._moved:
            return
        risk = abs(position.open_price - position.stop_loss)
        if risk <= 0:
            return
        trigger = position.open_price + cfg.breakeven_at_r * risk * (
            1 if position.direction == BUY else -1)
        bid, ask = self.broker.tick(symbol)
        price = bid if position.direction == BUY else ask
        reached = price >= trigger if position.direction == BUY else price <= trigger
        if not reached:
            return
        result = self.executor.trail(position, position.open_price)
        if result.ok:
            self._moved.add(position.ticket)
            self._record(now, symbol, "trailed",
                         f"{cfg.breakeven_at_r:g}R ahead: stop moved to entry "
                         f"{position.open_price:.5f}")
        else:
            self._record(now, symbol, "skipped", f"breakeven: {result.reason}")

    # ---------------------------------------------------------------- entry
    def _seek_entry(self, symbol: str, now: datetime, equity: float,
                    open_positions: int) -> None:
        spec = self._session(symbol)
        cfg = self._cfg_for(symbol)
        day = spec.session_date(now)
        key = (symbol, day)

        if not spec.is_open_weekday(day):
            return
        if self._entries.get(key, 0) >= cfg.max_trades_per_session:
            return

        open_at = spec.open_utc(day)
        range_end = open_at + timedelta(minutes=cfg.range_minutes)
        if now < range_end:
            return                      # the range is still forming

        allowed = self.guards.may_open(now, equity, symbol, open_positions)
        if not allowed:
            self._record(now, symbol, "blocked", allowed.reason, once=True)
            return

        point = self._spec(symbol).point
        bars = self._bars(symbol, now)
        if bars.empty:
            self._record(now, symbol, "skipped", "no bars returned for this session",
                         once=True)
            return

        rng = orb.build_range(
            bars, symbol, spec, day, cfg, point,
            commission_points=self.config.commission_points.get(symbol, 0.0),
            fallback_spread_points=self._spec(symbol).spread_current,
        )
        if rng is None:
            self._record(now, symbol, "skipped",
                         f"no bars inside the {open_at:%H:%M}-{range_end:%H:%M} UTC "
                         f"opening range", once=True)
            return
        self._ranges[key] = rng

        if now > orb.entry_deadline(rng, cfg, spec):
            return                      # the window has closed; nothing to say

        adr = self._adr_points(symbol, now, day, cfg, point)
        verdict = orb.check_range(rng, cfg, adr)
        if not verdict:
            self._record(now, symbol, "skipped", verdict.reason)
            self._entries[key] = cfg.max_trades_per_session      # done for today
            return

        # The last *closed* bar. The bar in progress has no close yet, and
        # treating its running price as a close is how a live bot trades
        # signals that never appeared in any backtest.
        closed = bars[bars.index < now.replace(second=0, microsecond=0)]
        closed = closed[closed.index >= rng.end]
        if closed.empty:
            return
        ts, bar = closed.index[-1], closed.iloc[-1]

        plan = orb.breakout(rng, ts, bar, cfg, taken=self._barred.get(key, set()))
        if plan is None:
            return

        ok = orb.check_plan(plan, cfg)
        if not ok:
            self._record(now, symbol, "skipped", ok.reason)
            return

        result = self.executor.open(
            symbol, plan.direction, stop_loss=plan.stop, take_profit=plan.target,
            comment=f"ORB {plan.side} {rng.width_points:.0f}pt")
        if result.ok:
            self._entries[key] = self._entries.get(key, 0) + 1
            self._barred.setdefault(key, set()).update(cfg.barred_after(plan.direction))
            self.guards.record_open()
            self._record(now, symbol, "opened",
                         f"{plan.describe()} [{result.reason or 'sent'}]")
        else:
            self._record(now, symbol, "skipped", f"not opened: {result.reason}")

    # ----------------------------------------------------------------- loop
    def run(self, cycles: Optional[int] = None,
            on_event: Optional[Callable[[CycleEvent], None]] = None) -> None:  # pragma: no cover
        done = 0
        while cycles is None or done < cycles:
            for event in self.cycle():
                if on_event:
                    on_event(event)
                else:
                    print(event.line(), flush=True)
            done += 1
            if cycles is None or done < cycles:
                time_mod.sleep(self.config.poll_seconds)


# --------------------------------------------------------------------------
# the CLI
# --------------------------------------------------------------------------
DEFAULT_SYMBOLS = ["XAUUSD", "XAGUSD", "US30", "SPX500", "NAS100",
                   "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"]


def _resolve(broker, wanted: list[str]) -> tuple[dict[str, str], list[str]]:
    from .survey import resolve

    return resolve(broker, wanted)


def cmd_backtest(args, broker=None) -> int:          # pragma: no cover
    from . import orb_backtest as bt
    from .mt5_bridge import MT5Trader, fetch_all

    broker = broker or MT5Trader(login=args.login, password=args.password,
                                 server=args.server, terminal_path=args.terminal)
    found, missing = _resolve(broker, args.symbols or DEFAULT_SYMBOLS)
    if not found:
        print("None of the requested instruments exist on this account.")
        return 1

    # Measured once, from any instrument that quotes: it is a property of the
    # server, not of the symbol.
    clock = BarClock(measure_server_offset(
        broker.server_time(sorted(found.values())[0])))
    print(clock.describe())

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    frames, problems = fetch_all(broker, list(found.values()),
                                 clock.to_server(start), clock.to_server(end), M1)
    frames = {s: clock.frame_to_utc(df) for s, df in frames.items()}
    points = {s: broker.spec(s).point for s in frames}

    cfg = orb.OrbConfig(range_minutes=args.range_minutes, target_r=args.target_r)
    sim = bt.SimConfig(commission_points=args.commission)
    results = bt.compare(frames, cfg, sim, points=points)
    print()
    print(bt.format_report(results["strategy"], equity=args.equity,
                           risk_fraction=args.risk))
    print(bt.format_comparison(results))
    for p in problems + [f"not offered on this account: {m}" for m in missing]:
        print(f"  ! {p}")
    return 0


def cmd_run(args, broker=None) -> int:               # pragma: no cover
    from .mt5_bridge import MT5Trader

    broker = broker or MT5Trader(login=args.login, password=args.password,
                                 server=args.server, terminal_path=args.terminal)
    found, missing = _resolve(broker, args.symbols or DEFAULT_SYMBOLS)
    for m in missing:
        print(f"  ! not offered on this account: {m}")
    if not found:
        print("None of the requested instruments exist on this account.")
        return 1

    if args.live:
        diag = broker.diagnostics()
        print(f"LIVE trading on {diag['company']} account {diag['login']} "
              f"({diag['currency']} {diag['equity']:.2f}), "
              f"risking {args.risk:.2%} per trade.")
        if input('Type "trade live" to confirm: ').strip() != "trade live":
            print("Not confirmed. Nothing was sent.")
            return 1

    config = AutobotConfig(
        symbols=sorted(found.values()),
        orb=orb.OrbConfig(range_minutes=args.range_minutes, target_r=args.target_r),
        execution=ExecutionConfig(risk_fraction=args.risk, dry_run=not args.live),
        guards=GuardConfig(max_daily_loss_fraction=args.max_daily_loss),
        poll_seconds=args.poll,
    )
    bot = Autobot(broker, config)
    for warning in config.warnings():
        print(f"  ! {warning}")
    print(bot.calibrate().describe())
    print(f"Watching {', '.join(config.symbols)} "
          f"({'LIVE' if args.live else 'dry run'}). Ctrl+C to stop.")
    bot.run()
    return 0


def cmd_check(args, broker=None) -> int:             # pragma: no cover
    from .mt5_bridge import MT5Trader
    from .survey import check

    broker = broker or MT5Trader(login=args.login, password=args.password,
                                 server=args.server, terminal_path=args.terminal)
    print(check(broker, symbols=args.symbols or DEFAULT_SYMBOLS))
    symbols = args.symbols or DEFAULT_SYMBOLS
    found, _ = _resolve(broker, symbols)
    if found:
        first = sorted(found.values())[0]
        clock = BarClock(measure_server_offset(broker.server_time(first)))
        print()
        print(f"BROKER CLOCK\n  {clock.describe()}")
        print("\nSESSION OPENS TODAY (real UTC)")
        today = datetime.now(timezone.utc).date()
        for want, actual in sorted(found.items()):
            try:
                spec = session_for(actual)
            except KeyError as exc:
                print(f"  {want:10s} {exc}")
                continue
            cfg_minutes = orb.suggest_range_minutes(actual)
            open_at = spec.open_utc(today)
            print(f"  {want:10s} {spec.label:20s} open {open_at:%H:%M} UTC, "
                  f"{cfg_minutes}min range, flat by "
                  f"{spec.close_utc(today):%H:%M} UTC")
    return 0


def main(argv: Optional[list[str]] = None) -> int:    # pragma: no cover
    ap = argparse.ArgumentParser(
        prog="python -m cheese_signals.markets.autobot",
        description="Opening-range breakout autobot for FX, metals and index CFDs.")
    ap.add_argument("--login", type=int, default=None)
    ap.add_argument("--password", default="")
    ap.add_argument("--server", default="", help="the broker's MT5 server name")
    ap.add_argument("--terminal", default=None, help="path to terminal64.exe")
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--range-minutes", type=int, default=15,
                    help="opening range length; FX and metals get 30 automatically")
    ap.add_argument("--target-r", type=float, default=2.0)
    ap.add_argument("--risk", type=float, default=0.005,
                    help="fraction of equity risked per trade (0.005 = 0.5%%)")

    sub = ap.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="connection, instruments, and session opens")
    c.set_defaults(func=cmd_check)

    b = sub.add_parser("backtest", help="walk-forward the rules on broker history")
    b.add_argument("--days", type=int, default=180)
    b.add_argument("--commission", type=float, default=0.0,
                   help="round-trip commission in points")
    b.add_argument("--equity", type=float, default=10_000.0)
    b.set_defaults(func=cmd_backtest)

    r = sub.add_parser("run", help="trade the strategy (dry run unless --live)")
    r.add_argument("--live", action="store_true",
                   help="actually send orders; requires typed confirmation")
    r.add_argument("--poll", type=int, default=20, help="seconds between cycles")
    r.add_argument("--max-daily-loss", type=float, default=0.03)
    r.set_defaults(func=cmd_run)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(main())
