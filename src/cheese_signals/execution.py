"""Order execution: off, paper, or live.

Three modes, and the default is **off**. An app that starts placing real
trades because a setting drifted is a bug with a bank balance attached, so
turning execution on is always an explicit act.

``paper``  Simulates fills against the real broker feed and keeps its own
           balance. This is how you find out whether a strategy is worth
           trading: identical signals, identical prices, no money at risk.
``live``   Places real orders through the Pocket Option client. The broker's
           own result is authoritative -- a locally computed win/loss is a
           reasonable estimate, but only the broker knows what actually
           filled and at what price.

Every order passes :class:`SafetyGate` first. The gate is deliberately
paranoid and fails closed: if it cannot confirm a rule is satisfied, the
trade does not happen.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional, Protocol

MODE_OFF = "off"
MODE_PAPER = "paper"
MODE_LIVE = "live"
MODES = (MODE_OFF, MODE_PAPER, MODE_LIVE)


@dataclass
class Order:
    """A placed trade, whether simulated or real."""

    asset: str
    direction: int
    stake: float
    expiry_seconds: int
    placed_at: datetime
    mode: str
    broker_id: Optional[str] = None
    entry_price: Optional[float] = None
    note: str = ""


@dataclass
class Fill:
    """The settled result of an order."""

    order: Order
    won: bool
    payout: float
    profit: float
    exit_price: Optional[float] = None
    source: str = "local"      # "broker" when the broker reported it


@dataclass
class SafetyConfig:
    """Hard limits applied to every order. Tighten freely; loosening is on you."""

    max_stake: float = 50.0
    max_concurrent: int = 3
    max_trades_per_hour: int = 10
    max_daily_loss: float = 100.0
    min_balance: float = 50.0
    # Live trading additionally requires this to be True, set by an explicit
    # confirmation in the UI. A mode string alone is not enough to risk money.
    live_confirmed: bool = False


@dataclass
class SafetyState:
    open_trades: int = 0
    day: Optional[date] = None
    realised_today: float = 0.0
    hour_start: Optional[datetime] = None
    trades_this_hour: int = 0
    halted: bool = False
    halt_reason: str = ""


class SafetyGate:
    """Decides whether an order may be placed. Fails closed."""

    def __init__(self, config: SafetyConfig):
        self.config = config
        self.state = SafetyState()
        self._lock = threading.Lock()

    def halt(self, reason: str) -> None:
        """Kill switch. Nothing further is placed until explicitly resumed."""
        with self._lock:
            self.state.halted = True
            self.state.halt_reason = reason

    def resume(self) -> None:
        with self._lock:
            self.state.halted = False
            self.state.halt_reason = ""

    def check(
        self, mode: str, stake: float, balance: float, now: Optional[datetime] = None
    ) -> tuple[bool, str]:
        now = now or datetime.now(timezone.utc)
        cfg = self.config
        with self._lock:
            st = self.state

            if mode == MODE_OFF:
                return False, "execution is off"
            if mode not in MODES:
                return False, f"unknown execution mode {mode!r}"
            if mode == MODE_LIVE and not cfg.live_confirmed:
                return False, "live trading has not been confirmed"

            if st.halted:
                return False, f"halted: {st.halt_reason}"

            # Roll the day and the hour before testing their limits.
            today = now.date()
            if st.day != today:
                st.day = today
                st.realised_today = 0.0
            if st.hour_start is None or (now - st.hour_start).total_seconds() >= 3600:
                st.hour_start = now
                st.trades_this_hour = 0

            if stake <= 0:
                return False, "stake must be positive"
            if stake > cfg.max_stake:
                return False, f"stake {stake:.2f} exceeds the {cfg.max_stake:.2f} cap"
            if balance < cfg.min_balance:
                return False, f"balance {balance:.2f} below the {cfg.min_balance:.2f} floor"
            if stake > balance:
                return False, "stake exceeds the available balance"
            if st.open_trades >= cfg.max_concurrent:
                return False, f"{st.open_trades} trades already open (max {cfg.max_concurrent})"
            if st.trades_this_hour >= cfg.max_trades_per_hour:
                return False, f"hourly cap reached ({cfg.max_trades_per_hour})"
            if -st.realised_today >= cfg.max_daily_loss:
                return False, (
                    f"daily loss limit hit ({-st.realised_today:.2f} of "
                    f"{cfg.max_daily_loss:.2f})"
                )
            return True, "ok"

    def register_open(self) -> None:
        with self._lock:
            self.state.open_trades += 1
            self.state.trades_this_hour += 1

    def register_close(self, profit: float) -> None:
        with self._lock:
            self.state.open_trades = max(0, self.state.open_trades - 1)
            self.state.realised_today += profit
            if -self.state.realised_today >= self.config.max_daily_loss:
                self.state.halted = True
                self.state.halt_reason = "daily loss limit reached"


class Executor(Protocol):
    mode: str

    def balance(self) -> float: ...
    def place(self, asset: str, direction: int, stake: float, expiry_seconds: int) -> Order: ...
    def settle(self, order: Order, exit_price: Optional[float], won_locally: bool,
               payout: float) -> Fill: ...


class PaperExecutor:
    """Simulated fills against the real feed, with its own balance.

    Deliberately does *not* model slippage or rejected orders, which means it
    is mildly optimistic. It is here to measure whether the strategy has an
    edge, not to promise what a live account would return.
    """

    mode = MODE_PAPER

    def __init__(self, starting_balance: float = 500.0):
        self._balance = starting_balance
        self.starting_balance = starting_balance

    def balance(self) -> float:
        return self._balance

    def place(self, asset: str, direction: int, stake: float, expiry_seconds: int) -> Order:
        self._balance -= stake
        return Order(
            asset=asset, direction=direction, stake=stake,
            expiry_seconds=expiry_seconds, placed_at=datetime.now(timezone.utc),
            mode=self.mode, note="paper",
        )

    def settle(self, order, exit_price, won_locally, payout) -> Fill:
        if won_locally:
            self._balance += order.stake * (1 + payout)
            profit = order.stake * payout
        else:
            profit = -order.stake
        return Fill(order=order, won=won_locally, payout=payout, profit=profit,
                    exit_price=exit_price, source="paper")


class LiveExecutor:
    """Places real orders through the Pocket Option client.

    The broker's own result is preferred over the locally computed one
    wherever available: our version is inferred from candle closes, while the
    broker knows the actual fill.
    """

    mode = MODE_LIVE

    def __init__(self, client, fallback_balance: float = 0.0):
        self._client = client
        self._fallback_balance = fallback_balance

    def balance(self) -> float:
        try:
            return float(self._client.balance())
        except Exception:
            return self._fallback_balance

    def place(self, asset: str, direction: int, stake: float, expiry_seconds: int) -> Order:
        fn = self._client.buy if direction == 1 else self._client.sell
        trade_id, details = fn(asset, stake, expiry_seconds, False)
        entry = None
        if isinstance(details, dict):
            for key in ("openPrice", "open_price", "price"):
                if key in details:
                    try:
                        entry = float(details[key])
                    except (TypeError, ValueError):
                        pass
                    break
        return Order(
            asset=asset, direction=direction, stake=stake,
            expiry_seconds=expiry_seconds, placed_at=datetime.now(timezone.utc),
            mode=self.mode, broker_id=str(trade_id), entry_price=entry,
            note="live",
        )

    def settle(self, order, exit_price, won_locally, payout) -> Fill:
        won, profit, source = won_locally, None, "local"
        if order.broker_id:
            try:
                res = self._client.check_win(order.broker_id)
                if isinstance(res, dict):
                    if "profit" in res:
                        profit = float(res["profit"])
                        won = profit > 0
                        source = "broker"
                    elif "result" in res:
                        won = str(res["result"]).lower() in ("win", "won", "true")
                        source = "broker"
            except Exception:
                pass  # fall back to the locally inferred result
        if profit is None:
            profit = order.stake * payout if won else -order.stake
        return Fill(order=order, won=won, payout=payout, profit=profit,
                    exit_price=exit_price, source=source)


def build_executor(mode: str, settings, client=None):
    """Create the executor for a mode, or None when execution is off."""
    if mode == MODE_PAPER:
        return PaperExecutor(starting_balance=settings.account_balance)
    if mode == MODE_LIVE:
        if client is None:
            raise ValueError(
                "Live trading needs a connected Pocket Option client. "
                "Set Data source to 'pocket_option' with a valid SSID."
            )
        return LiveExecutor(client, fallback_balance=settings.account_balance)
    return None
