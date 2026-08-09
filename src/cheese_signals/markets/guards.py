"""Hard limits that sit above the strategy and can overrule it.

The strategy decides what is a good trade. These decide whether *any* trade
is allowed right now. That separation matters because the failure mode being
guarded against is a strategy that is confidently, systematically wrong --
which is exactly what a strategy looks like from the inside when the market
regime it was fitted to has ended.

Every guard is a circuit breaker rather than a filter: it stops trading and
says why, and it stays stopped until a stated condition clears. None of them
can be overridden by a signal, however strong.

The daily loss limit is the one that matters most. It is the difference
between a bad day and a bad month, and no amount of strategy quality
substitutes for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional


@dataclass
class GuardConfig:
    max_daily_loss_fraction: float = 0.03     # stop for the day at -3% of start equity
    max_open_positions: int = 3
    max_trades_per_day: int = 10
    consecutive_losses_halt: int = 4
    min_equity: float = 0.0                   # absolute floor; 0 disables
    flat_before_close_minutes: int = 10
    news_blackout_minutes: int = 15           # either side of a high-impact release
    trading_hours_utc: Optional[dict[str, tuple[int, int]]] = None
    weekend_flat: bool = True


@dataclass
class DayState:
    """Everything reset at the start of each trading day."""

    day: date
    start_equity: float
    trades: int = 0
    consecutive_losses: int = 0
    halted: bool = False
    halt_reason: str = ""


@dataclass
class Decision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class Guards:
    """The gate every trade passes through, and the day's memory."""

    def __init__(self, config: Optional[GuardConfig] = None):
        self.config = config or GuardConfig()
        self.state: Optional[DayState] = None

    # ------------------------------------------------------------ day roll
    def start_day(self, now: datetime, equity: float) -> DayState:
        self.state = DayState(day=now.date(), start_equity=equity)
        return self.state

    def _ensure_day(self, now: datetime, equity: float) -> DayState:
        if self.state is None or self.state.day != now.date():
            return self.start_day(now, equity)
        return self.state

    # ------------------------------------------------------------- outcome
    def record_close(self, profit: float) -> None:
        """Feed a closed trade back in, so the breakers can see the damage."""
        if self.state is None:
            return
        if profit < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

    def record_open(self) -> None:
        if self.state is not None:
            self.state.trades += 1

    def halt(self, reason: str) -> None:
        if self.state is not None:
            self.state.halted = True
            self.state.halt_reason = reason

    # -------------------------------------------------------------- checks
    def may_open(
        self,
        now: datetime,
        equity: float,
        symbol: str,
        open_positions: int,
        news_windows: Optional[list[tuple[datetime, datetime, str]]] = None,
    ) -> Decision:
        """One decision, with the reason attached whichever way it goes."""
        st = self._ensure_day(now, equity)

        if st.halted:
            return Decision(False, f"halted for the day: {st.halt_reason}")

        cfg = self.config
        if cfg.min_equity > 0 and equity <= cfg.min_equity:
            self.halt(f"equity {equity:.2f} reached the floor of {cfg.min_equity:.2f}")
            return Decision(False, st.halt_reason)

        drawdown = (st.start_equity - equity) / st.start_equity if st.start_equity else 0.0
        if drawdown >= cfg.max_daily_loss_fraction:
            self.halt(f"down {drawdown:.2%} today, limit is "
                      f"{cfg.max_daily_loss_fraction:.2%}")
            return Decision(False, st.halt_reason)

        if st.consecutive_losses >= cfg.consecutive_losses_halt:
            self.halt(f"{st.consecutive_losses} losses in a row")
            return Decision(False, st.halt_reason)

        if st.trades >= cfg.max_trades_per_day:
            return Decision(False, f"{st.trades} trades already today, "
                                   f"limit is {cfg.max_trades_per_day}")

        if open_positions >= cfg.max_open_positions:
            return Decision(False, f"{open_positions} positions open, "
                                   f"limit is {cfg.max_open_positions}")

        if cfg.weekend_flat and now.weekday() >= 5:
            return Decision(False, "weekend: CFDs gap over the close and swap "
                                   "is charged to hold through it")

        hours = (cfg.trading_hours_utc or {}).get(symbol)
        if hours is not None:
            lo, hi = hours
            h = now.hour
            inside = (lo <= h < hi) if lo <= hi else (h >= lo or h < hi)
            if not inside:
                return Decision(False, f"{symbol} trades {lo:02d}:00-{hi:02d}:00 UTC, "
                                       f"it is {h:02d}:{now.minute:02d}")

        if self.flat_deadline_passed(now, symbol):
            return Decision(False, "too close to the session close to open a new trade")

        blocking = self.news_block(now, news_windows or [])
        if blocking:
            return Decision(False, blocking)

        return Decision(True)

    # ---------------------------------------------------------------- news
    def news_block(
        self, now: datetime, windows: list[tuple[datetime, datetime, str]]
    ) -> str:
        """Whether a scheduled release makes this moment untradeable.

        Not a view on direction. Around a high-impact release the spread
        widens, slippage jumps and stops fill far from where they were
        placed. A strategy tested on ordinary conditions was not tested on
        those, so it does not run in them.
        """
        pad = timedelta(minutes=self.config.news_blackout_minutes)
        for start, end, label in windows:
            if start - pad <= now <= end + pad:
                return (f"blackout: {label} at {start:%H:%M} UTC "
                        f"(+/-{self.config.news_blackout_minutes}min)")
        return ""

    # ------------------------------------------------------- session close
    def flat_deadline_passed(self, now: datetime, symbol: str) -> bool:
        hours = (self.config.trading_hours_utc or {}).get(symbol)
        if hours is None:
            return False
        _, close_hour = hours
        close_at = now.replace(hour=close_hour % 24, minute=0, second=0, microsecond=0)
        if close_hour >= 24 or close_at < now.replace(hour=0, minute=0, second=0,
                                                      microsecond=0):
            return False
        return now >= close_at - timedelta(minutes=self.config.flat_before_close_minutes)

    def must_flatten(
        self, now: datetime, symbol: str, equity: float
    ) -> Decision:
        """Whether an *existing* position has to be closed now.

        Distinct from ``may_open``: a bot that stops opening but keeps
        holding through the close, a blackout or its own loss limit has not
        actually protected anything.
        """
        st = self._ensure_day(now, equity)
        if st.halted:
            return Decision(True, f"halted: {st.halt_reason}")
        if self.config.weekend_flat and now.weekday() >= 5:
            return Decision(True, "weekend")
        if self.flat_deadline_passed(now, symbol):
            return Decision(True, "session close")
        return Decision(False)

    # -------------------------------------------------------------- status
    def summary(self, equity: float) -> str:
        if self.state is None:
            return "no trading day started"
        st = self.state
        pnl = equity - st.start_equity
        pct = pnl / st.start_equity if st.start_equity else 0.0
        bits = [f"{st.trades} trades", f"{pnl:+.2f} ({pct:+.2%})"]
        if st.consecutive_losses:
            bits.append(f"{st.consecutive_losses} losses in a row")
        if st.halted:
            bits.append(f"HALTED - {st.halt_reason}")
        return "; ".join(bits)
