"""Regression tests from real logged trades.

Both bugs here were found in a live trade export, not in synthetic testing.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from cheese_signals import outcome as om
from cheese_signals.scheduler import schedule_signal
from cheese_signals.strategies import DOWN, UP

from test_engine_integration import FakeFeed, _engine, _sweep_frame, journal  # noqa: F401


# ------------------------------- pip sizing -------------------------------
def test_jpy_pairs_use_a_001_pip():
    """A 0.011 USDJPY move is 1.1 pips, not 110 -- the log reported 110."""
    assert om.pip_size("USDJPY_otc") == 0.01
    assert om.pip_size("EURJPY_otc") == 0.01
    assert om.pips("USDJPY_otc", 0.011) == pytest.approx(1.1)


def test_non_jpy_pairs_use_a_00001_pip():
    assert om.pip_size("EURUSD_otc") == 0.0001
    assert om.pips("EURUSD_otc", 0.00022) == pytest.approx(2.2)


def test_reported_move_matches_reality_for_jpy():
    now = datetime(2026, 8, 2, 4, 39, tzinfo=timezone.utc)
    sig = schedule_signal("USDJPY_otc", UP, 0.7, "liquidity_sweep", "r", now, "tokyo")
    res = om.settle(sig, 161.488, 161.499, stake=10, payout=0.85, settled_at=now)
    assert res.move_pips == pytest.approx(1.1, abs=0.01)
    assert "+1.1 pips" in res.reason


def test_reported_move_matches_reality_for_eurjpy():
    now = datetime(2026, 8, 2, 4, 59, tzinfo=timezone.utc)
    sig = schedule_signal("EURJPY_otc", DOWN, 0.7, "liquidity_sweep", "r", now, "tokyo")
    res = om.settle(sig, 180.838, 180.773, stake=10, payout=0.85, settled_at=now)
    assert res.move_pips == pytest.approx(-6.5, abs=0.01)


# ---------------------------- settlement timing ---------------------------
def test_settlement_waits_for_the_expiry_candle(journal):  # noqa: F811
    """Settling early priced exit from the entry candle -> a fabricated loss.

    6% of real trades were recorded as "price closed exactly at the entry
    price" losses because of this.
    """
    feed = FakeFeed(_sweep_frame())
    eng, cap = _engine(journal, feed)

    eng._scan_asset("EURUSD_otc", datetime(2026, 8, 3, 14, 30, 20, tzinfo=timezone.utc))
    sig = cap["signals"][0]

    feed.append(sig.entry_at, 1.1000)
    eng._enter(sig, sig.entry_at)

    # Expiry has arrived but its candle has not. Must NOT settle yet.
    eng._settle(sig, sig.expiry_at)
    assert cap["results"] == []
    assert journal.summary_counts()["settled"] == 0
    assert sig.status == "active"

    # Once the expiry candle lands, it settles on the correct price.
    feed.append(sig.expiry_at, 1.0994)
    eng._settle(sig, sig.expiry_at)
    assert len(cap["results"]) == 1
    assert cap["results"][0].exit_price == pytest.approx(1.0994)
    assert cap["results"][0].won is True


def test_settlement_gives_up_after_the_grace_period(journal):  # noqa: F811
    """A feed that stalls must not leave a trade open forever."""
    feed = FakeFeed(_sweep_frame())
    eng, cap = _engine(journal, feed)

    eng._scan_asset("EURUSD_otc", datetime(2026, 8, 3, 14, 30, 20, tzinfo=timezone.utc))
    sig = cap["signals"][0]
    feed.append(sig.entry_at, 1.1000)
    eng._enter(sig, sig.entry_at)

    from cheese_signals.engine import SETTLEMENT_GRACE_SECONDS

    late = sig.expiry_at + timedelta(seconds=SETTLEMENT_GRACE_SECONDS + 1)
    eng._settle(sig, late)
    assert len(cap["results"]) == 1
    assert journal.summary_counts()["settled"] == 1
