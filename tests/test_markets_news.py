"""The calendar: blackout windows and the daily brief."""

from datetime import datetime, timedelta, timezone

import pytest

from cheese_signals.markets import news
from cheese_signals.markets.news import Event

UTC = timezone.utc
DAY = datetime(2026, 3, 2, tzinfo=UTC)
UNIVERSE = ["US30", "NAS100", "SPX500", "XAUUSD", "EURUSD", "EURGBP"]


def _ev(hour=13, minute=30, currency="USD", title="CPI m/m", impact="High", **kw):
    return Event(when=DAY.replace(hour=hour, minute=minute), currency=currency,
                 title=title, impact=impact, **kw)


# ------------------------------ parsing ------------------------------
def test_the_calendar_feed_parses_into_events():
    raw = [{"title": "Non-Farm Employment Change", "country": "USD",
            "date": "2026-03-06T13:30:00-00:00", "impact": "High",
            "forecast": "180K", "previous": "150K"}]
    events = news.parse(raw)
    assert len(events) == 1
    e = events[0]
    assert e.currency == "USD" and e.impact == "High"
    assert e.when == datetime(2026, 3, 6, 13, 30, tzinfo=UTC)
    assert e.forecast == "180K"


def test_rows_with_an_unreadable_date_are_dropped_not_fatal():
    raw = [{"title": "Bank Holiday", "country": "USD", "date": "", "impact": "Low"},
           {"title": "CPI", "country": "USD", "date": "2026-03-06T13:30:00-00:00",
            "impact": "High"}]
    assert [e.title for e in news.parse(raw)] == ["CPI"]


def test_events_come_back_in_time_order():
    raw = [{"title": "B", "country": "USD", "date": "2026-03-06T15:00:00-00:00",
            "impact": "High"},
           {"title": "A", "country": "USD", "date": "2026-03-06T13:30:00-00:00",
            "impact": "High"}]
    assert [e.title for e in news.parse(raw)] == ["A", "B"]


# ------------------------------ what counts as major ------------------------------
def test_a_high_impact_release_is_major():
    assert _ev(impact="High").is_major


def test_a_named_heavyweight_is_major_even_if_rated_medium():
    """Feeds disagree on impact ratings; an FOMC decision is an FOMC decision."""
    assert _ev(title="FOMC Statement", impact="Medium").is_major
    assert _ev(title="Non-Farm Employment Change", impact="Medium").is_major


def test_an_ordinary_low_impact_number_is_not_major():
    assert not _ev(title="Loan Officer Survey", impact="Low").is_major


# ------------------------------ which instruments ------------------------------
def test_a_us_release_touches_gold_and_the_indices_not_just_usd_pairs():
    """The mapping that a naive currency match would get wrong."""
    hit = _ev(currency="USD").instruments(UNIVERSE)
    assert "XAUUSD" in hit and "NAS100" in hit and "US30" in hit
    assert "EURUSD" in hit


def test_a_uk_release_does_not_touch_the_us_indices():
    hit = _ev(currency="GBP", title="BOE Rate Decision").instruments(UNIVERSE)
    assert hit == ["EURGBP"] or set(hit) <= {"EURGBP", "GBPUSD"}
    assert "NAS100" not in hit and "XAUUSD" not in hit


def test_a_currency_nothing_is_traded_in_touches_nothing():
    assert _ev(currency="NZD").instruments(["US30", "XAUUSD"]) == []


# ------------------------------ blackout windows ------------------------------
def test_a_major_release_produces_a_window():
    w = news.blackout_windows([_ev()], UNIVERSE, DAY)
    assert len(w) == 1
    start, end, label = w[0]
    assert start == DAY.replace(hour=13, minute=30)
    assert "USD" in label and "CPI" in label


def test_minor_releases_produce_no_windows():
    """Blacking out everything leaves no session, and then it gets switched off."""
    minor = [_ev(title="Loan Officer Survey", impact="Low", hour=h) for h in range(9, 18)]
    assert news.blackout_windows(minor, UNIVERSE, DAY) == []


def test_a_release_touching_nothing_traded_produces_no_window():
    assert news.blackout_windows([_ev(currency="NZD")], ["US30", "XAUUSD"], DAY) == []


def test_windows_are_limited_to_the_requested_day():
    today, tomorrow = _ev(), _ev()
    tomorrow.when = tomorrow.when + timedelta(days=1)
    assert len(news.blackout_windows([today, tomorrow], UNIVERSE, DAY)) == 1


def test_the_window_feeds_the_guards_in_the_shape_they_expect():
    from cheese_signals.markets.guards import GuardConfig, Guards

    g = Guards(GuardConfig(news_blackout_minutes=15))
    g.start_day(DAY.replace(hour=13), 10_000.0)
    windows = news.blackout_windows([_ev()], UNIVERSE, DAY)
    blocked = g.may_open(DAY.replace(hour=13, minute=35), 10_000.0, "US30", 0, windows)
    assert not blocked and "CPI" in blocked.reason


# ------------------------------ the daily brief ------------------------------
def test_the_brief_lists_each_major_release_with_its_instruments():
    text = news.daily_brief([_ev(), _ev(hour=15, title="FOMC Statement")],
                            UNIVERSE, DAY)
    assert "CPI m/m" in text and "FOMC Statement" in text
    assert "13:30 UTC" in text and "affects:" in text
    assert "NAS100" in text


def test_the_brief_includes_the_forecast_when_the_feed_has_one():
    text = news.daily_brief([_ev(forecast="0.3%", previous="0.2%")], UNIVERSE, DAY)
    assert "forecast 0.3%" in text and "previous 0.2%" in text


def test_a_quiet_day_says_so_plainly():
    text = news.daily_brief([], UNIVERSE, DAY)
    assert "No scheduled releases" in text
    assert "Risk: normal" in text


def test_a_day_of_only_minor_releases_is_reported_as_normal_risk():
    minor = [_ev(title="Loan Officer Survey", impact="Low")]
    text = news.daily_brief(minor, UNIVERSE, DAY)
    assert "none rated high impact" in text


def test_the_brief_never_predicts_direction():
    """A calendar the morning before cannot know the number against the
    expectation already in the price, so it must not pretend to."""
    text = news.daily_brief([_ev(forecast="0.3%")], UNIVERSE, DAY).lower()
    for claim in ("go long", "go short", "expect a rise", "bullish", "bearish",
                  "buy ", "sell "):
        assert claim not in text, f"the brief made a directional claim: {claim!r}"
    assert "not knowable now" in text


def test_the_brief_states_that_the_bot_stands_aside():
    text = news.daily_brief([_ev()], UNIVERSE, DAY)
    assert "suspended" in text and "stands aside" in text


def test_a_busy_day_is_called_elevated():
    events = [_ev(hour=h) for h in (12, 13, 15)]
    assert "elevated" in news.daily_brief(events, UNIVERSE, DAY)


# ------------------------------ the cached feed ------------------------------
def test_a_calendar_failure_keeps_yesterdays_windows_and_says_so():
    """An empty calendar means "trade through the release" -- never the
    right response to a network blip."""
    from cheese_signals.markets.run import NewsFeed

    feed = NewsFeed(UNIVERSE)
    feed.events = [_ev()]
    feed._fetched_on = None
    original = news.fetch
    try:
        news.fetch = lambda *a, **k: (_ for _ in ()).throw(OSError("no network"))
        assert feed.refresh(DAY) is False
    finally:
        news.fetch = original
    assert feed.events, "the previous calendar was discarded on a fetch failure"
    assert "could not be refreshed" in feed.brief(DAY)


def test_the_calendar_is_fetched_once_a_day():
    from cheese_signals.markets.run import NewsFeed

    calls = []
    feed = NewsFeed(UNIVERSE)
    original = news.fetch
    try:
        news.fetch = lambda *a, **k: (calls.append(1), [_ev()])[1]
        feed.refresh(DAY)
        feed.refresh(DAY.replace(hour=18))
        assert len(calls) == 1
        feed.refresh(DAY + timedelta(days=1))
        assert len(calls) == 2
    finally:
        news.fetch = original
