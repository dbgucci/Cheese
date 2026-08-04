"""A result must be priced *at* expiry, and arrive as soon as expiry closes.

Measured from the recovered live journal: every one of 13 trades settled
61-65 seconds after its stated expiry. The cause was a convention error, not
slowness.

A candle is stamped with its **open** time, so the bar stamped ``T`` spans
``[T, T+60)`` and its close is the price at ``T+60``. Settlement waited for a
bar stamped ``expiry_at`` -- which does not close until a minute *after*
expiry -- and then priced the exit from that bar. So a 1-minute option was
scored over the two minutes from entry to one minute past expiry, and the
message arrived a minute late as a side effect.

The bar that matters is the one stamped ``expiry_at - 60``: it closes exactly
at expiry, and it exists the moment expiry arrives.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from cheese_signals import engine as engine_mod, storage
from cheese_signals.scheduler import ACTIVE, schedule_signal
from cheese_signals.settings import Settings
from cheese_signals.strategies import UP

START = datetime(2026, 8, 4, 6, 0, tzinfo=timezone.utc)


class _Clock:
    """A feed exposing only the bars that have closed by ``self.now``."""

    def __init__(self, closes):
        idx = pd.date_range(START, periods=len(closes), freq="1min", tz="UTC")
        self.all = pd.DataFrame(
            {"open": closes, "high": [c + 0.001 for c in closes],
             "low": [c - 0.001 for c in closes], "close": closes, "volume": 100.0},
            index=idx,
        )
        self.now = START

    def get_candles(self, count):
        # A bar stamped T is only visible once it has closed, at T + 60s.
        visible = self.all[self.all.index + pd.Timedelta(seconds=60) <= pd.Timestamp(self.now)]
        return visible.tail(count)


def _engine(tmp_path, feed, **overrides):
    s = Settings()
    s.assets = ["EURUSD_otc"]
    for k, v in overrides.items():
        setattr(s, k, v)
    return engine_mod.SignalEngine(
        settings=s, journal=storage.Journal(tmp_path / "t.db"),
        feed_factory=lambda a: feed,
    )


# price series: 06:00 .. 06:09, one bar per minute
PRICES = [1.100, 1.101, 1.102, 1.103, 1.104, 1.105, 1.106, 1.107, 1.108, 1.109]


# ------------------------------ pricing at T ------------------------------
def test_price_at_is_the_bar_that_closed_then(tmp_path):
    feed = _Clock(PRICES)
    feed.now = START + timedelta(minutes=10)          # everything visible
    eng = _engine(tmp_path, feed)

    # The bar stamped 06:04 closes at 06:05, so the price at 06:05 is 1.104.
    assert eng._price_at("EURUSD_otc", START + timedelta(minutes=5)) == pytest.approx(1.104)
    assert eng._price_at("EURUSD_otc", START + timedelta(minutes=8)) == pytest.approx(1.107)


def test_price_at_is_not_one_candle_late(tmp_path):
    """The specific defect: returning the bar stamped ts, not the one before."""
    feed = _Clock(PRICES)
    feed.now = START + timedelta(minutes=10)
    eng = _engine(tmp_path, feed)

    at_five = eng._price_at("EURUSD_otc", START + timedelta(minutes=5))
    assert at_five != pytest.approx(1.105), (
        "priced from the bar that OPENS at 06:05, which closes at 06:06 -- "
        "a whole candle after the moment asked for"
    )


# --------------------------- when settlement runs ---------------------------
def test_the_expiry_bar_is_available_the_instant_expiry_arrives(tmp_path):
    feed = _Clock(PRICES)
    eng = _engine(tmp_path, feed)
    expiry = START + timedelta(minutes=5)

    feed.now = expiry - timedelta(seconds=1)
    assert not eng._has_candle_closing_at("EURUSD_otc", expiry)

    feed.now = expiry
    assert eng._has_candle_closing_at("EURUSD_otc", expiry), (
        "the bar closing at expiry exists at expiry; waiting longer is pure lag"
    )


def test_settlement_does_not_wait_for_an_extra_candle(tmp_path):
    """The 61-65 second lag seen on every trade in the live journal."""
    feed = _Clock(PRICES)
    eng = _engine(tmp_path, feed)

    entry = START + timedelta(minutes=4)
    expiry = START + timedelta(minutes=5)
    sig = schedule_signal("EURUSD_otc", UP, 0.8, "trend_continuation+bos", "r",
                          entry - timedelta(minutes=1), "tokyo",
                          lead_minutes=0, expiry_minutes=1)
    sig.entry_at, sig.expiry_at = entry, expiry
    sig.status = ACTIVE
    feed.now = entry                      # price the entry at entry, as the engine does
    sig.entry_price = eng._price_at("EURUSD_otc", entry)
    assert sig.entry_price == pytest.approx(1.103)
    eng.scheduler.add(sig)

    feed.now = expiry + timedelta(seconds=1)
    settled = []
    eng.on_result = settled.append
    eng._settle(sig, feed.now)

    assert settled, "result did not arrive within a second of expiry"
    assert settled[0].exit_price == pytest.approx(1.104), (
        f"exit priced at {settled[0].exit_price}, not the 06:05 close"
    )


def test_entry_and_exit_span_exactly_the_stated_expiry(tmp_path):
    """A 1-minute option must be scored over one minute, not two."""
    feed = _Clock(PRICES)
    eng = _engine(tmp_path, feed)
    feed.now = START + timedelta(minutes=10)

    entry = START + timedelta(minutes=4)
    expiry = START + timedelta(minutes=5)
    entry_price = eng._price_at("EURUSD_otc", entry)
    exit_price = eng._price_at("EURUSD_otc", expiry)

    assert entry_price == pytest.approx(1.103)   # bar stamped 06:03
    assert exit_price == pytest.approx(1.104)    # bar stamped 06:04
    # One minute of this series moves exactly one step.
    assert exit_price - entry_price == pytest.approx(0.001, abs=1e-9)


def test_a_two_minute_expiry_spans_two_minutes(tmp_path):
    feed = _Clock(PRICES)
    eng = _engine(tmp_path, feed)
    feed.now = START + timedelta(minutes=10)

    entry = START + timedelta(minutes=4)
    expiry = START + timedelta(minutes=6)
    move = eng._price_at("EURUSD_otc", expiry) - eng._price_at("EURUSD_otc", entry)
    assert move == pytest.approx(0.002, abs=1e-9), "a 2-minute expiry must span 2 bars"


def test_settlement_still_waits_if_the_bar_has_not_closed(tmp_path):
    """Do not price an exit from a bar that has not happened."""
    feed = _Clock(PRICES)
    eng = _engine(tmp_path, feed)
    expiry = START + timedelta(minutes=5)
    feed.now = expiry - timedelta(seconds=30)

    sig = schedule_signal("EURUSD_otc", UP, 0.8, "s", "r",
                          START, "tokyo", lead_minutes=0, expiry_minutes=1)
    sig.entry_at = START + timedelta(minutes=4)
    sig.expiry_at = expiry
    sig.status = ACTIVE
    sig.entry_price = 1.103
    settled = []
    eng.on_result = settled.append
    eng._settle(sig, feed.now)
    assert not settled, "settled before the expiry bar closed"


# ------------------------------ the messages ------------------------------
def test_both_messages_state_the_expiry_duration():
    """"Expiry 06:10:00" needs mental arithmetic; "1 min" does not."""
    from cheese_signals.notifiers.telegram import TelegramNotifier

    sent = []
    n = TelegramNotifier("t", "c")
    n.send = lambda text, **kw: sent.append(text) or True
    n.send_verbose = lambda text, **kw: (sent.append(text), (True, ""))[1]

    sig = schedule_signal("EURUSD_otc", UP, 0.8, "trend_continuation+bos", "r",
                          START, "tokyo", lead_minutes=1, expiry_minutes=2)
    n.send_signal(sig)
    assert "*2 min*" in sent[-1], sent[-1]

    class _Outcome:
        signal = sig
        won = True
        entry_price = 1.1030
        exit_price = 1.1050
        move_pips = 20.0
        pnl = 8.5
        reason = "held through expiry"

    n.send_result(_Outcome())
    assert "(2 min)" in sent[-1], sent[-1]
    assert f"{sig.entry_at:%H:%M:%S}" in sent[-1]
    assert f"{sig.expiry_at:%H:%M:%S}" in sent[-1]
