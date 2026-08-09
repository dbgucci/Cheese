"""When the session opens -- in the exchange's time zone, not in UTC.

An opening-range strategy is a bet that the first minutes after a *specific*
market opens carry information. Everything depends on the bot agreeing with
the exchange about when that moment is, and two separate clocks have to be
right for that to happen. Both are wrong by default, and both fail silently:
the bot builds a range over the wrong fifteen minutes and then trades
breakouts of a level that means nothing.

**1. The exchange's clock moves twice a year.** The New York cash open is
09:30 America/New_York all year round, which is 13:30 UTC in summer and 14:30
UTC in winter. A session written down as a UTC hour is therefore wrong for
roughly five months of every year. Worse, the US and Europe change clocks on
different dates, so for two or three weeks each spring and autumn the offset
between New York and London is not its usual five hours -- which breaks even
a bot that was diligent enough to maintain two hardcoded UTC tables. The only
way to be right is to name the exchange's zone and let ``zoneinfo`` do the
arithmetic, which is what ``SessionSpec`` does.

**2. The broker's clock is not UTC either.** MetaTrader stamps bars and ticks
with *server* time, delivered as a Unix timestamp of the server's wall clock
rather than of the actual instant. Most forex brokers run their servers on
EET, which is UTC+2 in winter and UTC+3 in summer. So a frame that
``mt5_bridge.normalise`` has labelled UTC actually reads two or three hours
ahead of it, and an unadjusted bot looking for the 13:30 UTC New York open
finds the bar for 11:30 New York time -- lunchtime, mid-session, no opening
range anywhere near it.

That offset cannot be assumed, because it varies by broker, so
``measure_server_offset`` measures it against a live tick instead. Nothing
here guesses it, and ``BarClock`` makes the correction explicit and visible
in one place rather than leaving it as a subtraction scattered through the
strategy code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd


@dataclass(frozen=True)
class SessionSpec:
    """One market's trading day, defined where the market itself defines it."""

    key: str
    tz: str
    open_time: time
    close_time: time
    label: str

    def _local(self, day: date, at: time) -> datetime:
        return datetime.combine(day, at, tzinfo=ZoneInfo(self.tz))

    def open_utc(self, day: date) -> datetime:
        """The session open on ``day``, as a real UTC instant."""
        return self._local(day, self.open_time).astimezone(timezone.utc)

    def close_utc(self, day: date) -> datetime:
        """The session close. Rolls to the next day for sessions that wrap."""
        end_day = day if self.close_time > self.open_time else day + timedelta(days=1)
        return self._local(end_day, self.close_time).astimezone(timezone.utc)

    def session_date(self, moment_utc: datetime) -> date:
        """Which trading day a UTC instant belongs to.

        The exchange's own calendar date, not UTC's. A trade at 00:30 UTC on
        Wednesday is part of Tuesday's Chicago session, and grouping it under
        Wednesday would split one session's bars across two days.
        """
        return moment_utc.astimezone(ZoneInfo(self.tz)).date()

    def is_open_weekday(self, day: date) -> bool:
        """Monday-Friday. Holidays are not modelled -- see ``holiday`` below."""
        return day.weekday() < 5


# Session presets. Each is the *cash* or floor session of the market that
# actually prices the instrument, because that is where an opening exists.
SESSIONS: dict[str, SessionSpec] = {
    # NYSE/Nasdaq cash hours. The open that US index CFDs gap into and the
    # only moment of the day where "the market opened" is literally true --
    # the CFD itself trades nearly 24h, which is exactly why the range has to
    # be anchored to the cash open rather than to midnight.
    "us_cash": SessionSpec("us_cash", "America/New_York", time(9, 30), time(16, 0),
                           "US cash equities"),
    # London open. The liquidity handover that sets the day's tone for FX and
    # for metals, both of which are London-centric markets.
    "london": SessionSpec("london", "Europe/London", time(8, 0), time(16, 30),
                          "London"),
    # COMEX open-outcry gold/silver session, kept as an alternative anchor for
    # metals because a good part of gold's daily range is still made here.
    "comex": SessionSpec("comex", "America/New_York", time(8, 20), time(13, 30),
                         "COMEX metals floor"),
    # The New York FX session, for USD pairs whose ranges are made in the
    # afternoon rather than the European morning.
    "ny_fx": SessionSpec("ny_fx", "America/New_York", time(8, 0), time(17, 0),
                         "New York FX"),
    "tokyo": SessionSpec("tokyo", "Asia/Tokyo", time(9, 0), time(15, 0), "Tokyo"),
}

# Which session each instrument's opening range belongs to. Keyed by the
# undecorated instrument name; brokers add their own suffixes and those are
# stripped before lookup.
#
# Indices go to their own cash open. Metals and FX go to London by default --
# a judgement, not a measurement, and the reason ``--session`` exists on the
# CLI so the alternative can be tested rather than argued about.
INSTRUMENT_SESSIONS: dict[str, str] = {
    # US indices
    "US30": "us_cash", "DJ30": "us_cash", "WS30": "us_cash", "DOW": "us_cash",
    "SPX500": "us_cash", "US500": "us_cash", "SP500": "us_cash",
    "NAS100": "us_cash", "USTEC": "us_cash", "NDX100": "us_cash",
    "US2000": "us_cash", "RUSSELL2000": "us_cash",
    # European and Asian indices, on their own cash opens
    "GER40": "london", "DE40": "london", "DAX40": "london", "GER30": "london",
    "UK100": "london", "FRA40": "london", "EU50": "london", "STOXX50": "london",
    "JP225": "tokyo", "JPN225": "tokyo",
    # Metals
    "XAUUSD": "london", "GOLD": "london", "XAUEUR": "london",
    "XAGUSD": "london", "SILVER": "london",
    "XPTUSD": "london", "XPDUSD": "london",
    # FX majors and crosses
    "EURUSD": "london", "GBPUSD": "london", "USDCHF": "london",
    "USDJPY": "london", "AUDUSD": "london", "NZDUSD": "london",
    "USDCAD": "london", "EURGBP": "london", "EURJPY": "london",
    "GBPJPY": "london", "AUDJPY": "london", "EURAUD": "london",
    "EURCHF": "london", "GBPCHF": "london", "CADJPY": "london",
    "CHFJPY": "london", "NZDJPY": "london", "AUDNZD": "london",
    "AUDCAD": "london", "EURCAD": "london", "GBPCAD": "london",
    "GBPAUD": "london", "USDSGD": "london", "USDZAR": "london",
}


def base_name(symbol: str) -> str:
    """A broker symbol reduced to the instrument it actually is.

    ``XAUUSD.r``, ``US30cash``, ``NAS100_m`` and ``EURUSD-ECN`` are all the
    same four instruments wearing different broker decorations, and a lookup
    table that only matches exact names silently treats every decorated
    symbol as unknown.
    """
    cleaned = "".join(ch for ch in symbol.upper() if ch.isalnum())
    # Longest match first, so SPX500 is not shadowed by a shorter key that
    # happens to prefix it.
    for known in sorted(INSTRUMENT_SESSIONS, key=len, reverse=True):
        if cleaned.startswith(known):
            return known
    return cleaned


def session_for(symbol: str, override: Optional[str] = None) -> SessionSpec:
    """The session an instrument's opening range is anchored to.

    An unknown instrument raises rather than defaulting to something
    plausible. Guessing "London, probably" for a symbol nobody mapped is how
    a bot ends up trading the opening range of a market that was closed.
    """
    if override:
        if override not in SESSIONS:
            raise KeyError(f"unknown session '{override}'; "
                           f"choose from {', '.join(sorted(SESSIONS))}")
        return SESSIONS[override]
    key = INSTRUMENT_SESSIONS.get(base_name(symbol))
    if key is None:
        raise KeyError(
            f"no session mapped for {symbol} (read as '{base_name(symbol)}'). "
            f"Pass an explicit session: {', '.join(sorted(SESSIONS))}"
        )
    return SESSIONS[key]


# --------------------------------------------------------------------------
# the broker's clock
# --------------------------------------------------------------------------
def measure_server_offset(
    server_time: datetime, real_now: Optional[datetime] = None
) -> int:
    """How far the broker's clock runs ahead of UTC, in minutes.

    ``server_time`` is what the terminal reported for a tick that just
    happened, read as though it were UTC. The difference between that and the
    real instant is the offset, and rounding it to the nearest half hour
    absorbs the second or two of latency between the two readings without
    inventing precision that isn't there.
    """
    real_now = real_now or datetime.now(timezone.utc)
    if server_time.tzinfo is None:
        server_time = server_time.replace(tzinfo=timezone.utc)
    minutes = (server_time - real_now).total_seconds() / 60.0
    return int(round(minutes / 30.0) * 30)


@dataclass(frozen=True)
class BarClock:
    """Translates the broker's timestamps into real UTC instants, and back.

    A single object rather than a loose ``- timedelta(hours=n)`` at each call
    site, because the sign of that subtraction is easy to get backwards and a
    backwards correction looks exactly like a working bot that never finds a
    setup.
    """

    server_offset_minutes: int = 0

    @property
    def offset(self) -> timedelta:
        return timedelta(minutes=self.server_offset_minutes)

    def to_utc(self, server_stamp: datetime) -> datetime:
        if server_stamp.tzinfo is None:
            server_stamp = server_stamp.replace(tzinfo=timezone.utc)
        return server_stamp - self.offset

    def to_server(self, real_utc: datetime) -> datetime:
        if real_utc.tzinfo is None:
            real_utc = real_utc.replace(tzinfo=timezone.utc)
        return real_utc + self.offset

    def frame_to_utc(self, bars: pd.DataFrame) -> pd.DataFrame:
        """Re-stamp a whole frame of broker bars onto real UTC.

        Done once, at the edge, so every module downstream can assume its
        index means what it says.
        """
        if self.server_offset_minutes == 0:
            return bars
        out = bars.copy()
        index = pd.DatetimeIndex(out.index)
        if index.tz is None:
            index = index.tz_localize(timezone.utc)
        out.index = index - self.offset
        out.index.name = bars.index.name or "ts"
        return out

    def describe(self) -> str:
        if self.server_offset_minutes == 0:
            return "broker bar times are UTC"
        hours = self.server_offset_minutes / 60.0
        return f"broker bar times run UTC{hours:+.1f}h; corrected before use"
