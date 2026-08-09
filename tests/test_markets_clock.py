"""Session opens across daylight saving, and the broker's own clock offset.

These are the tests for the failures that produce no error message: a bot that
builds its opening range over the wrong fifteen minutes trades happily and
loses money for reasons no log line explains.
"""

from datetime import date, datetime, time, timezone

import pandas as pd
import pytest

from cheese_signals.markets import clock
from cheese_signals.markets.clock import BarClock, SESSIONS, base_name, session_for


# ------------------------------ daylight saving ------------------------------
def test_the_new_york_open_is_1430_utc_in_winter():
    """09:30 EST. A bot with 13:30 hardcoded is an hour early all winter."""
    us = SESSIONS["us_cash"]
    assert us.open_utc(date(2026, 1, 14)) == datetime(2026, 1, 14, 14, 30, tzinfo=timezone.utc)


def test_the_new_york_open_is_1330_utc_in_summer():
    """The same 09:30 local, an hour earlier in UTC. Neither constant is right
    all year, which is the entire reason this module exists."""
    us = SESSIONS["us_cash"]
    assert us.open_utc(date(2026, 7, 14)) == datetime(2026, 7, 14, 13, 30, tzinfo=timezone.utc)


def test_the_open_shifts_on_the_dst_boundary_itself():
    """US clocks moved on 8 March 2026; the Friday before and Monday after
    differ by an hour in UTC while both are 09:30 in New York."""
    us = SESSIONS["us_cash"]
    friday = us.open_utc(date(2026, 3, 6))
    monday = us.open_utc(date(2026, 3, 9))
    assert friday.hour == 14 and monday.hour == 13


def test_london_and_new_york_are_not_always_five_hours_apart():
    """The three weeks a year that break a two-table hardcoded bot.

    Europe changes on 29 March 2026, the US on 8 March. In between, London is
    four hours ahead of New York rather than five.
    """
    ny, lon = SESSIONS["us_cash"], SESSIONS["london"]

    def gap(day):
        # Both at 08:00/09:30 local; compare the local-midnight offsets instead.
        a = ny.open_utc(day) - datetime.combine(
            day, time(9, 30), tzinfo=timezone.utc)
        b = lon.open_utc(day) - datetime.combine(
            day, time(8, 0), tzinfo=timezone.utc)
        return (a - b).total_seconds() / 3600

    assert gap(date(2026, 3, 2)) == 5.0       # both on winter time
    assert gap(date(2026, 3, 16)) == 4.0      # US moved, Europe has not
    assert gap(date(2026, 4, 6)) == 5.0       # both on summer time


def test_the_session_close_is_also_dst_correct():
    us = SESSIONS["us_cash"]
    assert us.close_utc(date(2026, 1, 14)).hour == 21
    assert us.close_utc(date(2026, 7, 14)).hour == 20


def test_a_session_date_is_the_exchanges_date_not_utcs():
    """00:30 UTC on Wednesday is still Tuesday's Tokyo... and Tuesday's US
    session. Grouping by the UTC date splits one session across two days."""
    us = SESSIONS["us_cash"]
    late = datetime(2026, 1, 15, 0, 30, tzinfo=timezone.utc)   # 19:30 Wed 14th NY
    assert us.session_date(late) == date(2026, 1, 14)


def test_weekends_are_not_trading_days():
    us = SESSIONS["us_cash"]
    assert us.is_open_weekday(date(2026, 3, 6)) is True        # Friday
    assert us.is_open_weekday(date(2026, 3, 7)) is False       # Saturday
    assert us.is_open_weekday(date(2026, 3, 8)) is False       # Sunday


# ------------------------------ symbol mapping ------------------------------
@pytest.mark.parametrize("decorated,expected", [
    ("XAUUSD", "XAUUSD"), ("XAUUSD.r", "XAUUSD"), ("XAUUSD-ECN", "XAUUSD"),
    ("US30", "US30"), ("US30cash", "US30"), ("NAS100_m", "NAS100"),
    ("SPX500.pro", "SPX500"), ("EURUSD.a", "EURUSD"),
])
def test_broker_suffixes_do_not_hide_the_instrument(decorated, expected):
    """A lookup that only matches exact names treats every decorated symbol as
    unknown, which is most symbols at most brokers."""
    assert base_name(decorated) == expected


def test_indices_go_to_their_cash_open_and_metals_to_london():
    assert session_for("NAS100_m").key == "us_cash"
    assert session_for("US30cash").key == "us_cash"
    assert session_for("XAUUSD.r").key == "london"
    assert session_for("EURUSD").key == "london"


def test_an_unmapped_instrument_raises_rather_than_guessing():
    """Guessing "London, probably" is how a bot trades the opening range of a
    market that was closed at the time."""
    with pytest.raises(KeyError, match="no session mapped"):
        session_for("BTCUSD")


def test_an_explicit_session_override_wins():
    assert session_for("XAUUSD", "comex").key == "comex"


def test_an_unknown_override_names_the_valid_choices():
    with pytest.raises(KeyError, match="us_cash"):
        session_for("US30", "wall_street")


# --------------------------- the broker's clock ---------------------------
def test_a_server_running_two_hours_ahead_is_measured_as_two_hours():
    real = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)
    server = datetime(2026, 3, 2, 14, 0, 3, tzinfo=timezone.utc)   # +3s of latency
    assert clock.measure_server_offset(server, real) == 120


def test_a_server_on_utc_measures_as_zero():
    real = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)
    assert clock.measure_server_offset(real, real) == 0


def test_a_server_behind_utc_measures_negative():
    real = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)
    server = datetime(2026, 3, 2, 7, 0, 0, tzinfo=timezone.utc)
    assert clock.measure_server_offset(server, real) == -300


def test_latency_does_not_invent_precision():
    """Half a minute of lag must not become a half-minute offset."""
    real = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)
    server = datetime(2026, 3, 2, 15, 0, 25, tzinfo=timezone.utc)
    assert clock.measure_server_offset(server, real) == 180


def test_correcting_a_frame_moves_it_back_onto_real_utc():
    """The EET broker case: bars labelled 15:30 are really 12:30 UTC, and an
    uncorrected bot looks for the New York open in the middle of lunch."""
    index = pd.date_range("2026-03-02 15:30", periods=3, freq="1min", tz="UTC")
    bars = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0},
                        index=index)
    corrected = BarClock(180).frame_to_utc(bars)
    assert corrected.index[0] == pd.Timestamp("2026-03-02 12:30", tz="UTC")
    assert len(corrected) == 3


def test_a_zero_offset_leaves_the_frame_alone():
    index = pd.date_range("2026-03-02 15:30", periods=3, freq="1min", tz="UTC")
    bars = pd.DataFrame({"close": 1.0}, index=index)
    assert BarClock(0).frame_to_utc(bars) is bars


def test_the_correction_round_trips():
    """The sign of this subtraction being backwards looks exactly like a bot
    that simply never finds a setup, so it is asserted both ways."""
    c = BarClock(180)
    real = datetime(2026, 3, 2, 12, 30, tzinfo=timezone.utc)
    assert c.to_utc(c.to_server(real)) == real
    assert c.to_server(real) == datetime(2026, 3, 2, 15, 30, tzinfo=timezone.utc)


def test_a_naive_frame_is_treated_as_utc_rather_than_local():
    index = pd.date_range("2026-03-02 15:30", periods=2, freq="1min")
    bars = pd.DataFrame({"close": [1.0, 1.0]}, index=index)
    out = BarClock(120).frame_to_utc(bars)
    assert out.index[0] == pd.Timestamp("2026-03-02 13:30", tz="UTC")


def test_describe_says_which_way_the_offset_goes():
    assert "UTC" in BarClock(0).describe()
    assert "+3.0h" in BarClock(180).describe()
