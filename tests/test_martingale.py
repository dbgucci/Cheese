"""Single-step martingale: re-enter a loss on the next candle, once.

The behaviour asked for, and what these tests pin:

* first entry wins  -> announce the win immediately;
* first entry loses -> announce *nothing*, re-enter the same pair and
  direction on the very next candle at double the stake;
* recovery wins     -> announce a win, marked as a recovery;
* recovery loses    -> announce the loss.

The sequence is the unit, not the trade. A loss mid-sequence is not an
outcome the user should be told about, because it is about to be answered.

None of this changes the edge -- expected value per unit staked is identical
to flat staking -- so the settings carry that in writing rather than letting
the feature imply otherwise.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from cheese_signals import engine as engine_mod, storage
from cheese_signals.scheduler import ACTIVE, schedule_signal
from cheese_signals.settings import Settings
from cheese_signals.strategies import UP

START = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


class _Feed:
    """Bars stamped with their open time, visible once they have closed."""

    def __init__(self, closes):
        idx = pd.date_range(START, periods=len(closes), freq="1min", tz="UTC")
        self.all = pd.DataFrame(
            {"open": closes, "high": [c + 0.002 for c in closes],
             "low": [c - 0.002 for c in closes], "close": closes, "volume": 100.0},
            index=idx,
        )
        self.now = START + timedelta(hours=1)

    def get_candles(self, count):
        visible = self.all[self.all.index + pd.Timedelta(seconds=60) <= pd.Timestamp(self.now)]
        return visible.tail(count)


class _Notifier:
    def __init__(self):
        self.results = []

    def send_result(self, outcome):
        self.results.append(outcome)
        return True, ""


def _engine(tmp_path, closes, **overrides):
    s = Settings()
    s.assets = ["EURUSD_otc"]
    s.martingale_enabled = True
    s.expiry_minutes = 1
    for k, v in overrides.items():
        setattr(s, k, v)
    feed = _Feed(closes)
    note = _Notifier()
    eng = engine_mod.SignalEngine(
        settings=s, journal=storage.Journal(tmp_path / "m.db"),
        feed_factory=lambda a: feed, notifier=note,
    )
    return eng, feed, note


def _price_at(feed, minute):
    """Price at START+minute: the close of the bar stamped one minute earlier.

    Taking the close of the bar stamped START+minute instead names the price a
    full minute later, which in a monotonic series makes entry equal exit --
    and that is a refund now, not a loss, so the fixture stopped testing what
    it claimed to.
    """
    return float(feed.all.close.loc[START + timedelta(minutes=minute - 1)])


def _place(eng, feed, minute, price=None):
    """An active trade entering at START+minute, expiring a minute later."""
    if price is None:
        price = _price_at(feed, minute)
    sig = schedule_signal("EURUSD_otc", UP, 0.8, "trend_continuation+bos", "r",
                          START, "london", lead_minutes=0, expiry_minutes=1)
    sig.entry_at = START + timedelta(minutes=minute)
    sig.expiry_at = sig.entry_at + timedelta(minutes=1)
    sig.status = ACTIVE
    sig.entry_price = price
    sig.db_id = eng.journal.record_signal(
        asset=sig.asset, direction=sig.direction, score=sig.score, strategy=sig.strategy,
        reason=sig.reason, detected_at=START, entry_at=sig.entry_at,
        expiry_at=sig.expiry_at, session="london", utc_hour=12, features={},
    )
    eng.scheduler.add(sig)
    return sig


# price series: rises to minute 5, then falls
RISING = [1.100 + 0.001 * i for i in range(12)]
FALLING = [1.120 - 0.001 * i for i in range(12)]


# ------------------------------ a winning first entry ------------------------------
def test_a_first_entry_win_is_announced_immediately(tmp_path):
    eng, feed, note = _engine(tmp_path, RISING)
    sig = _place(eng, feed, 4)
    eng._settle(sig, sig.expiry_at + timedelta(seconds=1))

    assert len(note.results) == 1, "a win must be reported at once"
    assert note.results[0].won
    assert not eng.scheduler.awaiting_entry(sig.expiry_at), "no recovery after a win"


# ------------------------------ a losing first entry -------------------------------
def test_a_losing_first_entry_announces_nothing(tmp_path):
    """The sequence is still open; a loss about to be recovered is not news."""
    eng, feed, note = _engine(tmp_path, FALLING)
    sig = _place(eng, feed, 4)
    eng._settle(sig, sig.expiry_at + timedelta(seconds=1))

    assert note.results == [], "a recoverable loss was announced"


def test_a_losing_first_entry_re_enters_on_the_very_next_candle(tmp_path):
    eng, feed, note = _engine(tmp_path, FALLING)
    sig = _place(eng, feed, 4)
    eng._settle(sig, sig.expiry_at + timedelta(seconds=1))

    pending = eng.scheduler.awaiting_entry(sig.expiry_at - timedelta(seconds=1))
    assert len(pending) == 1
    rec = pending[0]
    assert rec.entry_at == sig.expiry_at, "recovery must start as the loss ends -- no gap"
    assert rec.expiry_at == sig.expiry_at + timedelta(minutes=1)
    assert rec.asset == sig.asset and rec.direction == sig.direction


def test_the_recovery_doubles_the_stake(tmp_path):
    eng, feed, note = _engine(tmp_path, FALLING, account_balance=500.0,
                              risk_per_trade=0.02, max_stake=100.0)
    sig = _place(eng, feed, 4)
    assert eng._stake(sig) == 10.0
    eng._settle(sig, sig.expiry_at + timedelta(seconds=1))

    rec = eng.scheduler.awaiting_entry(sig.expiry_at - timedelta(seconds=1))[0]
    assert eng._stake(rec) == 20.0


def test_the_safety_cap_still_binds_on_a_recovery(tmp_path):
    """Doubling must not be a way around max_stake."""
    eng, feed, note = _engine(tmp_path, FALLING, account_balance=500.0,
                              risk_per_trade=0.02, max_stake=15.0)
    sig = _place(eng, feed, 4)
    eng._settle(sig, sig.expiry_at + timedelta(seconds=1))
    rec = eng.scheduler.awaiting_entry(sig.expiry_at - timedelta(seconds=1))[0]
    assert eng._stake(rec) == 15.0


# --------------------------- how the sequence finishes ----------------------------
def test_a_recovery_win_is_announced_and_marked(tmp_path):
    eng, feed, note = _engine(tmp_path, FALLING)
    first = _place(eng, feed, 4)
    eng._settle(first, first.expiry_at + timedelta(seconds=1))
    rec = eng.scheduler.awaiting_entry(first.expiry_at - timedelta(seconds=1))[0]

    rec.status = ACTIVE
    rec.entry_price = _price_at(feed, 5)
    rec.direction = -1               # falling series: a SELL recovery wins
    eng._settle(rec, rec.expiry_at + timedelta(seconds=1))

    assert len(note.results) == 1
    out = note.results[0]
    assert out.won
    assert (out.signal.features or {}).get("martingale_step") == 1


def test_a_recovery_loss_is_announced_with_no_further_re_entry(tmp_path):
    eng, feed, note = _engine(tmp_path, FALLING, martingale_reentries=1)
    first = _place(eng, feed, 4)
    eng._settle(first, first.expiry_at + timedelta(seconds=1))
    rec = eng.scheduler.awaiting_entry(first.expiry_at - timedelta(seconds=1))[0]

    rec.status = ACTIVE
    rec.entry_price = _price_at(feed, 5)   # falling series, UP call -> loses
    eng._settle(rec, rec.expiry_at + timedelta(seconds=1))

    assert len(note.results) == 1, "the final loss must be reported"
    assert not note.results[0].won
    later = eng.scheduler.awaiting_entry(rec.expiry_at - timedelta(seconds=1))
    assert not later, "the ladder ran past its configured depth"


def test_the_ladder_stops_at_the_configured_depth(tmp_path):
    eng, feed, note = _engine(tmp_path, FALLING, martingale_reentries=2)
    sig = _place(eng, feed, 2)
    steps = []
    for _ in range(5):
        eng._settle(sig, sig.expiry_at + timedelta(seconds=1))
        nxt = eng.scheduler.awaiting_entry(sig.expiry_at - timedelta(seconds=1))
        if not nxt:
            break
        sig = nxt[0]
        sig.status = ACTIVE
        sig.entry_price = _price_at(feed, int((sig.entry_at - START).total_seconds() // 60))
        steps.append(sig.features["martingale_step"])
    assert steps == [1, 2], f"expected two re-entries, got {steps}"


# ------------------------------- feature is opt-in --------------------------------
def test_martingale_is_off_by_default():
    assert Settings().martingale_enabled is False


def test_a_loss_is_announced_normally_when_disabled(tmp_path):
    eng, feed, note = _engine(tmp_path, FALLING, martingale_enabled=False)
    sig = _place(eng, feed, 4)
    eng._settle(sig, sig.expiry_at + timedelta(seconds=1))
    assert len(note.results) == 1 and not note.results[0].won
    assert not eng.scheduler.awaiting_entry(sig.expiry_at)


# ------------------------------- the warnings -------------------------------
def test_turning_it_on_states_that_it_does_not_change_the_edge():
    s = Settings(); s.martingale_enabled = True
    assert any("does not change the edge" in c for c in s.conflicts())


def test_turning_it_on_reports_the_measured_win_rate():
    """The ADX<25 carve-out this warning used to cite failed out of sample.

    It read 55.4% on the first 855 trades and 46.5% on the next 641, so the
    warning no longer offers it as the configuration that rescues martingale.
    """
    s = Settings(); s.martingale_enabled = True; s.adx_max = 25.0
    text = " ".join(s.conflicts())
    assert "48.9%" in text and "break-even" in text
    assert "no ADX filter" not in text


def test_a_deep_ladder_is_called_out():
    s = Settings(); s.martingale_enabled = True; s.martingale_reentries = 3
    assert any("busted the account" in c for c in s.conflicts())


# ------------------------------ the telegram label -----------------------------
@pytest.mark.parametrize("step,won,expected", [
    (0, True, "✅ *WIN*"),
    (1, True, "recovery"),
    (0, False, "❌ *LOSS*"),
    (1, False, "after recovery"),
])
def test_the_result_message_distinguishes_a_recovery(step, won, expected):
    from cheese_signals.notifiers.telegram import TelegramNotifier

    sent = []
    n = TelegramNotifier("t", "c")
    n.send_verbose = lambda text, **kw: (sent.append(text), (True, ""))[1]

    sig = schedule_signal("EURUSD_otc", UP, 0.8, "s", "r", START, "london",
                          lead_minutes=0, expiry_minutes=1)
    sig.features = {"martingale_step": step}

    class _Out:
        signal = sig
        entry_price = 1.1000
        exit_price = 1.1010
        move_pips = 10.0
        pnl = 8.5 if won else -10.0
        reason = "r"
    _Out.won = won

    n.send_result(_Out())
    assert expected in sent[-1], sent[-1]
    if step:
        assert f"martingale step {step}" in sent[-1]
