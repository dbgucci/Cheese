"""The limits that can overrule the strategy.

These exist for the case where the strategy is confidently and systematically
wrong -- which is what a strategy looks like from the inside when the regime
it was fitted to has ended. No signal, however strong, can override any of
them.
"""

from datetime import datetime, timedelta, timezone

import pytest

from cheese_signals.markets.guards import GuardConfig, Guards

UTC = timezone.utc
MON = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)      # a Monday afternoon
SAT = datetime(2026, 3, 7, 14, 0, tzinfo=UTC)


def _g(**cfg):
    g = Guards(GuardConfig(**cfg))
    g.start_day(MON, 10_000.0)
    return g


def _open(g, now=MON, equity=10_000.0, symbol="US30", positions=0, news=None):
    return g.may_open(now, equity, symbol, positions, news)


# ------------------------------ the daily loss limit ------------------------------
def test_the_daily_loss_limit_halts_trading():
    """The difference between a bad day and a bad month."""
    g = _g(max_daily_loss_fraction=0.03)
    assert _open(g, equity=9_800.0)              # -2%, still allowed
    d = _open(g, equity=9_700.0)                 # -3%
    assert not d and "limit is 3.00%" in d.reason


def test_the_halt_persists_after_equity_recovers():
    """A limit that un-trips on a bounce is not a limit."""
    g = _g(max_daily_loss_fraction=0.03)
    _open(g, equity=9_600.0)
    d = _open(g, equity=10_500.0)
    assert not d and "halted for the day" in d.reason


def test_a_new_day_clears_the_halt_and_rebases_the_limit():
    g = _g(max_daily_loss_fraction=0.03)
    _open(g, equity=9_600.0)
    assert not _open(g, equity=9_600.0)
    tomorrow = MON + timedelta(days=1)
    assert _open(g, now=tomorrow, equity=9_600.0)
    assert g.state.start_equity == 9_600.0, "the new day's limit is off the new equity"


def test_the_absolute_equity_floor_halts_regardless_of_the_daily_move():
    g = _g(min_equity=5_000.0, max_daily_loss_fraction=0.99)
    d = _open(g, equity=4_999.0)
    assert not d and "floor" in d.reason


# ------------------------------ the other breakers ------------------------------
def test_consecutive_losses_halt_the_day():
    g = _g(consecutive_losses_halt=3)
    for _ in range(3):
        g.record_close(-50.0)
    d = _open(g)
    assert not d and "3 losses in a row" in d.reason


def test_a_win_resets_the_losing_streak():
    g = _g(consecutive_losses_halt=3)
    g.record_close(-50.0); g.record_close(-50.0); g.record_close(+80.0)
    assert _open(g)
    assert g.state.consecutive_losses == 0


def test_the_daily_trade_cap_stops_overtrading():
    g = _g(max_trades_per_day=2)
    g.record_open(); g.record_open()
    d = _open(g)
    assert not d and "limit is 2" in d.reason
    # Unlike a halt, this is a cap rather than a circuit breaker: tomorrow is fine.
    assert _open(g, now=MON + timedelta(days=1))


def test_the_open_position_cap_is_enforced():
    g = _g(max_open_positions=2)
    assert _open(g, positions=1)
    assert not _open(g, positions=2)


# ------------------------------ time and session ------------------------------
def test_the_weekend_is_refused():
    g = _g()
    d = _open(g, now=SAT)
    assert not d and "weekend" in d.reason


def test_trading_hours_are_enforced_per_symbol():
    g = _g(trading_hours_utc={"US30": (13, 20)})
    assert _open(g, now=MON.replace(hour=14))
    d = _open(g, now=MON.replace(hour=9))
    assert not d and "13:00-20:00" in d.reason


def test_a_symbol_with_no_hours_configured_trades_any_time():
    g = _g(trading_hours_utc={"US30": (13, 20)})
    assert _open(g, now=MON.replace(hour=3), symbol="XAUUSD")


def test_no_new_trades_close_to_the_session_close():
    g = _g(trading_hours_utc={"US30": (13, 20)}, flat_before_close_minutes=10)
    assert _open(g, now=MON.replace(hour=19, minute=45))
    d = _open(g, now=MON.replace(hour=19, minute=55))
    assert not d and "close" in d.reason


# ------------------------------ news blackouts ------------------------------
def _event(hour, minute=30, label="US CPI"):
    t = MON.replace(hour=hour, minute=minute)
    return (t, t, label)


def test_a_high_impact_release_blocks_trading_around_it():
    g = _g(news_blackout_minutes=15)
    windows = [_event(14, 30)]
    assert not _open(g, now=MON.replace(hour=14, minute=25), news=windows)
    assert not _open(g, now=MON.replace(hour=14, minute=40), news=windows)


def test_outside_the_blackout_trading_resumes():
    g = _g(news_blackout_minutes=15)
    windows = [_event(14, 30)]
    assert _open(g, now=MON.replace(hour=14, minute=10), news=windows)
    assert _open(g, now=MON.replace(hour=14, minute=50), news=windows)


def test_the_blackout_reason_names_the_event():
    g = _g(news_blackout_minutes=15)
    d = _open(g, now=MON.replace(hour=14, minute=31), news=[_event(14, 30)])
    assert "US CPI" in d.reason and "14:30" in d.reason


def test_no_scheduled_events_means_no_blackout():
    assert _open(_g(), news=[])


# ------------------------------ flattening ------------------------------
def test_a_halted_day_flattens_open_positions_too():
    """Stopping new trades while still holding protects nothing."""
    g = _g(max_daily_loss_fraction=0.03)
    _open(g, equity=9_600.0)
    d = g.must_flatten(MON, "US30", 9_600.0)
    assert d and "halted" in d.reason


def test_positions_are_flattened_at_the_session_close():
    g = _g(trading_hours_utc={"US30": (13, 20)}, flat_before_close_minutes=10)
    assert not g.must_flatten(MON.replace(hour=15), "US30", 10_000.0)
    assert g.must_flatten(MON.replace(hour=19, minute=55), "US30", 10_000.0)


def test_positions_are_flattened_before_the_weekend():
    g = _g()
    assert g.must_flatten(SAT, "US30", 10_000.0)


def test_an_ordinary_moment_does_not_flatten():
    g = _g(trading_hours_utc={"US30": (13, 20)})
    assert not g.must_flatten(MON.replace(hour=15), "US30", 10_000.0)


# ------------------------------ reporting ------------------------------
def test_the_summary_states_the_day_in_one_line():
    g = _g()
    g.record_open(); g.record_close(-120.0)
    s = g.summary(9_880.0)
    assert "1 trades" in s and "-120.00" in s and "-1.20%" in s


def test_a_halted_summary_says_so_and_why():
    g = _g(max_daily_loss_fraction=0.03)
    _open(g, equity=9_600.0)
    assert "HALTED" in g.summary(9_600.0)


def test_a_day_that_never_started_reports_rather_than_crashing():
    assert Guards().summary(10_000.0) == "no trading day started"


def test_the_first_check_starts_the_day_on_its_own():
    """A bot restarted mid-session must not trade with no limits in force."""
    g = Guards(GuardConfig(max_daily_loss_fraction=0.03))
    assert g.state is None
    assert g.may_open(MON, 8_000.0, "US30", 0)
    assert g.state is not None and g.state.start_equity == 8_000.0
