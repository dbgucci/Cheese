"""MetaTrader 5 as the source of history, costs and execution.

Why the broker's terminal rather than a data vendor
---------------------------------------------------
A backtest is only as honest as its cost assumptions, and the cost that will
actually be charged is the one this broker quotes on this account. Vendor
data would give cleaner prices and the wrong spreads, which is the more
dangerous of the two errors: it produces a backtest that works and a live
account that does not.

MT5 also carries the per-bar spread in its own history, so several years of
real quoted spreads come back with the candles rather than having to be
recorded going forward.

The Python package is Windows-only, so nothing here imports it at module
scope. ``MT5Feed`` is one implementation of ``Broker``; ``ReplayFeed`` is
another, which is what the tests and the backtester use.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol

import pandas as pd

M1 = "M1"
M5 = "M5"
M15 = "M15"
H1 = "H1"


@dataclass
class SymbolSpec:
    """The contract details that turn a price move into money.

    Getting these from the broker rather than assuming them matters more than
    it sounds: a "point" on US30 is not a point on EURUSD, and a strategy
    sized as though it were will be wrong by three orders of magnitude.
    """

    name: str
    point: float                  # smallest price increment
    digits: int
    contract_size: float
    tick_value: float             # account currency per tick per lot
    volume_min: float
    volume_step: float
    volume_max: float
    spread_current: float         # in points, as the terminal reports it
    tick_size: float = 0.0        # price change per tick_value; often == point
    stops_level: int = 0          # minimum SL/TP distance from price, in points
    freeze_level: int = 0
    swap_long: float = 0.0
    swap_short: float = 0.0
    trade_allowed: bool = True

    def money_per_point(self, lots: float = 1.0) -> float:
        """Account currency gained per point of favourable move, per ``lots``.

        ``tick_value`` is the money per *tick*, and a tick is not always a
        point -- on several index CFDs a tick is ten points. Dividing through
        by the ratio is the difference between sizing a position correctly
        and sizing it ten times too large, which is not an error a live
        account survives twice.
        """
        tick = self.tick_size or self.point
        ticks_per_point = self.point / tick if tick else 1.0
        return self.tick_value * ticks_per_point * lots


class Broker(Protocol):
    """What the rest of this package needs. Deliberately small."""

    def symbols(self) -> list[str]: ...

    def spec(self, symbol: str) -> SymbolSpec: ...

    def history(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> pd.DataFrame: ...


class ReplayFeed:
    """A Broker backed by stored frames. Used by tests and the backtester."""

    def __init__(
        self,
        frames: dict[str, pd.DataFrame],
        specs: Optional[dict[str, SymbolSpec]] = None,
    ):
        self._frames = frames
        self._specs = specs or {}

    def symbols(self) -> list[str]:
        return sorted(self._frames)

    def spec(self, symbol: str) -> SymbolSpec:
        if symbol in self._specs:
            return self._specs[symbol]
        return SymbolSpec(
            name=symbol, point=1.0, digits=2, contract_size=1.0, tick_value=1.0,
            volume_min=0.01, volume_step=0.01, volume_max=100.0, spread_current=0.0,
        )

    def history(self, symbol, timeframe, start, end) -> pd.DataFrame:
        df = self._frames[symbol]
        return df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]


class MT5Feed:
    """The real thing. Windows only, and only while the terminal is running.

    The terminal must be open and logged in: the Python package attaches to a
    running terminal rather than making its own connection, so a headless
    server needs the terminal running under a desktop session.
    """

    _TIMEFRAMES = {M1: "TIMEFRAME_M1", M5: "TIMEFRAME_M5",
                   M15: "TIMEFRAME_M15", H1: "TIMEFRAME_H1"}

    def __init__(self, login: Optional[int] = None, password: str = "",
                 server: str = "", terminal_path: Optional[str] = None):
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:      # pragma: no cover - platform dependent
            raise RuntimeError(
                "The MetaTrader5 package is Windows-only and is not installed. "
                "Install it on the machine running the terminal: pip install MetaTrader5"
            ) from exc
        self._mt5 = mt5

        kwargs = {}
        if terminal_path:
            kwargs["path"] = terminal_path
        if login:
            kwargs.update(login=int(login), password=password, server=server)
        if not mt5.initialize(**kwargs):
            code, msg = mt5.last_error()
            raise RuntimeError(f"MetaTrader 5 did not initialise: {code} {msg}")

    def close(self) -> None:                       # pragma: no cover
        self._mt5.shutdown()

    def diagnostics(self) -> dict:                 # pragma: no cover
        """What the terminal and account actually are, before trusting either.

        Worth checking explicitly because two of the three ways this fails
        are silent: the Python package attaches to whichever terminal is
        running, so with more than one MT5 installed it can connect to a
        different broker than intended; and algo trading disabled in the
        toolbar blocks orders while leaving data working perfectly, which
        looks like a strategy that never fires.
        """
        t = self._mt5.terminal_info()
        a = self._mt5.account_info()
        return {
            "terminal": getattr(t, "name", "?"),
            "terminal_path": getattr(t, "path", "?"),
            "connected": bool(getattr(t, "connected", False)),
            "algo_allowed": bool(getattr(t, "trade_allowed", False)),
            "login": getattr(a, "login", None),
            "server": getattr(a, "server", "?"),
            "company": getattr(a, "company", "?"),
            "currency": getattr(a, "currency", "?"),
            "balance": getattr(a, "balance", 0.0),
            "equity": getattr(a, "equity", 0.0),
            "leverage": getattr(a, "leverage", 0),
            "trade_expert": bool(getattr(a, "trade_expert", False)),
        }

    def quote(self, symbol: str) -> tuple[float, float]:   # pragma: no cover
        if not self._mt5.symbol_select(symbol, True):
            raise KeyError(f"{symbol} is not available on this account")
        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"no live tick for {symbol}")
        return float(tick.bid), float(tick.ask)

    def symbols(self) -> list[str]:                # pragma: no cover
        return sorted(s.name for s in self._mt5.symbols_get())

    def spec(self, symbol: str) -> SymbolSpec:     # pragma: no cover
        if not self._mt5.symbol_select(symbol, True):
            raise KeyError(f"{symbol} is not available on this account")
        i = self._mt5.symbol_info(symbol)
        if i is None:
            raise KeyError(f"no symbol_info for {symbol}")
        return SymbolSpec(
            name=i.name, point=i.point, digits=i.digits,
            contract_size=i.trade_contract_size, tick_value=i.trade_tick_value,
            volume_min=i.volume_min, volume_step=i.volume_step, volume_max=i.volume_max,
            spread_current=float(i.spread), tick_size=i.trade_tick_size,
            stops_level=int(i.trade_stops_level), freeze_level=int(i.trade_freeze_level),
            swap_long=i.swap_long, swap_short=i.swap_short,
            trade_allowed=i.trade_mode != self._mt5.SYMBOL_TRADE_MODE_DISABLED,
        )

    def history(self, symbol, timeframe, start, end) -> pd.DataFrame:  # pragma: no cover
        tf = getattr(self._mt5, self._TIMEFRAMES[timeframe])
        if not self._mt5.symbol_select(symbol, True):
            raise KeyError(f"{symbol} is not available on this account")
        rates = self._mt5.copy_rates_range(symbol, tf, _utc(start), _utc(end))
        if rates is None or len(rates) == 0:
            code, msg = self._mt5.last_error()
            raise RuntimeError(f"no history for {symbol} {timeframe}: {code} {msg}")
        return normalise(pd.DataFrame(rates))


def _utc(ts: datetime) -> datetime:                # pragma: no cover
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def normalise(raw: pd.DataFrame) -> pd.DataFrame:
    """MT5's rate array into the frame shape the rest of the app expects.

    Bars are stamped by **open** time here, matching both MT5 and the
    convention already used throughout this codebase -- the one that caused
    two separate off-by-one-bar bugs on the Pocket Option side when it was
    left implicit. It is stated once, here, and not re-derived anywhere else.
    """
    df = raw.copy()
    if "time" in df:
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time")
    df.index.name = "ts"
    keep = [c for c in ("open", "high", "low", "close", "tick_volume",
                        "spread", "real_volume") if c in df]
    df = df[keep].astype(float)
    return df.sort_index()


def fetch_all(
    broker: Broker,
    symbols: list[str],
    start: datetime,
    end: datetime,
    timeframe: str = M1,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """History for every symbol, with the failures named rather than dropped.

    A symbol silently missing from a study is how an instrument that the
    broker does not actually offer ends up absent from the report without
    anyone noticing it was never tested.
    """
    frames, problems = {}, []
    for s in symbols:
        try:
            df = broker.history(s, timeframe, start, end)
        except (KeyError, RuntimeError) as exc:
            problems.append(f"{s}: {exc}")
            continue
        if df.empty:
            problems.append(f"{s}: no bars returned for the requested range")
            continue
        frames[s] = df
    return frames, problems


class MT5Trader(MT5Feed):
    """MT5Feed plus order placement. Windows only.

    Filling mode is read from the symbol rather than hardcoded: brokers
    support different subsets, and an unsupported mode is rejected with an
    error that looks unrelated to filling.
    """

    def account(self):                                  # pragma: no cover
        from .execution import AccountState
        a = self._mt5.account_info()
        if a is None:
            raise RuntimeError("no account_info; the terminal is not logged in")
        return AccountState(balance=a.balance, equity=a.equity,
                            margin_free=a.margin_free, currency=a.currency)

    def tick(self, symbol: str):                        # pragma: no cover
        return self.quote(symbol)

    def positions(self, magic=None):                    # pragma: no cover
        from datetime import datetime as _dt
        from .execution import Position
        raw = self._mt5.positions_get()
        out = []
        for p in raw or []:
            if magic is not None and p.magic != magic:
                continue
            out.append(Position(
                ticket=p.ticket, symbol=p.symbol,
                direction=1 if p.type == self._mt5.POSITION_TYPE_BUY else -1,
                lots=p.volume, open_price=p.price_open, stop_loss=p.sl,
                take_profit=p.tp,
                opened_at=_dt.fromtimestamp(p.time, tz=timezone.utc),
                profit=p.profit, magic=p.magic, comment=p.comment))
        return out

    def _filling(self, symbol: str) -> int:             # pragma: no cover
        info = self._mt5.symbol_info(symbol)
        modes = getattr(info, "filling_mode", 0)
        # SYMBOL_FILLING_FOK = 1, SYMBOL_FILLING_IOC = 2 as a bitmask.
        if modes & 2:
            return self._mt5.ORDER_FILLING_IOC
        if modes & 1:
            return self._mt5.ORDER_FILLING_FOK
        return self._mt5.ORDER_FILLING_RETURN

    def send(self, order):                              # pragma: no cover
        from .execution import BUY, OrderResult
        bid, ask = self.quote(order.symbol)
        price = ask if order.direction == BUY else bid
        req = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": order.symbol,
            "volume": float(order.lots),
            "type": (self._mt5.ORDER_TYPE_BUY if order.direction == BUY
                     else self._mt5.ORDER_TYPE_SELL),
            "price": price,
            "sl": float(order.stop_loss),
            "deviation": 20,
            "magic": int(order.magic),
            "comment": order.comment[:31],
            "type_time": self._mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(order.symbol),
        }
        if order.take_profit:
            req["tp"] = float(order.take_profit)
        r = self._mt5.order_send(req)
        if r is None:
            code, msg = self._mt5.last_error()
            return OrderResult(False, f"order_send returned nothing: {code} {msg}")
        if r.retcode != self._mt5.TRADE_RETCODE_DONE:
            return OrderResult(False, f"{r.retcode} {r.comment}", retcode=r.retcode)
        return OrderResult(True, ticket=r.order, price=r.price, lots=r.volume,
                           retcode=r.retcode)

    def close(self, position, reason=""):               # pragma: no cover
        from .execution import BUY, OrderResult
        bid, ask = self.quote(position.symbol)
        closing_buy = position.direction != BUY
        req = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": float(position.lots),
            "type": (self._mt5.ORDER_TYPE_BUY if closing_buy
                     else self._mt5.ORDER_TYPE_SELL),
            "position": int(position.ticket),
            "price": ask if closing_buy else bid,
            "deviation": 20,
            "magic": int(position.magic),
            "comment": reason[:31],
            "type_time": self._mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(position.symbol),
        }
        r = self._mt5.order_send(req)
        if r is None or r.retcode != self._mt5.TRADE_RETCODE_DONE:
            code = getattr(r, "retcode", None)
            return OrderResult(False, f"close failed: {code} "
                                      f"{getattr(r, 'comment', self._mt5.last_error())}",
                               retcode=code)
        return OrderResult(True, ticket=position.ticket, price=r.price,
                           retcode=r.retcode)

    def modify(self, position, stop_loss, take_profit=None):   # pragma: no cover
        from .execution import OrderResult
        req = {
            "action": self._mt5.TRADE_ACTION_SLTP,
            "symbol": position.symbol,
            "position": int(position.ticket),
            "sl": float(stop_loss),
            "tp": float(take_profit or position.take_profit or 0.0),
        }
        r = self._mt5.order_send(req)
        if r is None or r.retcode != self._mt5.TRADE_RETCODE_DONE:
            code = getattr(r, "retcode", None)
            return OrderResult(False, f"modify failed: {code}", retcode=code)
        return OrderResult(True, ticket=position.ticket, retcode=r.retcode)
