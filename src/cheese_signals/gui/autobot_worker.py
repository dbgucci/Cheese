"""The background thread that owns the broker connection.

Why a command queue rather than a thread per job
------------------------------------------------
The MetaTrader 5 Python package is a thin wrapper over a process-global
connection to a running terminal: ``initialize`` and ``shutdown`` affect the
whole process, and there is no per-connection handle to hand to a second
thread. Calling into it from two threads at once is therefore not a
performance question but a correctness one -- the results can interleave.

So exactly one thread ever touches the broker, and the UI asks it for things
by posting commands. That also solves the problem the UI actually has: every
broker call blocks, some of them for minutes (a 180-day M1 history pull), and
a blocked Qt main thread is a frozen window that Windows offers to kill.

Results come back as Qt signals. Emitting a signal from a non-GUI thread is
safe and is queued onto the main thread by Qt, which is the one piece of
cross-thread machinery in here.
"""

from __future__ import annotations

import queue
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from PySide6.QtCore import QObject, Signal

from ..markets import orb
from ..markets.autobot import Autobot, CycleEvent
from ..markets.clock import session_for
from ..markets.launcher import LauncherSettings, build_config, connect

# Command names. Strings rather than an enum because they cross a queue and
# are read in logs.
CONNECT = "connect"
START = "start"
STOP = "stop"
BACKTEST = "backtest"
SURVEY = "survey"
QUIT = "quit"


@dataclass
class SessionRow:
    """One instrument's state today, as the dashboard shows it."""

    symbol: str
    session: str
    range_window: str
    flat_by: str
    width_points: Optional[float] = None
    status: str = "waiting for the open"
    detail: str = ""


@dataclass
class Snapshot:
    """Everything the dashboard needs, gathered on the worker thread.

    Assembled here rather than by the UI pulling values, because every field
    is a broker call and doing them one at a time from paint code is how a
    window ends up unresponsive.
    """

    balance: float = 0.0
    equity: float = 0.0
    currency: str = "USD"
    day_pnl: float = 0.0
    day_pnl_fraction: float = 0.0
    trades_today: int = 0
    open_positions: list[dict] = field(default_factory=list)
    sessions: list[SessionRow] = field(default_factory=list)
    guard_summary: str = ""
    halted: bool = False


class BotWorker(QObject):
    """Owns the broker, the bot, and the only thread allowed to touch either."""

    connected = Signal(dict)          # the terminal diagnostics
    connect_failed = Signal(str, list)  # error, help lines
    symbols_resolved = Signal(list, list)   # available, missing
    snapshot = Signal(object)         # Snapshot
    event = Signal(object)            # CycleEvent
    running_changed = Signal(bool, bool)    # running, live
    backtest_done = Signal(object, str)     # results dict, formatted text
    survey_done = Signal(str)
    busy = Signal(str)                # a long call started; "" when it ends
    failed = Signal(str)

    def __init__(self, settings: LauncherSettings):
        super().__init__()
        self.settings = settings
        self._commands: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._broker: Any = None
        self._bot: Optional[Autobot] = None
        self._running = False
        self._live = False
        self._symbols: list[str] = []
        self._stop_flag = threading.Event()

    # ------------------------------------------------------------ lifecycle
    def start_thread(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="autobot",
                                        daemon=True)
        self._thread.start()

    def post(self, command: str, **kwargs) -> None:
        self._commands.put((command, kwargs))

    def shutdown(self) -> None:
        self._stop_flag.set()
        self.post(QUIT)
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_connected(self) -> bool:
        return self._broker is not None

    # --------------------------------------------------------------- thread
    def _loop(self) -> None:
        while True:
            try:
                command, kwargs = self._commands.get(timeout=0.25)
            except queue.Empty:
                # Idle work: keep the dashboard current even when stopped, so a
                # connected-but-not-trading bot still shows a live account.
                if self._broker is not None:
                    self._tick()
                continue

            if command == QUIT:
                self._close_broker()
                return
            try:
                self._dispatch(command, kwargs)
            except Exception as exc:                      # pragma: no cover
                # A worker thread that dies takes the whole app's broker access
                # with it and reports nothing, so every command is wrapped.
                self.failed.emit(f"{command}: {exc}")
                self.busy.emit("")
                traceback.print_exc()

    def _dispatch(self, command: str, kwargs: dict) -> None:
        if command == CONNECT:
            self._do_connect()
        elif command == START:
            self._do_start(live=bool(kwargs.get("live")))
        elif command == STOP:
            self._do_stop()
        elif command == BACKTEST:
            self._do_backtest(days=int(kwargs.get("days") or
                                       self.settings.backtest_days))
        elif command == SURVEY:
            self._do_survey(days=int(kwargs.get("days") or 90))

    # -------------------------------------------------------------- connect
    def _close_broker(self) -> None:
        if self._broker is not None:
            try:
                self._broker.close()
            except Exception:
                pass
        self._broker = None
        self._bot = None
        self._running = False

    def _do_connect(self) -> None:
        from ..markets.launcher import connection_help, find_terminals

        self.busy.emit("Connecting to MetaTrader 5...")
        self._close_broker()
        broker, error = connect(self.settings)
        if broker is None:
            self.busy.emit("")
            self.connect_failed.emit(error, connection_help(error, find_terminals()))
            return

        self._broker = broker
        try:
            diag = broker.diagnostics()
        except Exception as exc:
            self.busy.emit("")
            self.connect_failed.emit(str(exc), connection_help(str(exc), []))
            self._close_broker()
            return

        self.connected.emit(diag)

        from ..markets.survey import resolve

        available, missing = resolve(broker, self.settings.symbols)
        self._symbols = sorted(available.values())
        self.symbols_resolved.emit(self._symbols, missing)
        self.busy.emit("")
        self._tick()

    # ---------------------------------------------------------- start / stop
    def _do_start(self, live: bool) -> None:
        if self._broker is None or not self._symbols:
            self.failed.emit("Connect to a broker first.")
            return
        config = build_config(self.settings, self._symbols, live)
        problems = config.validate()
        if problems:
            self.failed.emit("; ".join(problems))
            return
        self._bot = Autobot(self._broker, config)
        for warning in config.warnings():
            self.event.emit(CycleEvent(datetime.now(timezone.utc), "-",
                                       "warning", warning))
        try:
            self._bot.calibrate()
            self.event.emit(CycleEvent(datetime.now(timezone.utc), "-", "clock",
                                       self._bot.clock.describe()))
        except Exception as exc:
            self.event.emit(CycleEvent(datetime.now(timezone.utc), "-", "warning",
                                       f"could not measure the broker clock ({exc}); "
                                       f"assuming its bar times are UTC"))
        self._live = live
        self._running = True
        self.running_changed.emit(True, live)

    def _do_stop(self) -> None:
        self._running = False
        self.running_changed.emit(False, self._live)

    # ----------------------------------------------------------------- tick
    def _tick(self) -> None:
        """One cycle if running, plus a fresh dashboard snapshot either way."""
        if self._running and self._bot is not None:
            for event in self._bot.cycle():
                self.event.emit(event)
        self.snapshot.emit(self._build_snapshot())

    def _build_snapshot(self) -> Snapshot:
        snap = Snapshot()
        try:
            account = self._broker.account()
        except Exception as exc:
            self.failed.emit(f"cannot read the account: {exc}")
            return snap
        snap.balance, snap.equity = account.balance, account.equity
        snap.currency = account.currency

        bot = self._bot
        if bot is not None and bot.guards.state is not None:
            state = bot.guards.state
            snap.day_pnl = account.equity - state.start_equity
            snap.day_pnl_fraction = (snap.day_pnl / state.start_equity
                                     if state.start_equity else 0.0)
            snap.trades_today = state.trades
            snap.halted = state.halted
            snap.guard_summary = bot.guards.summary(account.equity)

        if bot is not None:
            for p in bot.executor.adopt():
                snap.open_positions.append({
                    "symbol": p.symbol, "direction": p.direction, "lots": p.lots,
                    "entry": p.open_price, "stop": p.stop_loss,
                    "target": p.take_profit, "profit": p.profit,
                    "ticket": p.ticket,
                })

        snap.sessions = self._session_rows()
        return snap

    def _session_rows(self) -> list[SessionRow]:
        """Today's plan and progress per instrument, in real UTC."""
        bot = self._bot
        now = bot.now() if bot is not None else datetime.now(timezone.utc)
        rows: list[SessionRow] = []
        for symbol in self._symbols:
            try:
                spec = session_for(symbol)
            except KeyError:
                rows.append(SessionRow(symbol, "unmapped", "--", "--",
                                       status="not traded",
                                       detail="no session mapped for this symbol"))
                continue
            day = spec.session_date(now)
            minutes = orb.suggest_range_minutes(symbol)
            open_at = spec.open_utc(day)
            end = open_at + timedelta(minutes=minutes)
            row = SessionRow(
                symbol=symbol, session=spec.label,
                range_window=f"{open_at:%H:%M}-{end:%H:%M}",
                flat_by=f"{spec.close_utc(day):%H:%M}",
            )
            if not spec.is_open_weekday(day):
                row.status = "closed"
                row.detail = "weekend"
            elif now < open_at:
                row.status = "before the open"
            elif now < end:
                row.status = "range forming"
            else:
                row.status = "watching"
            if bot is not None:
                rng = bot._ranges.get((symbol, day))
                if rng is not None:
                    row.width_points = rng.width_points
                held = [p for p in bot.executor.adopt() if p.symbol == symbol]
                if held:
                    row.status = "in a trade"
                elif bot._entries.get((symbol, day), 0) >= \
                        bot._cfg_for(symbol).max_trades_per_session:
                    row.status = "done for today"
                # The most recent thing said about this symbol is the most
                # useful explanation of why it is in the state it is in.
                last = bot._last_said.get(symbol)
                if last:
                    row.detail = last
            rows.append(row)
        return rows

    # ------------------------------------------------------------- backtest
    def _do_backtest(self, days: int) -> None:
        from ..markets import orb_backtest as bt
        from ..markets.clock import BarClock, measure_server_offset
        from ..markets.mt5_bridge import M1, fetch_all

        if self._broker is None or not self._symbols:
            self.failed.emit("Connect to a broker first.")
            return

        self.busy.emit(f"Pulling {days} days of 1-minute history...")
        clock = BarClock(measure_server_offset(
            self._broker.server_time(self._symbols[0])))
        end = datetime.now(timezone.utc)
        frames, problems = fetch_all(
            self._broker, self._symbols, clock.to_server(end - timedelta(days=days)),
            clock.to_server(end), M1)
        if not frames:
            self.busy.emit("")
            self.failed.emit("No history came back, so there is nothing to test. "
                             + ("; ".join(problems) if problems else ""))
            return

        self.busy.emit("Walking the strategy forward...")
        frames = {s: clock.frame_to_utc(df) for s, df in frames.items()}
        points = {s: self._broker.spec(s).point for s in frames}
        cfg = orb.OrbConfig(range_minutes=self.settings.range_minutes,
                            target_r=self.settings.target_r,
                            breakeven_at_r=self.settings.breakeven_at_r,
                            max_trades_per_session=self.settings.max_trades_per_session)
        sim = bt.SimConfig(commission_points=self.settings.commission_points)
        results = bt.compare(frames, cfg, sim, points=points)

        equity = self._broker.account().equity
        text = "\n".join([
            clock.describe(), "",
            bt.format_report(results["strategy"], equity=equity,
                             risk_fraction=self.settings.risk_fraction),
            bt.format_comparison(results),
        ] + [f"  ! {p}" for p in problems])
        self.busy.emit("")
        self.backtest_done.emit(results, text)

    # --------------------------------------------------------------- survey
    def _do_survey(self, days: int) -> None:
        from ..markets.survey import format_report, run

        if self._broker is None or not self._symbols:
            self.failed.emit("Connect to a broker first.")
            return
        self.busy.emit(f"Measuring spreads over {days} days...")
        text = format_report(run(self._broker, symbols=self._symbols, days=days))
        self.busy.emit("")
        self.survey_done.emit(text)
