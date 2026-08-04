"""Regression tests for the bug where every signal was cancelled before entry.

A liquidity sweep is a one-shot event: it fires on a single candle and is
over. Re-running the detector during the lead window correctly reports "no
sweep right now" (score 0.0), so scoring a pending signal that way cancelled
100% of trades before entry -- which meant nothing ever entered, and no
outcomes, history or analytics were ever produced.
"""

from datetime import datetime, timezone

import pytest

from cheese_signals.scheduler import revalidate, schedule_signal
from cheese_signals.strategies import DOWN, FLAT, UP


def _pending(direction=DOWN, level=1.1050, event=True, score=0.76):
    now = datetime(2026, 8, 1, 3, 45, tzinfo=timezone.utc)
    sig = schedule_signal(
        "USDJPY_otc", direction, score, "liquidity_sweep", "swept liquidity",
        now, "off_session", lead_minutes=2, expiry_minutes=1,
    )
    sig.features = {"invalidation_level": level, "event_setup": event}
    return sig


def test_event_setup_survives_score_falling_to_zero():
    """The exact production bug: score reads 0.00 next candle, must NOT cancel."""
    sig = _pending()
    assert revalidate(sig, FLAT, 0.0, min_score=0.6, latest_close=1.1040) is None


def test_event_setup_survives_no_redetection_over_several_candles():
    sig = _pending()
    for close in (1.1042, 1.1039, 1.1045):
        assert revalidate(sig, FLAT, 0.0, min_score=0.6, latest_close=close) is None


def test_sell_setup_cancels_when_price_closes_above_the_level():
    """A close beyond the level means it genuinely broke -- the opposite trade.

    Asserts the decision and the numbers, not the phrasing: the wording is
    now chosen per trigger (see test_stake_and_wording.py) and pinning the
    exact sentence here would break every time it is reworded.
    """
    sig = _pending(direction=DOWN, level=1.1050)
    reason = revalidate(sig, FLAT, 0.0, min_score=0.6, latest_close=1.1061)
    assert reason and "invalidated" in reason
    assert "1.10500" in reason and "1.10610" in reason


def test_buy_setup_cancels_when_price_closes_below_the_level():
    sig = _pending(direction=UP, level=1.1000)
    reason = revalidate(sig, FLAT, 0.0, min_score=0.6, latest_close=1.0988)
    assert reason and "invalidated" in reason
    assert "1.10000" in reason and "1.09880" in reason


def test_opposing_signal_still_cancels():
    sig = _pending(direction=DOWN, level=1.1050)
    reason = revalidate(sig, UP, 0.8, min_score=0.6, latest_close=1.1040)
    assert reason and "direction flipped" in reason


def test_continuous_setup_still_cancels_on_score_decay():
    """Trend/mean-reversion are ongoing conditions, so decay remains meaningful."""
    sig = _pending(direction=UP, level=None, event=False)
    sig.features = {"event_setup": False}
    reason = revalidate(sig, UP, 0.10, min_score=0.6, latest_close=1.1040)
    assert reason and "decayed" in reason


def test_continuous_setup_survives_healthy_score():
    sig = _pending(direction=UP, level=None, event=False)
    sig.features = {"event_setup": False}
    assert revalidate(sig, UP, 0.55, min_score=0.6, latest_close=1.1040) is None


def test_missing_close_price_does_not_cancel():
    sig = _pending()
    assert revalidate(sig, FLAT, 0.0, min_score=0.6, latest_close=None) is None


def test_already_entered_signal_is_never_revalidated():
    sig = _pending()
    sig.status = "active"
    assert revalidate(sig, UP, 0.9, min_score=0.6, latest_close=99.0) is None
