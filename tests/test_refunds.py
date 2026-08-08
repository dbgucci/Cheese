"""A flat close is a refund, not a loss.

Found while re-auditing 1,543 live trades: 16 of them closed at exactly the
entry price and every one was recorded as a full loss -- $800 of P/L that
never happened, and half a point off the reported win rate, because a
returned stake sat in the denominator as if it had been lost.

Pocket Option returns the stake when the expiry price equals the entry price.
So a flat close must:

* pay nothing and lose nothing;
* stay out of the win-rate denominator, since it decided nothing;
* not start a post-win or post-loss cooldown;
* not trigger a martingale recovery -- there is nothing to recover.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd

from cheese_signals import analytics, engine as engine_mod, outcome as outcome_mod, storage
from cheese_signals.scheduler import ACTIVE, schedule_signal
from cheese_signals.settings import Settings
from cheese_signals.strategies import UP

START = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
FLAT = [1.100] * 12          # every bar closes where the last one did


def _sig():
    s = schedule_signal("EURUSD_otc", UP, 0.8, "trend_continuation+bos", "r",
                        START, "london", lead_minutes=0, expiry_minutes=1)
    s.features = {}
    return s


# ------------------------------- the outcome itself -------------------------------
def test_a_flat_close_is_not_a_loss():
    o = outcome_mod.settle(_sig(), 1.1000, 1.1000, stake=50.0, payout=0.85,
                           settled_at=START)
    assert o.refunded is True
    assert o.won is False
    assert o.pnl == 0.0, "a returned stake is not a -50 loss"
    assert o.result_word == "REFUND"


def test_the_reason_says_the_stake_came_back():
    o = outcome_mod.settle(_sig(), 1.1000, 1.1000, stake=50.0, payout=0.85,
                           settled_at=START)
    assert "returned" in o.reason and "Refund" in o.reason


def test_a_decided_trade_is_untouched():
    win = outcome_mod.settle(_sig(), 1.1000, 1.1010, 50.0, 0.85, START)
    loss = outcome_mod.settle(_sig(), 1.1000, 1.0990, 50.0, 0.85, START)
    assert win.won and win.pnl == 42.5 and not win.refunded
    assert not loss.won and loss.pnl == -50.0 and not loss.refunded


# ------------------------------- the win rate -------------------------------
def test_refunds_leave_the_win_rate_denominator(tmp_path):
    j = storage.Journal(tmp_path / "r.db")
    rows = [(1.1000, 1.1010, True), (1.1000, 1.0990, False), (1.1000, 1.1000, False)]
    for i, (e, x, won) in enumerate(rows, start=1):
        j.record_signal(asset="EURUSD_otc", direction=1, score=0.8, strategy="s",
                        reason="r", detected_at=START, entry_at=START,
                        expiry_at=START + timedelta(minutes=1), session="london",
                        utc_hour=12, features={})
        j.record_outcome(signal_id=i, asset="EURUSD_otc", direction=1, entry_price=e,
                         exit_price=x, won=won, payout=0.85, stake=50.0,
                         settled_at=START, reason="r")
    st = j.stats()
    assert st["trades"] == 3
    assert st["refunds"] == 1
    assert st["wins"] == 1 and st["losses"] == 1
    assert st["win_rate"] == 0.5, "the refund was counted as a loss"
    assert st["pnl"] == 42.5 - 50.0, "the refund moved the balance"
    assert j.summary_counts()["refunds"] == 1


def test_the_analytics_slices_exclude_refunds():
    rows = [
        {"asset": "EURUSD_otc", "won": 1, "pnl": 42.5, "entry_price": 1.1, "exit_price": 1.2},
        {"asset": "EURUSD_otc", "won": 0, "pnl": -50.0, "entry_price": 1.1, "exit_price": 1.0},
        {"asset": "EURUSD_otc", "won": 0, "pnl": 0.0, "entry_price": 1.1, "exit_price": 1.1},
    ]
    o = analytics.overall(rows)
    assert o.trades == 2 and o.refunds == 1 and o.win_rate == 0.5
    # P/L per trade still spreads over every settled trade, refunds included.
    assert o.avg_pnl == (42.5 - 50.0) / 3
    by_asset = analytics.breakdown(rows)["asset"][0]
    assert by_asset.trades == 2 and by_asset.refunds == 1


def test_a_refund_is_not_mined_for_loss_reasons():
    rows = [{"won": 0, "entry_price": 1.1, "exit_price": 1.1,
             "outcome_reason": "Refund: price closed exactly at the entry price."}]
    assert analytics.loss_reasons(rows) == []


# ------------------------------- engine behaviour -------------------------------
class _Feed:
    def __init__(self, closes):
        idx = pd.date_range(START, periods=len(closes), freq="1min", tz="UTC")
        self.all = pd.DataFrame(
            {"open": closes, "high": [c + 0.002 for c in closes],
             "low": [c - 0.002 for c in closes], "close": closes, "volume": 100.0},
            index=idx,
        )
        self.now = START + timedelta(hours=1)

    def get_candles(self, count):
        vis = self.all[self.all.index + pd.Timedelta(seconds=60) <= pd.Timestamp(self.now)]
        return vis.tail(count)


class _Notifier:
    def __init__(self):
        self.results = []

    def send_result(self, outcome):
        self.results.append(outcome)
        return True, ""


def _engine(tmp_path, **over):
    s = Settings()
    s.assets = ["EURUSD_otc"]
    s.expiry_minutes = 1
    for k, v in over.items():
        setattr(s, k, v)
    note = _Notifier()
    eng = engine_mod.SignalEngine(
        settings=s, journal=storage.Journal(tmp_path / "e.db"),
        feed_factory=lambda a: _Feed(FLAT), notifier=note,
    )
    return eng, note


def _active(eng, minute=4, price=1.100):
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


def test_a_refund_is_announced_immediately(tmp_path):
    eng, note = _engine(tmp_path)
    sig = _active(eng)
    eng._settle(sig, sig.expiry_at + timedelta(seconds=1))
    assert len(note.results) == 1
    assert note.results[0].refunded and note.results[0].pnl == 0.0


def test_a_refund_does_not_trigger_a_martingale_recovery(tmp_path):
    eng, note = _engine(tmp_path, martingale_enabled=True)
    sig = _active(eng)
    eng._settle(sig, sig.expiry_at + timedelta(seconds=1))
    assert not eng.scheduler.awaiting_entry(sig.expiry_at + timedelta(minutes=5))
    assert len(note.results) == 1, "the refund was withheld as if it were a loss"


def test_a_refund_starts_no_cooldown(tmp_path):
    eng, _ = _engine(tmp_path, win_cooldown_minutes=30, loss_cooldown_minutes=30,
                     cooldown_minutes=0)
    sig = _active(eng)
    eng._settle(sig, sig.expiry_at + timedelta(seconds=1))
    assert eng._cooldown_remaining("EURUSD_otc", sig.expiry_at + timedelta(seconds=2)) is None


def test_the_journal_stores_zero_pnl_for_a_refund(tmp_path):
    eng, _ = _engine(tmp_path)
    sig = _active(eng)
    eng._settle(sig, sig.expiry_at + timedelta(seconds=1))
    row = eng.journal.joined_results()[-1]
    assert row["pnl"] == 0.0
    assert analytics.is_refund(row)


# ------------------------------- the telegram message -------------------------------
def test_the_result_message_says_refund():
    from cheese_signals.notifiers.telegram import TelegramNotifier

    sent = []
    n = TelegramNotifier("t", "c")
    n.send_verbose = lambda text, **kw: (sent.append(text), (True, ""))[1]
    o = outcome_mod.settle(_sig(), 1.1000, 1.1000, 50.0, 0.85, START)
    n.send_result(o)
    assert "REFUND" in sent[-1]
    assert "LOSS" not in sent[-1]
