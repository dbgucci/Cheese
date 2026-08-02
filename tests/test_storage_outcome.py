from datetime import datetime, timedelta, timezone

import pytest

from cheese_signals import analytics, outcome as om, sessions, storage
from cheese_signals.data.synthetic import generate_synthetic_candles
from cheese_signals.scheduler import schedule_signal
from cheese_signals.strategies import DOWN, UP


@pytest.fixture
def journal(tmp_path):
    j = storage.Journal(tmp_path / "test.db")
    yield j
    j.close()


def _sig(direction=UP, score=0.8, strategy="liquidity_sweep"):
    now = datetime(2026, 8, 1, 14, 30, tzinfo=timezone.utc)
    s = schedule_signal("EURUSD_otc", direction, score, strategy, "reason", now, "overlap")
    s.features = {"adx": 26.0, "displacement_atr": 0.9, "bias_aligned": True}
    return s


def test_candles_roundtrip(journal):
    df = generate_synthetic_candles(80, seed=3)
    n = journal.record_candles("EURUSD_otc", df)
    assert n == 80
    assert journal.candle_count("EURUSD_otc") == 80
    back = journal.load_candles("EURUSD_otc", 80)
    assert len(back) == 80
    assert list(back.columns) == ["open", "high", "low", "close", "volume"]


def test_candles_upsert_is_idempotent(journal):
    df = generate_synthetic_candles(40, seed=4)
    journal.record_candles("EURUSD_otc", df)
    journal.record_candles("EURUSD_otc", df)
    assert journal.candle_count("EURUSD_otc") == 40


def test_signal_and_outcome_roundtrip(journal):
    s = _sig()
    sid = journal.record_signal(
        s.asset, s.direction, s.score, s.strategy, s.reason,
        s.detected_at, s.entry_at, s.expiry_at, s.session, 14, s.features,
    )
    assert journal.summary_counts()["pending"] == 1

    journal.record_outcome(
        sid, s.asset, s.direction, 1.1000, 1.1004, True, 0.85, 10.0,
        s.expiry_at, "Win: reason",
    )
    counts = journal.summary_counts()
    assert counts == {"settled": 1, "wins": 1, "losses": 0, "pending": 0}

    rows = journal.joined_results()
    assert len(rows) == 1
    assert rows[0]["won"] == 1
    assert rows[0]["pnl"] == pytest.approx(8.5)
    assert rows[0]["features"]["adx"] == 26.0


def test_cancelled_signal_not_counted_as_pending(journal):
    s = _sig()
    sid = journal.record_signal(
        s.asset, s.direction, s.score, s.strategy, s.reason,
        s.detected_at, s.entry_at, s.expiry_at, s.session, 14, s.features,
    )
    journal.set_signal_status(sid, "cancelled", "flipped")
    assert journal.summary_counts()["pending"] == 0


# ------------------------------- outcomes -------------------------------
def test_settle_win_and_loss():
    s = _sig(direction=UP)
    now = datetime.now(timezone.utc)
    win = om.settle(s, 1.1000, 1.1005, stake=10, payout=0.85, settled_at=now)
    assert win.won and win.pnl == pytest.approx(8.5)

    loss = om.settle(s, 1.1000, 1.0995, stake=10, payout=0.85, settled_at=now)
    assert not loss.won and loss.pnl == pytest.approx(-10.0)


def test_flat_close_is_a_loss():
    s = _sig(direction=UP)
    now = datetime.now(timezone.utc)
    res = om.settle(s, 1.1000, 1.1000, stake=10, payout=0.85, settled_at=now)
    assert not res.won
    assert "flat close" in res.reason


def test_attribution_mentions_key_conditions():
    s = _sig(direction=UP)
    s.features["bias_aligned"] = False
    reason = om.attribute(s, 1.1000, 1.0995, won=False)
    assert reason.startswith("Loss:")
    assert "against the higher-timeframe bias" in reason
    assert "displacement" in reason


# ------------------------------ analytics -------------------------------
def _rows(n, win_rate=0.6, strategy="liquidity_sweep"):
    """Rows shaped like joined_results(), with reasons from the real attributor."""
    out = []
    for i in range(n):
        won = i < int(n * win_rate)
        s = _sig(strategy=strategy)
        s.features["displacement_atr"] = 0.55  # a condition worth surfacing
        entry, exit_ = 1.1000, (1.1005 if won else 1.0995)
        out.append({
            "asset": "EURUSD_otc", "direction": 1, "score": 0.7,
            "strategy": strategy, "session": "overlap", "utc_hour": 14,
            "lead_seconds": 120, "won": int(won),
            "pnl": 8.5 if won else -10.0,
            "outcome_reason": om.attribute(s, entry, exit_, won),
        })
    return out


def test_analytics_is_silent_on_small_samples():
    tips = analytics.suggestions(_rows(5))
    assert len(tips) == 1
    assert "Collect at least" in tips[0]


def test_analytics_reports_on_sufficient_samples():
    tips = analytics.suggestions(_rows(100, win_rate=0.62))
    assert any("Overall" in t for t in tips)


def test_breakeven_math():
    assert analytics.breakeven_win_rate(0.85) == pytest.approx(1 / 1.85)


def test_slice_significance_threshold():
    small = analytics.Slice("x", trades=5, wins=3, pnl=1.0)
    big = analytics.Slice("y", trades=100, wins=60, pnl=10.0)
    assert not small.is_significant
    assert big.is_significant
    assert big.win_rate == pytest.approx(0.6)


def test_loss_reasons_extracts_conditions():
    reasons = analytics.loss_reasons(_rows(20, win_rate=0.0))
    assert reasons
    assert any("displacement" in r for r, _ in reasons)


# ------------------------------- sessions -------------------------------
def test_otc_is_always_open():
    saturday = datetime(2026, 8, 1, 3, 0, tzinfo=timezone.utc)
    assert sessions.market_is_open("EURUSD_otc", saturday)
    assert not sessions.market_is_open("EURUSD", saturday)


def test_overlap_detection():
    assert sessions.is_overlap(datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc))
    assert not sessions.is_overlap(datetime(2026, 8, 3, 3, 0, tzinfo=timezone.utc))
    assert sessions.session_label(
        datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc)
    ) == "london_ny_overlap"


# ------------------------------ drift check ------------------------------
def _priced_rows(n, up_fraction, win_fraction):
    """Rows with entry/exit prices so drift_check can measure the base rate."""
    out = []
    for i in range(n):
        rose = i < int(n * up_fraction)
        won = i < int(n * win_fraction)
        out.append({
            "asset": "EURUSD_otc", "direction": 1, "score": 0.7,
            "strategy": "liquidity_sweep", "session": "london", "utc_hour": 12,
            "lead_seconds": 120, "won": int(won),
            "pnl": 8.5 if won else -10.0,
            "outcome_reason": "x",
            "entry_price": 1.1000,
            "exit_price": 1.1005 if rose else 1.0995,
        })
    return out


def test_drift_check_flags_a_directional_sample():
    # Price rose in 65% of windows, strategy only won 48% -> no skill.
    tips = analytics.drift_check(_priced_rows(100, up_fraction=0.65, win_fraction=0.48))
    joined = " ".join(tips)
    assert "65%" in joined
    assert "always BUY" in joined
    assert "did NOT beat" in joined
    assert "reflect which way price happened to move" in joined


def test_drift_check_quiet_on_a_balanced_sample():
    tips = analytics.drift_check(_priced_rows(100, up_fraction=0.50, win_fraction=0.60))
    joined = " ".join(tips)
    assert "did NOT beat" not in joined
    assert "suspicion" not in joined


def test_drift_check_needs_a_sample():
    assert analytics.drift_check(_priced_rows(5, 0.65, 0.48)) == []


def test_drift_check_ignores_flat_settlements():
    rows = _priced_rows(40, 0.5, 0.5)
    for r in rows[:10]:
        r["exit_price"] = r["entry_price"]      # bug-contaminated flats
    tips = analytics.drift_check(rows)
    assert "30 minutes you traded" in " ".join(tips)
