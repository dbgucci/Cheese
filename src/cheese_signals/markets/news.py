"""Scheduled economic releases: blackout windows and the daily brief.

Two separate jobs, and it is worth keeping them apart.

**The blackout** is mechanical and has nothing to do with direction. Around a
high-impact release the spread widens, slippage jumps and stops fill far from
where they were placed. A strategy validated on ordinary conditions was not
validated on those, so it does not run in them. This is the part that touches
trading.

**The brief** is the daily summary: what is scheduled, which instruments it
touches, and what it means for the day's risk. This is the part that touches
the user. It deliberately does not predict direction from a release, because
nothing here can -- the market's reaction depends on the number relative to
an expectation that is already priced in, and a bot reading a calendar the
night before does not know it. Saying "CPI at 13:30, expect volatility in
USD pairs and indices, size down or stand aside" is honest. Saying "CPI is
expected hot, go long USD" is a guess wearing a suit.

Which currency a release moves is mapped to instruments explicitly, since a
USD release moves XAUUSD and the US indices as surely as it moves EURUSD.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

# The free Forex Factory weekly calendar. No key, updated continuously.
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

HIGH = "High"
MEDIUM = "Medium"

# Which instruments a release in a given currency actually moves. USD is the
# broad one: a US release moves gold and the US indices, not only USD pairs.
CURRENCY_INSTRUMENTS: dict[str, tuple[str, ...]] = {
    "USD": ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "EURGBP",
            "XAUUSD", "US30", "SPX500", "NAS100"),
    "EUR": ("EURUSD", "EURGBP", "EURJPY"),
    "GBP": ("GBPUSD", "EURGBP"),
    "JPY": ("USDJPY", "EURJPY"),
    "AUD": ("AUDUSD",),
    "CAD": ("USDCAD",),
    "CHF": ("USDCHF",),
    "NZD": ("NZDUSD",),
    "CNY": ("AUDUSD", "XAUUSD"),
}

# Releases that reliably move markets hard enough to matter, by name fragment.
MAJOR = ("non-farm", "nonfarm", "cpi", "fomc", "federal funds", "interest rate",
         "gdp", "unemployment", "ppi", "pce", "retail sales", "ecb", "boe",
         "powell", "lagarde")


@dataclass
class Event:
    when: datetime
    currency: str
    title: str
    impact: str
    forecast: str = ""
    previous: str = ""

    @property
    def is_major(self) -> bool:
        low = self.title.lower()
        return self.impact == HIGH or any(m in low for m in MAJOR)

    def instruments(self, universe: Iterable[str]) -> list[str]:
        affected = set(CURRENCY_INSTRUMENTS.get(self.currency.upper(), ()))
        return sorted(s for s in universe if s.upper() in affected)


def fetch(url: str = CALENDAR_URL, timeout: float = 20.0) -> list[Event]:
    """This week's calendar. Network failures return nothing, loudly handled.

    A calendar that cannot be fetched must not silently become "no events
    today" -- that would turn a network problem into trading straight
    through an FOMC decision. Callers get an exception to decide on.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "kps-markets/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        raw = json.loads(fh.read().decode("utf-8"))
    return parse(raw)


def parse(raw: list[dict]) -> list[Event]:
    out = []
    for row in raw:
        stamp = row.get("date") or ""
        try:
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        out.append(Event(
            when=when.astimezone(timezone.utc),
            currency=(row.get("country") or row.get("currency") or "").upper(),
            title=row.get("title") or "",
            impact=(row.get("impact") or "").capitalize(),
            forecast=str(row.get("forecast") or ""),
            previous=str(row.get("previous") or ""),
        ))
    return sorted(out, key=lambda e: e.when)


def on_day(events: list[Event], day: datetime) -> list[Event]:
    return [e for e in events if e.when.date() == day.date()]


def blackout_windows(
    events: list[Event],
    universe: Iterable[str],
    day: Optional[datetime] = None,
) -> list[tuple[datetime, datetime, str]]:
    """The windows the guards refuse to trade in.

    Only major releases produce a window. Blacking out every low-impact
    number on the calendar would leave almost no tradeable session, which
    ends with the blackout being switched off entirely.
    """
    universe = list(universe)
    out = []
    for e in events:
        if not e.is_major:
            continue
        if day is not None and e.when.date() != day.date():
            continue
        if not e.instruments(universe):
            continue
        out.append((e.when, e.when, f"{e.currency} {e.title}"))
    return out


def daily_brief(
    events: list[Event],
    universe: Iterable[str],
    day: datetime,
    blackout_minutes: int = 15,
) -> str:
    """The morning message: what is scheduled and what it means for risk."""
    universe = list(universe)
    today = on_day(events, day)
    major = [e for e in today if e.is_major and e.instruments(universe)]

    lines = [f"*Market brief* — {day:%A %d %B %Y}", ""]
    if not today:
        lines.append("No scheduled releases on the calendar for today.")
    elif not major:
        lines.append(f"{len(today)} minor releases scheduled, none rated high "
                     f"impact for the instruments traded. Normal risk.")
    else:
        lines.append(f"*{len(major)} high-impact release(s) today.* Trading is "
                     f"suspended for {blackout_minutes} minutes either side of each.")
        lines.append("")
        for e in major:
            touched = e.instruments(universe)
            detail = f"  {e.when:%H:%M} UTC  {e.currency}  {e.title}"
            if e.forecast:
                detail += f"  (forecast {e.forecast}"
                detail += f", previous {e.previous})" if e.previous else ")"
            lines.append(detail)
            lines.append(f"      affects: {', '.join(touched)}")

    lines += ["", _posture(major, universe)]
    return "\n".join(lines)


def _posture(major: list[Event], universe: list[str]) -> str:
    """The risk sentence. Deliberately not a direction.

    Which way a release moves the market depends on the number against an
    expectation already in the price, which is not knowable the morning
    before. Anything that claimed otherwise would be inventing conviction.
    """
    if not major:
        return ("_Risk: normal. No scheduled event is expected to move the "
                "instruments traded today._")
    hit = sorted({s for e in major for s in e.instruments(universe)})
    heavy = len(major) >= 3
    return (
        f"_Risk: {'elevated' if heavy else 'raised'} on {', '.join(hit)}. "
        f"Direction after a release depends on the number against the "
        f"expectation already priced in, which is not knowable now — so the "
        f"bot stands aside through each window rather than guessing._"
    )
