from datetime import datetime, timedelta, timezone

from cheese_signals.scheduler import (
    CANCELLED,
    PENDING,
    SignalScheduler,
    next_candle_open,
    revalidate,
    schedule_signal,
)
from cheese_signals.strategies import DOWN, FLAT, UP


def _now():
    return datetime(2026, 8, 1, 14, 31, 17, tzinfo=timezone.utc)


def test_next_candle_open_snaps_forward():
    nxt = next_candle_open(_now(), 60)
    assert nxt == datetime(2026, 8, 1, 14, 32, 0, tzinfo=timezone.utc)


def test_schedule_gives_requested_lead_time():
    sig = schedule_signal(
        "EURUSD_otc", UP, 0.8, "liquidity_sweep", "test", _now(), "overlap",
        lead_minutes=2, expiry_minutes=1,
    )
    # Detected 14:31:17 -> next candle 14:32 -> +2 candles = 14:34 entry.
    assert sig.entry_at == datetime(2026, 8, 1, 14, 34, 0, tzinfo=timezone.utc)
    assert sig.expiry_at == datetime(2026, 8, 1, 14, 35, 0, tzinfo=timezone.utc)
    assert sig.lead_seconds >= 120


def test_zero_lead_enters_next_candle():
    sig = schedule_signal(
        "EURUSD_otc", UP, 0.8, "s", "r", _now(), "overlap", lead_minutes=0, expiry_minutes=1
    )
    assert sig.entry_at == datetime(2026, 8, 1, 14, 32, 0, tzinfo=timezone.utc)


def test_revalidate_cancels_on_direction_flip():
    sig = schedule_signal("EURUSD_otc", UP, 0.8, "s", "r", _now(), "x", lead_minutes=2)
    reason = revalidate(sig, DOWN, 0.8, min_score=0.6)
    assert reason and "direction flipped" in reason


def test_revalidate_cancels_on_score_collapse():
    sig = schedule_signal("EURUSD_otc", UP, 0.8, "s", "r", _now(), "x", lead_minutes=2)
    reason = revalidate(sig, UP, 0.20, min_score=0.6)
    assert reason and "decayed" in reason


def test_revalidate_keeps_valid_signal():
    sig = schedule_signal("EURUSD_otc", UP, 0.8, "s", "r", _now(), "x", lead_minutes=2)
    assert revalidate(sig, UP, 0.75, min_score=0.6) is None
    assert revalidate(sig, FLAT, 0.75, min_score=0.6) is None


def test_scheduler_lifecycle():
    sched = SignalScheduler()
    now = _now()
    sig = schedule_signal("EURUSD_otc", UP, 0.8, "s", "r", now, "x", lead_minutes=2)
    sched.add(sig)

    assert sched.has_pending_for("EURUSD_otc")
    assert not sched.has_pending_for("GBPUSD_otc")
    assert sched.awaiting_entry(now) == [sig]
    assert sched.due_for_entry(now) == []

    at_entry = sig.entry_at
    assert sched.due_for_entry(at_entry) == [sig]

    sched.mark_active(sig, 1.1)
    assert sig.entry_price == 1.1
    assert sched.due_for_settlement(sig.expiry_at) == [sig]

    sched.mark_settled(sig)
    assert not sched.has_pending_for("EURUSD_otc")


def test_cancel_records_reason():
    sched = SignalScheduler()
    sig = schedule_signal("EURUSD_otc", UP, 0.8, "s", "r", _now(), "x")
    sched.add(sig)
    sched.cancel(sig, "invalidated")
    assert sig.status == CANCELLED
    assert sig.cancel_reason == "invalidated"
    assert not sched.has_pending_for("EURUSD_otc")
