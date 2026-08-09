"""The autotrader: one cycle, run on a timer.

Every cycle does the same four things in the same order, and the order is
the design:

1. **Reconcile.** Ask the broker what is open. Never trust memory.
2. **Manage what is open** -- trail stops, and flatten anything the guards
   say must go. Managing before opening means a bot at its position limit
   still protects what it holds.
3. **Decide.** Run the strategy on closed bars only.
4. **Open**, if and only if every guard agrees.

Anything that goes wrong in one symbol is caught and recorded rather than
allowed to end the cycle, because a single unquotable instrument must not
stop the others from being managed.

Every decision -- including every refusal -- is written to the journal. "No
trades today" with no explanation is the most common way a bot turns out to
have been broken for a week.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import pandas as pd

from .execution import BUY, MAGIC, Executor, Position
from .guards import Decision, Guards
from .mt5_bridge import M1
from .strategy import FLAT, Intent, Strategy


@dataclass
class SymbolPlan:
    """One instrument, its strategy, and the cost limit it was validated at."""

    symbol: str
    strategy: Strategy
    max_spread_points: Optional[float] = None
    history_days: int = 25          # enough for a 14-day band plus weekends


@dataclass
class CycleEvent:
    at: datetime
    symbol: str
    kind: str            # opened | closed | trailed | refused | error
    detail: str


class Trader:
    """Ties strategy, guards and execution together. Owns no market state."""

    def __init__(
        self,
        broker,                       # Broker + Trader protocols (MT5Trader)
        plans: list[SymbolPlan],
        executor: Executor,
        guards: Guards,
        on_event: Optional[Callable[[CycleEvent], None]] = None,
        news_windows: Optional[Callable[[], list[tuple[datetime, datetime, str]]]] = None,
    ):
        self.broker = broker
        self.plans = {p.symbol: p for p in plans}
        self.executor = executor
        self.guards = guards
        self.on_event = on_event or (lambda e: None)
        self.news_windows = news_windows or (lambda: [])
        self._seen_tickets: dict[int, float] = {}
        self._trades_today: dict[str, int] = {}
        self._trades_day = None
        self._cycle_events: list[CycleEvent] = []

    # ------------------------------------------------------------------
    def _emit(self, now, symbol, kind, detail):
        event = CycleEvent(now, symbol, kind, detail)
        self._cycle_events.append(event)
        self.on_event(event)

    def _roll_session(self, now: datetime) -> None:
        if self._trades_day != now.date():
            self._trades_day = now.date()
            self._trades_today = {}

    def history(self, symbol: str, now: datetime, days: int) -> pd.DataFrame:
        """Closed bars only.

        The forming bar is excluded because acting on it means acting on a
        price that has not happened yet -- the same lookahead that made the
        Pocket Option backtests disagree with live results.
        """
        df = self.broker.history(symbol, M1, now - timedelta(days=days), now)
        if df.empty:
            return df
        cutoff = pd.Timestamp(now).floor("1min")
        return df[df.index < cutoff]

    # ------------------------------------------------------------------
    def cycle(self, now: Optional[datetime] = None) -> list[CycleEvent]:
        now = now or datetime.now(timezone.utc)
        self._roll_session(now)
        self._cycle_events = []

        try:
            account = self.broker.account()
            open_positions = self.executor.adopt()
            self._note_closures(now, open_positions)
            self._manage(now, open_positions, account)

            open_positions = self.executor.adopt()
            held = {p.symbol for p in open_positions}
            for symbol, plan in self.plans.items():
                if symbol in held:
                    continue
                try:
                    self._consider(now, plan, account, len(open_positions))
                except Exception as exc:          # one bad symbol must not stop the rest
                    self._emit(now, symbol, "error", f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            # A broker that will not answer at all is one event, not a crash:
            # the loop must survive to try again on the next cycle.
            self._emit(now, "", "error", f"cycle aborted: {type(exc).__name__}: {exc}")
        return list(self._cycle_events)

    # ------------------------------------------------------------------
    def _note_closures(self, now: datetime, open_positions: list[Position]) -> None:
        """Feed positions that have gone away back into the guards.

        A position can close without the bot doing it -- a stop fills, or the
        user closes it by hand. The loss breakers only work if those count.
        """
        live = {p.ticket for p in open_positions}
        for ticket, last_profit in list(self._seen_tickets.items()):
            if ticket not in live:
                self.guards.record_close(last_profit)
                self._emit(now, "", "closed",
                           f"ticket {ticket} is gone, last seen at {last_profit:+.2f}")
                del self._seen_tickets[ticket]
        for p in open_positions:
            self._seen_tickets[p.ticket] = p.profit

    # ------------------------------------------------------------------
    def _manage(self, now, positions: list[Position], account) -> None:
        for p in positions:
            plan = self.plans.get(p.symbol)
            flat = self.guards.must_flatten(now, p.symbol, account.equity)
            if flat:
                for r in self.executor.close_all(flat.reason, symbol=p.symbol):
                    self._emit(now, p.symbol, "closed" if r.ok else "error",
                               f"{flat.reason}: {r.reason or 'closed'}")
                continue
            if plan is None or not hasattr(plan.strategy, "trailing_stop"):
                continue
            try:
                df = self.history(p.symbol, now, plan.history_days)
                if df.empty:
                    continue
                stop = plan.strategy.trailing_stop(df, pd.Timestamp(now), p.direction)
            except Exception as exc:
                self._emit(now, p.symbol, "error", f"trailing: {exc}")
                continue
            if stop is None:
                continue
            r = self.executor.trail(p, stop)
            if r.ok:
                self._emit(now, p.symbol, "trailed", f"stop -> {stop:.5f}")

    # ------------------------------------------------------------------
    def _consider(self, now, plan: SymbolPlan, account, open_count: int) -> None:
        allowed = self.guards.may_open(
            now, account.equity, plan.symbol, open_count, self.news_windows())
        if not allowed:
            self._emit(now, plan.symbol, "refused", allowed.reason)
            return

        df = self.history(plan.symbol, now, plan.history_days)
        if df.empty:
            self._emit(now, plan.symbol, "refused", "no history returned")
            return

        intent: Intent = plan.strategy.evaluate(
            df, pd.Timestamp(now), self._trades_today.get(plan.symbol, 0))
        if not intent:
            self._emit(now, plan.symbol, "refused", intent.reason)
            return

        result = self.executor.open(
            plan.symbol, intent.direction, intent.stop_loss,
            comment=f"{getattr(plan.strategy, 'name', 'strategy')}")
        if not result.ok:
            self._emit(now, plan.symbol, "refused", result.reason)
            return

        self.guards.record_open()
        self._trades_today[plan.symbol] = self._trades_today.get(plan.symbol, 0) + 1
        side = "BUY" if intent.direction == BUY else "SELL"
        self._emit(now, plan.symbol, "opened",
                   f"{side} {result.lots} @ {result.price} stop {intent.stop_loss:.5f} "
                   f"-- {intent.reason}")
