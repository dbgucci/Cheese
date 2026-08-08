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
    swap_long: float = 0.0
    swap_short: float = 0.0
    trade_allowed: bool = True

    def money_per_point(self, lots: float) -> float:
        return self.tick_value * lots * (self.point / self.point)


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
            spread_current=float(i.spread), swap_long=i.swap_long, swap_short=i.swap_short,
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
