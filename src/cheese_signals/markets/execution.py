"""Placing and managing real orders.

Deciding to buy is a line of code. Everything in this module is the other
part -- the part where a live broker rejects, re-quotes, moves the stop
level, changes the filling mode, or fills a position the bot has forgotten
about. A signal engine that ignores this works perfectly in a backtest and
loses money in ways that have nothing to do with its strategy.

The failure modes handled here, each of which is a real way retail bots
lose accounts:

* **Duplicate positions.** The bot restarts, does not recognise the position
  it opened five minutes ago, and opens another. Positions are tagged with a
  magic number and reconciled on every cycle.
* **Volume that the broker will not accept.** Lots must land on the symbol's
  step, inside its min and max. Rounding up here overshoots the risk budget,
  so rounding is always down.
* **Stops the broker will reject.** Brokers enforce a minimum distance
  between price and any stop. A stop inside that band is rejected, and a bot
  that ignores the rejection ends up holding an unprotected position.
* **Filling modes.** Not every broker accepts every mode, and the rejection
  looks like an unrelated error. The supported mode is read from the symbol.
* **Spread at the moment of entry.** The cost wall says what a strategy can
  afford; this refuses the trade when the live spread exceeds it. Without
  this the bot faithfully executes trades the backtest proved unprofitable.

Sizing is derived from the stop distance, never from a fixed lot size. A
fixed lot means the risk taken varies with volatility, which is the same as
having no risk policy at all.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Protocol

from .mt5_bridge import SymbolSpec

BUY = 1
SELL = -1

# Tags every order this bot places, so its own positions can be told apart
# from anything opened by hand in the same terminal. Closing a position the
# user opened themselves would be an unforgivable bug.
MAGIC = 770_412


@dataclass
class Order:
    """The intent, before the broker has seen it."""

    symbol: str
    direction: int
    lots: float
    stop_loss: float
    take_profit: Optional[float]
    comment: str = ""
    magic: int = MAGIC

    @property
    def side(self) -> str:
        return "BUY" if self.direction == BUY else "SELL"


@dataclass
class Position:
    """A position the broker says exists."""

    ticket: int
    symbol: str
    direction: int
    lots: float
    open_price: float
    stop_loss: float
    take_profit: float
    opened_at: datetime
    profit: float = 0.0
    magic: int = MAGIC
    comment: str = ""


@dataclass
class OrderResult:
    ok: bool
    reason: str = ""
    ticket: Optional[int] = None
    price: Optional[float] = None
    lots: Optional[float] = None
    retcode: Optional[int] = None

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class AccountState:
    balance: float
    equity: float
    margin_free: float
    currency: str = "USD"


class Trader(Protocol):
    """Order placement, on top of the read-only Broker interface."""

    def account(self) -> AccountState: ...
    def positions(self, magic: Optional[int] = None) -> list[Position]: ...
    def tick(self, symbol: str) -> tuple[float, float]: ...      # (bid, ask)
    def send(self, order: Order) -> OrderResult: ...
    def close(self, position: Position, reason: str = "") -> OrderResult: ...
    def modify(self, position: Position, stop_loss: float,
               take_profit: Optional[float] = None) -> OrderResult: ...


# --------------------------------------------------------------------------
# sizing and normalisation
# --------------------------------------------------------------------------
def normalise_volume(lots: float, spec: SymbolSpec) -> float:
    """Round a lot size onto the broker's grid, always downward.

    Rounding up would place a position larger than the risk budget allows.
    Below the minimum the answer is zero, not the minimum: silently trading
    a bigger position than the risk policy permits is how a "1% risk" bot
    ends up risking eight.
    """
    step = spec.volume_step or 0.01
    if lots < spec.volume_min:
        return 0.0
    stepped = math.floor(lots / step + 1e-9) * step
    stepped = min(stepped, spec.volume_max)
    # Re-round to the step's own precision; floating point leaves 0.30000004.
    decimals = max(0, -int(math.floor(math.log10(step))) + 2)
    stepped = round(stepped, decimals)
    return stepped if stepped >= spec.volume_min else 0.0


def size_for_risk(
    equity: float,
    risk_fraction: float,
    entry: float,
    stop: float,
    spec: SymbolSpec,
) -> float:
    """Lots such that the stop being hit costs ``risk_fraction`` of equity.

    This is the only sizing rule in the system. A stop twice as far away
    gets half the position, so the money at risk is constant while the
    market's volatility is not.
    """
    distance_points = abs(entry - stop) / spec.point
    if distance_points <= 0 or equity <= 0 or risk_fraction <= 0:
        return 0.0
    money_at_risk = equity * risk_fraction
    per_point_per_lot = spec.money_per_point(1.0)
    if per_point_per_lot <= 0:
        return 0.0
    raw = money_at_risk / (distance_points * per_point_per_lot)
    return normalise_volume(raw, spec)


def enforce_stop_distance(
    direction: int, price: float, stop: float, spec: SymbolSpec
) -> float:
    """Push a stop out to the broker's minimum distance if it is too close.

    Widening rather than rejecting is deliberate: the alternative is a
    position that opens with no stop at all because the protective order was
    refused. A slightly wider stop is a smaller position, since sizing is
    derived from the distance -- the risk budget is preserved either way.
    """
    if spec.stops_level <= 0:
        return stop
    minimum = spec.stops_level * spec.point
    if direction == BUY:
        return min(stop, price - minimum)
    return max(stop, price + minimum)


def spread_is_acceptable(
    bid: float, ask: float, spec: SymbolSpec, max_spread_points: Optional[float]
) -> tuple[bool, str]:
    """The gate that connects the cost wall to live trading.

    A strategy is validated against an assumed cost. When the live spread is
    three times that -- around a release, or in a thin session -- the trade
    on offer is not the trade that was tested. Refusing is the whole point of
    having measured the wall in the first place.
    """
    if max_spread_points is None:
        return True, ""
    points = (ask - bid) / spec.point
    if points > max_spread_points:
        return False, (f"spread {points:.1f}pt exceeds the "
                       f"{max_spread_points:.1f}pt this strategy was tested at")
    return True, ""


# --------------------------------------------------------------------------
# the executor
# --------------------------------------------------------------------------
@dataclass
class ExecutionConfig:
    risk_fraction: float = 0.005          # 0.5% of equity per trade
    max_spread_points: Optional[dict[str, float]] = None
    deviation_points: int = 20            # slippage tolerance on market orders
    one_position_per_symbol: bool = True
    dry_run: bool = True                  # nothing reaches the broker until off


class Executor:
    """Turns a decided trade into a broker position, or explains why not.

    Every refusal returns a reason rather than raising, because the reasons
    are the diagnostic record. "No trades today" with no explanation is the
    single most common way a bot is discovered to have been broken for a
    week.
    """

    def __init__(self, trader: Trader, config: Optional[ExecutionConfig] = None,
                 specs: Optional[dict[str, SymbolSpec]] = None):
        self.trader = trader
        self.config = config or ExecutionConfig()
        self._specs = specs or {}

    def spec(self, symbol: str) -> SymbolSpec:
        if symbol not in self._specs:
            self._specs[symbol] = self.trader.spec(symbol)   # type: ignore[attr-defined]
        return self._specs[symbol]

    # ---------------------------------------------------------------- open
    def open(
        self,
        symbol: str,
        direction: int,
        stop_loss: float,
        take_profit: Optional[float] = None,
        comment: str = "",
    ) -> OrderResult:
        spec = self.spec(symbol)
        if not spec.trade_allowed:
            return OrderResult(False, f"{symbol} is not tradeable on this account")

        if self.config.one_position_per_symbol:
            held = [p for p in self.trader.positions(MAGIC) if p.symbol == symbol]
            if held:
                return OrderResult(
                    False, f"already holding {symbol} (ticket {held[0].ticket})")

        bid, ask = self.trader.tick(symbol)
        limits = self.config.max_spread_points or {}
        ok, why = spread_is_acceptable(bid, ask, spec, limits.get(symbol))
        if not ok:
            return OrderResult(False, why)

        price = ask if direction == BUY else bid
        # Checked against the *requested* stop, before any distance
        # enforcement. Enforcement clamps a stop to the protective side, so
        # running it first would silently convert a strategy that asked to
        # buy with its stop above the entry into a plausible-looking trade
        # rather than surfacing the bug that produced it.
        if (direction == BUY and stop_loss >= price) or (
            direction == SELL and stop_loss <= price
        ):
            return OrderResult(False, "the stop is on the wrong side of the entry")
        stop = enforce_stop_distance(direction, price, stop_loss, spec)

        account = self.trader.account()
        lots = size_for_risk(account.equity, self.config.risk_fraction,
                             price, stop, spec)
        if lots <= 0:
            return OrderResult(
                False,
                f"risk budget of {self.config.risk_fraction:.2%} on "
                f"{account.equity:.2f} is smaller than the {spec.volume_min} lot "
                f"minimum at a {abs(price - stop) / spec.point:.0f}pt stop")

        if take_profit is not None:
            take_profit = enforce_stop_distance(-direction, price, take_profit, spec)

        order = Order(symbol=symbol, direction=direction, lots=lots,
                      stop_loss=stop, take_profit=take_profit, comment=comment[:31])
        if self.config.dry_run:
            return OrderResult(True, "dry run: not sent to the broker",
                               price=price, lots=lots)
        return self.trader.send(order)

    # --------------------------------------------------------------- close
    def close_all(self, reason: str, symbol: Optional[str] = None) -> list[OrderResult]:
        out = []
        for p in self.trader.positions(MAGIC):
            if symbol and p.symbol != symbol:
                continue
            if self.config.dry_run:
                out.append(OrderResult(True, f"dry run: would close {p.ticket}"))
                continue
            out.append(self.trader.close(p, reason))
        return out

    # ------------------------------------------------------------ trailing
    def trail(self, position: Position, new_stop: float) -> OrderResult:
        """Move a stop, but only ever in the protective direction.

        A trailing rule with a bug that loosens the stop turns a bounded loss
        into an unbounded one, so the direction is enforced here rather than
        trusted to the caller.
        """
        spec = self.spec(position.symbol)
        bid, ask = self.trader.tick(position.symbol)
        price = bid if position.direction == BUY else ask
        new_stop = enforce_stop_distance(position.direction, price, new_stop, spec)

        if position.direction == BUY and new_stop <= position.stop_loss:
            return OrderResult(False, "a trailing stop may not move down")
        if position.direction == SELL and new_stop >= position.stop_loss:
            return OrderResult(False, "a trailing stop may not move up")
        if self.config.dry_run:
            return OrderResult(True, f"dry run: would trail to {new_stop:.5f}")
        return self.trader.modify(position, new_stop, position.take_profit or None)

    # ------------------------------------------------------- reconciliation
    def adopt(self) -> list[Position]:
        """What the broker says this bot is holding, right now.

        Asked fresh every cycle rather than tracked in memory. In-memory
        state and broker state diverge the moment a stop is hit, a position
        is closed by hand, or the process restarts -- and the broker is the
        one that is right.
        """
        return [p for p in self.trader.positions(MAGIC)]
