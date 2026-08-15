"""Tests for the candle recorder.

The recorder's job is to still be running in a fortnight. Every test here
is about that: a broker error must not kill the process, a dead socket must
be rebuilt rather than reused, and one sick asset must not stall the others.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from cheese_signals import storage
from cheese_signals.research import recorder as rec


def make_candles(n=60, start="2026-08-01"):
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    close = 1.10 + np.cumsum(np.full(n, 1e-5))
    return pd.DataFrame(
        {"open": close, "high": close + 1e-5, "low": close - 1e-5,
         "close": close, "volume": 0.0},
        index=idx,
    )


class FakeFeed:
    def __init__(self, frames, fail_times=0):
        self._frames = frames
        self._fail_times = fail_times
        self.calls = 0

    def get_candles(self, count):
        self.calls += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            raise ConnectionError("socket closed")
        return self._frames


@pytest.fixture
def journal(tmp_path):
    return storage.Journal(tmp_path / "test.db")


def test_candles_reach_the_journal(journal):
    r = rec.Recorder(
        assets=["EURUSD_otc"],
        feed_factory=lambda a: FakeFeed(make_candles(60)),
        journal=journal,
        sleep=lambda s: None,
    )
    written = r.poll_once()
    assert written == 60
    assert journal.candle_count("EURUSD_otc") == 60


def test_a_broker_error_does_not_kill_the_run(journal):
    """A process that dies at 3am quietly costs a week of data."""
    r = rec.Recorder(
        assets=["EURUSD_otc"],
        feed_factory=lambda a: FakeFeed(make_candles(10), fail_times=1),
        journal=journal,
        sleep=lambda s: None,
    )
    assert r.poll_once() == 0           # first poll fails
    assert r.stats.stat("EURUSD_otc").errors == 1
    assert r.stats.reconnects == 1       # and the feed was dropped


def test_a_failed_feed_is_rebuilt_not_reused(journal):
    """A socket that failed once keeps failing; recreating it is the recovery."""
    built = []

    def factory(asset):
        feed = FakeFeed(make_candles(10), fail_times=1 if not built else 0)
        built.append(feed)
        return feed

    # A poll interval longer than the first backoff clears the wait in one
    # skipped poll, so the third poll is a real retry.
    r = rec.Recorder(
        assets=["EURUSD_otc"],
        feed_factory=factory,
        journal=journal,
        poll_seconds=rec.MAX_BACKOFF,
        sleep=lambda s: None,
    )
    r.poll_once()                        # fails, drops the feed
    r.poll_once()                        # waits out the backoff
    r.poll_once()                        # retries, rebuilding the feed

    assert len(built) == 2, "the broken feed was reused instead of rebuilt"
    assert journal.candle_count("EURUSD_otc") == 10


def test_one_sick_asset_does_not_stall_the_others(journal):
    """Backoff is per asset, so a dead pair cannot hold up a healthy one."""

    def factory(asset):
        if asset == "BROKEN_otc":
            return FakeFeed(make_candles(10), fail_times=99)
        return FakeFeed(make_candles(30))

    r = rec.Recorder(
        assets=["BROKEN_otc", "EURUSD_otc"],
        feed_factory=factory,
        journal=journal,
        sleep=lambda s: None,
    )
    r.poll_once()
    assert journal.candle_count("EURUSD_otc") == 30
    assert journal.candle_count("BROKEN_otc") == 0


def test_backoff_doubles_from_the_failure_count():
    r = rec.Recorder(assets=["X"], feed_factory=lambda a: None)
    assert r._backoff_for(0) == 0.0
    assert r._backoff_for(1) == rec.INITIAL_BACKOFF
    assert r._backoff_for(2) == rec.INITIAL_BACKOFF * 2
    assert r._backoff_for(3) == rec.INITIAL_BACKOFF * 4
    assert r._backoff_for(99) == rec.MAX_BACKOFF, "backoff exceeded its cap"


def test_repeated_failures_lengthen_the_wait(journal):
    r = rec.Recorder(
        assets=["X_otc"],
        feed_factory=lambda a: FakeFeed(make_candles(5), fail_times=99),
        journal=journal,
        poll_seconds=rec.MAX_BACKOFF,   # each skipped poll clears the wait
        sleep=lambda s: None,
    )
    waits = []
    for _ in range(6):
        r.poll_once()
        if r._wait["X_otc"] > 0:
            waits.append(r._wait["X_otc"])

    assert waits == sorted(waits), "backoff did not grow monotonically"
    assert waits[0] < waits[-1], "backoff never increased"
    assert max(waits) <= rec.MAX_BACKOFF


def test_a_success_resets_the_backoff(journal):
    """One good poll must clear the penalty, not leave it half-decayed."""
    feed = FakeFeed(make_candles(10), fail_times=1)
    r = rec.Recorder(
        assets=["EURUSD_otc"],
        feed_factory=lambda a: feed,
        journal=journal,
        poll_seconds=rec.MAX_BACKOFF,
        sleep=lambda s: None,
    )
    r.poll_once()                        # fail
    assert r._failures["EURUSD_otc"] == 1
    r.poll_once()                        # wait
    r.poll_once()                        # succeed
    assert r._failures["EURUSD_otc"] == 0
    assert r._wait["EURUSD_otc"] == 0.0


def test_a_fast_poll_interval_still_clears_the_wait(journal):
    """With poll_seconds below 1s the decrement must not round to nothing."""
    # One feed instance, so its single scripted failure is not reset each
    # time the recorder rebuilds it.
    feed = FakeFeed(make_candles(5), fail_times=1)
    r = rec.Recorder(
        assets=["X_otc"],
        feed_factory=lambda a: feed,
        journal=journal,
        poll_seconds=0.0,
        sleep=lambda s: None,
    )
    r.poll_once()                        # fail -> wait = INITIAL_BACKOFF
    for _ in range(int(rec.INITIAL_BACKOFF) + 2):
        r.poll_once()
    assert journal.candle_count("X_otc") == 5, "asset never retried"


def test_repeated_polls_do_not_duplicate_candles(journal):
    """The journal is upsert-keyed, so a re-poll of the same window is free."""
    frames = make_candles(40)
    r = rec.Recorder(
        assets=["EURUSD_otc"],
        feed_factory=lambda a: FakeFeed(frames),
        journal=journal,
        sleep=lambda s: None,
    )
    assert r.poll_once() == 40
    assert r.poll_once() == 0
    assert journal.candle_count("EURUSD_otc") == 40


def test_run_stops_when_asked(journal):
    r = rec.Recorder(
        assets=["EURUSD_otc"],
        feed_factory=lambda a: FakeFeed(make_candles(5)),
        journal=journal,
        sleep=lambda s: None,
    )
    stats = r.run(max_polls=3)
    assert stats.polls == 3


def test_recorder_needs_at_least_one_asset(journal):
    with pytest.raises(ValueError):
        rec.Recorder(assets=[], feed_factory=lambda a: None, journal=journal)


class FakeClock:
    """Controllable clock, so staleness can be tested without waiting."""

    def __init__(self, start=None):
        self.now = start or datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + pd.Timedelta(seconds=seconds).to_pytimedelta()


def test_a_silent_recorder_raises_a_stale_warning(journal):
    """An expired SSID must not look like a healthy run for three weeks."""
    clock = FakeClock()
    r = rec.Recorder(
        assets=["EURUSD_otc"],
        feed_factory=lambda a: FakeFeed(make_candles(5), fail_times=999),
        journal=journal,
        sleep=lambda s: None,
        now=clock,
        stale_after_seconds=600,
    )
    r.poll_once()
    assert r.stale_warning() is None, "warned before the threshold"

    clock.advance(601)
    warning = r.stale_warning()
    assert warning is not None
    assert "POCKET_OPTION_SSID" in warning


def test_the_stale_warning_does_not_repeat_every_poll(journal):
    clock = FakeClock()
    r = rec.Recorder(
        assets=["EURUSD_otc"],
        feed_factory=lambda a: FakeFeed(make_candles(5), fail_times=999),
        journal=journal,
        sleep=lambda s: None,
        now=clock,
        stale_after_seconds=600,
    )
    clock.advance(601)
    assert r.stale_warning() is not None
    assert r.stale_warning() is None, "warning repeated immediately"

    clock.advance(601)
    assert r.stale_warning() is not None, "warning never repeated"


def test_a_healthy_recorder_never_warns(journal):
    clock = FakeClock()
    r = rec.Recorder(
        assets=["EURUSD_otc"],
        feed_factory=lambda a: FakeFeed(make_candles(5)),
        journal=journal,
        sleep=lambda s: None,
        now=clock,
        stale_after_seconds=600,
    )
    r.poll_once()
    clock.advance(300)
    assert r.stale_warning() is None


def test_progress_report_states_what_the_sample_can_test(journal):
    journal.record_candles("EURUSD_otc", make_candles(5_000))
    text = rec.progress_report(journal)

    assert "EURUSD_otc" in text
    assert "hour-of-day rules" in text
    # 5,000 bars is under the 8,000 an hour rule needs -- it must say so.
    assert "needs 8,000" in text


def test_progress_report_handles_an_empty_journal(journal):
    assert "No candles recorded yet" in rec.progress_report(journal)
