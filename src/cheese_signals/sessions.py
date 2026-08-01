"""Trading session tagging -- measured, not assumed.

A widely repeated claim is that short-timeframe reversal setups work best
during the London/New York overlap. That is plausible for *real* exchange-fed
instruments, where the overlap genuinely carries the most institutional
volume.

It should NOT be assumed for Pocket Option **OTC** pairs. OTC instruments
trade 24/7 and are priced by the broker's own synthetic engine rather than
by an interbank order book, so there is no real London or New York volume
behind an "OTC" quote at all. Any session effect on OTC is a property of the
broker's pricing model, not of world liquidity -- which means it has to be
*measured from your own logged results*, not taken from a forex article.

So this module only **tags** each signal with its session. The analytics
module then reports realised win rate per session bucket from your own trade
history, and you can enable ``restrict_to_sessions`` in settings once your
data actually justifies it.
"""

from __future__ import annotations

from datetime import datetime, time

# UTC hour ranges. Endpoints are treated as [start, end).
SESSION_RANGES = {
    "sydney": (time(21, 0), time(6, 0)),
    "tokyo": (time(0, 0), time(9, 0)),
    "london": (time(7, 0), time(16, 0)),
    "new_york": (time(12, 0), time(21, 0)),
}

# The London/NY overlap: highest real-market liquidity. Tagged for analysis;
# only meaningful as a filter on non-OTC instruments.
OVERLAP = (time(12, 0), time(16, 0))


def _in_range(t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # wraps midnight


def active_sessions(now_utc: datetime) -> list[str]:
    t = now_utc.time()
    return [name for name, (s, e) in SESSION_RANGES.items() if _in_range(t, s, e)]


def is_overlap(now_utc: datetime) -> bool:
    """True during the London/New York overlap (12:00-16:00 UTC)."""
    return _in_range(now_utc.time(), *OVERLAP)


def session_label(now_utc: datetime) -> str:
    """A single human-readable bucket used as the analytics grouping key."""
    if is_overlap(now_utc):
        return "london_ny_overlap"
    active = active_sessions(now_utc)
    if not active:
        return "off_session"
    return "+".join(sorted(active))


def hour_bucket(now_utc: datetime) -> int:
    """UTC hour, the finest-grained session key used for per-hour win-rate stats."""
    return now_utc.hour


def is_otc(asset: str) -> bool:
    return asset.lower().endswith("_otc")


def market_is_open(asset: str, now_utc: datetime) -> bool:
    """OTC pairs trade 24/7; real pairs follow the Sunday-open/Friday-close forex week."""
    if is_otc(asset):
        return True
    weekday = now_utc.weekday()  # Mon=0 .. Sun=6
    if weekday == 5:  # Saturday
        return False
    if weekday == 6:  # Sunday: opens ~21:00 UTC
        return now_utc.time() >= time(21, 0)
    if weekday == 4:  # Friday: closes ~21:00 UTC
        return now_utc.time() < time(21, 0)
    return True
