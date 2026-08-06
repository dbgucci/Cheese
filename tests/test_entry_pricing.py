"""Entry must be priced at the entry minute, not the one before it.

Found by re-deriving 855 live trades from the stored candles. 6.5% of entries
were priced from the bar *before* the correct one, which flipped the recorded
verdict on 13 trades -- including losses reported on trades that had clearly
won.

The cause was asymmetry: ``_settle`` waited for the bar closing at expiry
before pricing the exit, but ``_enter`` priced immediately. A bar stamped
``T-1min`` closes exactly at ``T`` and reaches the feed a few seconds later,
so an entry evaluated in those few seconds fell through to the previous bar
and recorded a price a full minute stale.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from cheese_signals import engine as engine_mod, storage
from cheese_signals.scheduler import PENDING, schedule_signal
from cheese_signals.settings import Settings
from cheese_signals.strategies import UP

START = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
# One clean step per minute, so an off-by-one-bar price is unmistakable.
PRICES = [1.100 + 0.001 * i for i in range(15)]


class _Clock:
    """Bars stamped by open time, visible only once closed and delivered."""

    def __init__(self, delivery_delay=0.0):
        idx = pd.date_range(START, periods=len(PRICES), freq="1min", tz="UTC")
        self.all = pd.DataFrame(
            {"open": PRICES, "high": [p + 0.0005 for p in PRICES],
             "low": [p - 0.0005 for p in PRICES], "close": PRICES, "volume": 100.0},
            index=idx,
        )
        self.now = START
        self.delay = delivery_delay

    def get_candles(self, count):
        ready = pd.Timestamp(self.now) - pd.Timedelta(seconds=self.delay)
        visible = self.all[self.all.index + pd.Timedelta(seconds=60) <= ready]
        return visible.tail(count)


def _engine(tmp_path, feed):
    s = Settings()
    s.assets = ["EURUSD_otc"]
    s.expiry_minutes = 1
    return engine_mod.SignalEngine(
        settings=s, journal=storage.Journal(tmp_path / "e.db"),
        feed_factory=lambda a: feed,
    )


def _pending(eng, minute):
    sig = schedule_signal("EURUSD_otc", UP, 0.8, "trend_continuation+bos", "r",
                          START, "london", lead_minutes=0, expiry_minutes=1)
    sig.entry_at = START + timedelta(minutes=minute)
    sig.expiry_at = sig.entry_at + timedelta(minutes=1)
    sig.status = PENDING
    sig.db_id = eng.journal.record_signal(
        asset=sig.asset, direction=sig.direction, score=sig.score, strategy=sig.strategy,
        reason=sig.reason, detected_at=START, entry_at=sig.entry_at,
        expiry_at=sig.expiry_at, session="london", utc_hour=12, features={},
    )
    eng.scheduler.add(sig)
    return sig


def test_entry_is_priced_at_the_entry_minute(tmp_path):
    """Price at 12:06 is the close of the bar stamped 12:05, i.e. 1.105."""
    feed = _Clock()
    eng = _engine(tmp_path, feed)
    sig = _pending(eng, 6)

    feed.now = sig.entry_at + timedelta(seconds=1)
    eng._enter(sig, feed.now)

    assert sig.entry_price == pytest.approx(1.105), (
        f"entered at {sig.entry_price}; 1.104 means it used the bar before"
    )


def test_a_slow_feed_does_not_produce_a_stale_entry_price(tmp_path):
    """The actual defect: the bar has closed but has not been delivered yet."""
    feed = _Clock(delivery_delay=4.0)      # bars arrive 4s after they close
    eng = _engine(tmp_path, feed)
    sig = _pending(eng, 6)

    feed.now = sig.entry_at + timedelta(seconds=1)
    eng._enter(sig, feed.now)
    assert sig.status == PENDING, "entered before the entry bar had arrived"
    assert sig.entry_price is None

    feed.now = sig.entry_at + timedelta(seconds=6)
    eng._enter(sig, feed.now)
    assert sig.entry_price == pytest.approx(1.105)


def test_entry_still_happens_if_the_bar_is_very_late(tmp_path):
    """A trade must not be stranded waiting past the grace window.

    The delay here is longer than the grace period but short enough that
    earlier bars are still available -- which is the real situation. A feed
    delivering nothing at all is a different case, covered below.
    """
    feed = _Clock(delivery_delay=40.0)
    eng = _engine(tmp_path, feed)
    sig = _pending(eng, 6)

    feed.now = sig.entry_at + timedelta(seconds=1)
    eng._enter(sig, feed.now)
    assert sig.status == PENDING

    feed.now = sig.entry_at + timedelta(seconds=engine_mod.ENTRY_GRACE_SECONDS + 1)
    eng._enter(sig, feed.now)
    assert sig.entry_price is not None, "the trade was stranded"


def test_a_late_entry_is_marked_approximate(tmp_path):
    """So an audit can exclude it instead of trusting a stale fill."""
    feed = _Clock(delivery_delay=40.0)
    eng = _engine(tmp_path, feed)
    sig = _pending(eng, 6)

    feed.now = sig.entry_at + timedelta(seconds=engine_mod.ENTRY_GRACE_SECONDS + 1)
    eng._enter(sig, feed.now)

    assert sig.features.get("entry_price_approximate") is True
    assert any(t.outcome == "ENTRY PRICE APPROXIMATE" for t in eng.traces.recent())


def test_a_normal_entry_is_not_marked_approximate(tmp_path):
    feed = _Clock()
    eng = _engine(tmp_path, feed)
    sig = _pending(eng, 6)
    feed.now = sig.entry_at + timedelta(seconds=1)
    eng._enter(sig, feed.now)
    assert not (sig.features or {}).get("entry_price_approximate")


def test_entry_and_exit_are_priced_by_the_same_rule(tmp_path):
    """The asymmetry between them was the bug; they must stay in step."""
    feed = _Clock()
    eng = _engine(tmp_path, feed)
    feed.now = START + timedelta(minutes=14)

    entry_at = START + timedelta(minutes=6)
    expiry_at = START + timedelta(minutes=7)
    assert eng._price_at("EURUSD_otc", entry_at) == pytest.approx(1.105)
    assert eng._price_at("EURUSD_otc", expiry_at) == pytest.approx(1.106)
    # A 1-minute call therefore moves exactly one step of the series.
    move = eng._price_at("EURUSD_otc", expiry_at) - eng._price_at("EURUSD_otc", entry_at)
    assert move == pytest.approx(0.001, abs=1e-9)


def test_both_sides_wait_on_the_same_helper(tmp_path):
    """Guards against one path being fixed and the other drifting again."""
    import inspect

    enter = inspect.getsource(engine_mod.SignalEngine._enter)
    settle = inspect.getsource(engine_mod.SignalEngine._settle)
    assert "_has_candle_closing_at" in enter
    assert "_has_candle_closing_at" in settle


def test_the_recorded_verdict_matches_an_independent_recomputation(tmp_path):
    """End to end: enter, settle, and check the outcome against the candles."""
    feed = _Clock()
    eng = _engine(tmp_path, feed)
    sig = _pending(eng, 6)

    feed.now = sig.entry_at + timedelta(seconds=1)
    eng._enter(sig, feed.now)

    results = []
    eng.on_result = results.append
    feed.now = sig.expiry_at + timedelta(seconds=1)
    eng._settle(sig, feed.now)

    assert results
    out = results[0]
    truth = 1.106 > 1.105          # a BUY on a rising series
    assert out.won is truth
    assert out.entry_price == pytest.approx(1.105)
    assert out.exit_price == pytest.approx(1.106)


def test_a_feed_with_no_data_at_all_cancels_rather_than_inventing_a_price(tmp_path):
    """Distinct from a late bar: with nothing to price from, do not trade."""
    feed = _Clock(delivery_delay=10_000.0)
    eng = _engine(tmp_path, feed)
    sig = _pending(eng, 6)

    feed.now = sig.entry_at + timedelta(seconds=engine_mod.ENTRY_GRACE_SECONDS + 1)
    eng._enter(sig, feed.now)

    assert sig.entry_price is None
    assert sig.status == "cancelled"
    assert "no price" in (sig.cancel_reason or "")
