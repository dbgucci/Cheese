"""Warm-up must accumulate across restarts, and a stall must mean a stall.

From a real 26-minute session log:

- the live feed opened with ~145 candles and gained 0.64 per minute, so an
  EMA 200 (220 bars) needed roughly 90 more minutes of uptime;
- the user restarted five times, and each restart began again from ~145
  because candles were only journalled *after* the warm-up gate, so nothing
  was ever stored while warming up;
- BLOCKED fired 51 times on one asset while the feed was filling normally,
  because the stall check counted consecutive scans (every ~5s) rather than
  elapsed time (a candle every ~90s).
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from cheese_signals import engine as engine_mod, storage
from cheese_signals.settings import Settings


def _frame(start: datetime, n: int, price: float = 1.1) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {"open": price, "high": price + 0.001, "low": price - 0.001,
         "close": price, "volume": 100.0},
        index=idx,
    )


class _ShortWindowFeed:
    """A broker feed that only ever exposes a short trailing window.

    This is what Pocket Option's live stream actually does: ask for eight
    hours and get about two and a half, refilling a candle at a time.
    """

    def __init__(self, start: datetime, window: int = 145):
        self.start = start
        self.window = window
        self.offset = 0

    def advance(self, minutes: int) -> None:
        self.offset += minutes

    def get_candles(self, count):
        return _frame(self.start + timedelta(minutes=self.offset), self.window)


def _engine(tmp_path, feed, assets=("EURUSD_otc",), **overrides):
    s = Settings()
    s.assets = list(assets)
    for k, v in overrides.items():
        setattr(s, k, v)
    return engine_mod.SignalEngine(
        settings=s,
        journal=storage.Journal(tmp_path / "w.db"),
        feed_factory=lambda a: feed,
    )


# --------------------------- candles are journalled ---------------------------
def test_candles_are_stored_while_still_warming_up(tmp_path):
    """The fix that makes warm-up cumulative at all."""
    start = datetime(2026, 8, 4, 1, 0, tzinfo=timezone.utc)
    feed = _ShortWindowFeed(start, window=145)
    eng = _engine(tmp_path, feed)

    eng._tick()
    stored = eng.journal.candle_count("EURUSD_otc")
    assert stored == 145, (
        f"only {stored} candles stored -- nothing is journalled during warm-up, "
        "so a restart loses all progress"
    )


def test_history_accumulates_past_what_the_feed_alone_offers(tmp_path):
    """145 live candles + a journal that remembers = enough for an EMA 200."""
    start = datetime(2026, 8, 4, 1, 0, tzinfo=timezone.utc)
    feed = _ShortWindowFeed(start, window=145)
    eng = _engine(tmp_path, feed)

    eng._tick()
    assert len(eng._history_for("EURUSD_otc", feed.get_candles(500))) == 145

    # Two hours later the feed still shows only its 145-candle window...
    feed.advance(120)
    eng._tick()
    assert len(feed.get_candles(500)) == 145

    # ...but the merged view now covers the whole span.
    merged = eng._history_for("EURUSD_otc", feed.get_candles(500))
    assert len(merged) == 265, f"expected 145 + 120 new minutes, got {len(merged)}"
    assert merged.index.is_monotonic_increasing
    assert not merged.index.has_duplicates


def test_a_restart_keeps_the_history_it_had(tmp_path):
    """The bug the user hit five times in one session."""
    start = datetime(2026, 8, 4, 1, 0, tzinfo=timezone.utc)
    feed = _ShortWindowFeed(start, window=145)

    first = _engine(tmp_path, feed)
    first._tick()
    feed.advance(120)
    first._tick()
    before = len(first._history_for("EURUSD_otc", feed.get_candles(500)))
    first.journal.close()

    # Same data folder, brand new engine -- as if the app were reopened.
    restarted = _engine(tmp_path, feed)
    after = len(restarted._history_for("EURUSD_otc", feed.get_candles(500)))
    assert after == before, f"restart dropped history: {before} -> {after}"


def test_live_candles_win_over_stored_ones_on_overlap(tmp_path):
    """A journalled bar is a cache, not the source of truth."""
    start = datetime(2026, 8, 4, 1, 0, tzinfo=timezone.utc)
    eng = _engine(tmp_path, _ShortWindowFeed(start))

    eng.journal.record_candles("EURUSD_otc", _frame(start, 50, price=1.1))
    live = _frame(start + timedelta(minutes=40), 20, price=1.5)

    merged = eng._history_for("EURUSD_otc", live)
    assert merged.loc[live.index[0], "close"] == 1.5, "stored bar overwrote a live one"
    assert len(merged) == 60


def test_a_long_live_window_skips_the_journal_query(tmp_path):
    """No point reading history the feed already covers."""
    start = datetime(2026, 8, 4, 1, 0, tzinfo=timezone.utc)
    eng = _engine(tmp_path, _ShortWindowFeed(start))
    eng.journal.record_candles("EURUSD_otc", _frame(start, 100))

    big = _frame(start, engine_mod.HISTORY_MERGE_CEILING + 10)
    assert eng._history_for("EURUSD_otc", big) is big


def test_an_empty_feed_is_passed_through_untouched(tmp_path):
    start = datetime(2026, 8, 4, 1, 0, tzinfo=timezone.utc)
    eng = _engine(tmp_path, _ShortWindowFeed(start))
    assert eng._history_for("EURUSD_otc", None) is None
    empty = _frame(start, 0)
    assert len(eng._history_for("EURUSD_otc", empty)) == 0


# ------------------------------ stall detection ------------------------------
def test_a_filling_feed_is_never_called_blocked(tmp_path):
    """The false alarm: 51 BLOCKED traces in 26 minutes on a healthy feed.

    Scans happen every ~5s; candles arrive every ~90s. Counting scans made
    "no growth since the last scan" the normal case.
    """
    start = datetime(2026, 8, 4, 1, 0, tzinfo=timezone.utc)
    eng = _engine(tmp_path, _ShortWindowFeed(start))

    now = start
    have = 145
    for step in range(300):              # 25 minutes of 5-second scans
        now += timedelta(seconds=5)
        if step % 18 == 0:               # a candle roughly every 90 seconds
            have += 1
        eng._warmup_trace("EURUSD_otc", have, 220, now)

    blocked = [t for t in eng.traces.recent(limit=1000) if t.outcome == "BLOCKED"]
    assert not blocked, f"BLOCKED fired {len(blocked)} times on a feed that was filling"


def test_a_truly_dead_feed_is_still_reported(tmp_path):
    start = datetime(2026, 8, 4, 1, 0, tzinfo=timezone.utc)
    eng = _engine(tmp_path, _ShortWindowFeed(start))

    now = start
    for _ in range(200):
        now += timedelta(seconds=5)
        eng._warmup_trace("EURUSD_otc", 145, 220, now)   # never grows

    blocked = [t for t in eng.traces.recent(limit=1000) if t.outcome == "BLOCKED"]
    assert blocked, "a feed that genuinely stopped must still be reported"
    assert "stuck at 145 candles" in blocked[0].summary


def test_the_stall_report_does_not_repeat_every_scan(tmp_path):
    start = datetime(2026, 8, 4, 1, 0, tzinfo=timezone.utc)
    eng = _engine(tmp_path, _ShortWindowFeed(start))

    now = start
    for _ in range(600):                 # 50 minutes of dead feed
        now += timedelta(seconds=5)
        eng._warmup_trace("EURUSD_otc", 145, 220, now)

    blocked = [t for t in eng.traces.recent(limit=2000) if t.outcome == "BLOCKED"]
    assert len(blocked) <= 8, f"BLOCKED repeated {len(blocked)} times in 50 minutes"


def test_warming_up_reports_an_eta(tmp_path):
    """"90 min to go" is actionable; "145 candles" alone is not."""
    start = datetime(2026, 8, 4, 1, 0, tzinfo=timezone.utc)
    eng = _engine(tmp_path, _ShortWindowFeed(start))

    now = start
    have = 145
    for _ in range(20):
        now += timedelta(minutes=1)
        have += 1                        # ~1 candle/min
        eng._warmup_trace("EURUSD_otc", have, 220, now)

    warming = [t for t in eng.traces.recent(limit=100) if t.outcome == "WARMING UP"]
    assert any("min to go" in t.summary for t in warming), warming[-1].summary
    assert any("candles/min" in t.summary for t in warming)


def test_no_eta_is_invented_from_too_short_a_window(tmp_path):
    start = datetime(2026, 8, 4, 1, 0, tzinfo=timezone.utc)
    eng = _engine(tmp_path, _ShortWindowFeed(start))

    eng._warmup_trace("EURUSD_otc", 145, 220, start)
    eng._warmup_trace("EURUSD_otc", 146, 220, start + timedelta(seconds=30))

    warming = [t for t in eng.traces.recent(limit=10) if t.outcome == "WARMING UP"]
    assert not any("min to go" in t.summary for t in warming), (
        "an ETA from 30 seconds of data is a guess dressed as a measurement"
    )
