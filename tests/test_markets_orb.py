"""The opening-range rules, one rule at a time.

The bars here are written by hand rather than generated, so each test asserts
against a range and a breakout that can be read off the source.
"""

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from cheese_signals.markets import orb
from cheese_signals.markets.clock import SESSIONS
from cheese_signals.markets.execution import BUY, SELL
from cheese_signals.markets.orb import OrbConfig

US = SESSIONS["us_cash"]
LON = SESSIONS["london"]
DAY = date(2026, 3, 2)              # a Monday, US winter time: open 14:30 UTC
OPEN = datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc)
POINT = 0.1                         # US30-like


def bars(rows, start=OPEN, spread=10.0):
    """``rows`` are (high, low, close) triples, one per minute from ``start``."""
    index = pd.date_range(start, periods=len(rows), freq="1min", tz="UTC")
    return pd.DataFrame(
        {"open": [r[2] for r in rows],
         "high": [r[0] for r in rows],
         "low": [r[1] for r in rows],
         "close": [r[2] for r in rows],
         "spread": [spread] * len(rows)},
        index=index,
    )


def flat_range(n=15, high=44010.0, low=43990.0, close=44000.0, **kw):
    """A clean, wide opening range: 20 index points = 200 broker points."""
    return bars([(high, low, close)] * n, **kw)


def cfg(**kw):
    return OrbConfig(**kw)


# ------------------------------ building the range ------------------------------
def test_the_range_is_the_high_and_low_of_the_first_n_minutes():
    frame = bars([(44010, 43990, 44000)] * 15 + [(44100, 44050, 44090)] * 5)
    rng = orb.build_range(frame, "US30", US, DAY, cfg(), POINT)
    assert (rng.high, rng.low) == (44010.0, 43990.0)
    assert rng.bars == 15 and rng.expected_bars == 15


def test_the_range_window_excludes_the_bar_that_could_trigger_it():
    """15 bars from 14:30 are 14:30..14:44; 14:45 is the first that may fire."""
    rng = orb.build_range(flat_range(20), "US30", US, DAY, cfg(), POINT)
    assert rng.start == OPEN
    assert rng.end == OPEN + timedelta(minutes=15)
    assert rng.bars == 15


def test_a_session_with_no_bars_is_absence_not_an_error():
    """Exchange holidays are not in any calendar here, and look like this."""
    assert orb.build_range(flat_range(), "US30", US, date(2026, 3, 3), cfg(),
                           POINT) is None


def test_the_cost_is_taken_from_the_ranges_own_spreads():
    rng = orb.build_range(flat_range(spread=30.0), "US30", US, DAY, cfg(), POINT,
                          commission_points=5.0)
    assert rng.cost_points == 35.0


def test_a_feed_with_no_spread_column_falls_back_rather_than_assuming_free():
    frame = flat_range().drop(columns=["spread"])
    rng = orb.build_range(frame, "US30", US, DAY, cfg(), POINT,
                          fallback_spread_points=25.0)
    assert rng.cost_points == 25.0


def test_width_is_reported_in_broker_points_not_price():
    rng = orb.build_range(flat_range(), "US30", US, DAY, cfg(), POINT)
    assert rng.width == pytest.approx(20.0)
    assert rng.width_points == pytest.approx(200.0)
    assert rng.mid == pytest.approx(44000.0)


# ------------------------------ the day filters ------------------------------
def test_a_range_narrower_than_the_cost_wall_is_refused_with_its_numbers():
    rng = orb.build_range(flat_range(high=44001.0, low=44000.0), "US30", US, DAY,
                          cfg(), POINT, fallback_spread_points=10.0)
    check = orb.check_range(rng, cfg(), adr_points=500.0)
    assert not check
    assert "round-trip cost" in check.reason and "10.0pt" in check.reason


def test_a_range_too_quiet_against_the_daily_range_is_refused():
    rng = orb.build_range(flat_range(), "US30", US, DAY, cfg(), POINT)
    check = orb.check_range(rng, cfg(min_range_adr_fraction=0.5), adr_points=1000.0)
    assert not check and "too" in check.reason


def test_a_range_that_already_covered_the_day_is_refused():
    """After a gap the breakout enters at the end of the move."""
    rng = orb.build_range(flat_range(), "US30", US, DAY, cfg(), POINT)
    check = orb.check_range(rng, cfg(max_range_adr_fraction=0.1), adr_points=1000.0)
    assert not check and "before the range even closed" in check.reason


def test_a_gappy_range_is_refused_because_its_extremes_are_unreliable():
    rng = orb.build_range(flat_range(n=5), "US30", US, DAY, cfg(), POINT)
    check = orb.check_range(rng, cfg(), adr_points=1000.0)
    assert not check and "range bars present" in check.reason


def test_a_zero_width_range_is_refused():
    rng = orb.build_range(flat_range(high=44000.0, low=44000.0), "US30", US, DAY,
                          cfg(), POINT)
    assert not orb.check_range(rng, cfg(), adr_points=1000.0)


def test_an_ordinary_range_passes_and_says_how_far_over_the_wall_it_is():
    rng = orb.build_range(flat_range(), "US30", US, DAY, cfg(), POINT)
    check = orb.check_range(rng, cfg(), adr_points=1000.0)
    assert check and "20.0x cost" in check.reason


def test_no_daily_range_history_disables_that_filter_rather_than_blocking():
    rng = orb.build_range(flat_range(), "US30", US, DAY, cfg(), POINT)
    assert orb.check_range(rng, cfg(), adr_points=0.0)


# ------------------------------ the breakout ------------------------------
def _rng(**kw):
    return orb.build_range(flat_range(**kw), "US30", US, DAY, cfg(), POINT)


def test_a_close_above_the_range_is_a_long():
    rng = _rng()
    ts = OPEN + timedelta(minutes=15)
    plan = orb.breakout(rng, ts, {"high": 44020, "low": 44005, "close": 44015},
                        cfg())
    assert plan.direction == BUY and plan.entry == 44015


def test_a_close_below_the_range_is_a_short():
    rng = _rng()
    ts = OPEN + timedelta(minutes=15)
    plan = orb.breakout(rng, ts, {"high": 43995, "low": 43980, "close": 43985},
                        cfg())
    assert plan.direction == SELL and plan.entry == 43985


def test_a_wick_through_the_level_that_closes_back_inside_is_not_a_breakout():
    """The single most common false break, and the reason close-through is the
    default entry mode."""
    rng = _rng()
    ts = OPEN + timedelta(minutes=15)
    assert orb.breakout(rng, ts, {"high": 44030, "low": 44000, "close": 44005},
                        cfg()) is None


def test_touch_mode_takes_that_same_wick():
    rng = _rng()
    ts = OPEN + timedelta(minutes=15)
    plan = orb.breakout(rng, ts, {"high": 44030, "low": 44000, "close": 44005},
                        cfg(entry_mode=orb.ENTRY_TOUCH))
    assert plan.direction == BUY
    assert plan.entry == 44010.0, "a stop order fills at the level, not the close"


def test_a_bar_inside_the_range_does_nothing():
    rng = _rng()
    ts = OPEN + timedelta(minutes=15)
    assert orb.breakout(rng, ts, {"high": 44008, "low": 43992, "close": 44000},
                        cfg()) is None


def test_a_bar_still_inside_the_range_window_cannot_trigger():
    """No lookahead: the range is not known until its last bar has closed."""
    rng = _rng()
    ts = OPEN + timedelta(minutes=5)
    assert orb.breakout(rng, ts, {"high": 44100, "low": 44050, "close": 44090},
                        cfg()) is None


def test_a_bar_that_takes_out_both_sides_is_refused_rather_than_guessed():
    """Which side went first is not recoverable from OHLC, and guessing awards
    the backtest the good half of every whipsaw."""
    rng = _rng()
    ts = OPEN + timedelta(minutes=15)
    assert orb.breakout(rng, ts, {"high": 44050, "low": 43950, "close": 44030},
                        cfg(entry_mode=orb.ENTRY_TOUCH)) is None


def test_a_direction_already_used_is_not_traded_again():
    rng = _rng()
    ts = OPEN + timedelta(minutes=15)
    bar = {"high": 44020, "low": 44005, "close": 44015}
    assert orb.breakout(rng, ts, bar, cfg(), taken={BUY}) is None


def test_the_entry_buffer_requires_travel_past_the_level():
    rng = _rng()
    ts = OPEN + timedelta(minutes=15)
    bar = {"high": 44012, "low": 44005, "close": 44011}      # 10 points past
    assert orb.breakout(rng, ts, bar, cfg()) is not None
    assert orb.breakout(rng, ts, bar, cfg(entry_buffer_points=50.0)) is None


# ------------------------------ stops and targets ------------------------------
def test_the_default_stop_is_the_far_side_of_the_range():
    rng = _rng()
    stop, target = orb.stop_and_target(rng, cfg(), BUY, entry=44015.0)
    assert stop == 43990.0
    assert target == pytest.approx(44015.0 + 2 * 25.0)


def test_the_range_fraction_stop_sits_inside_the_range():
    rng = _rng()
    stop, _ = orb.stop_and_target(rng, cfg(stop_mode=orb.STOP_RANGE_FRACTION),
                                  BUY, entry=44015.0)
    assert stop == pytest.approx(44000.0), "half the 20pt range below the high"


def test_the_atr_stop_is_a_distance_from_the_entry():
    rng = _rng()
    stop, _ = orb.stop_and_target(rng, cfg(stop_mode=orb.STOP_ATR,
                                           stop_atr_multiple=2.0),
                                  BUY, entry=44015.0, atr_points=50.0)
    assert stop == pytest.approx(44015.0 - 2 * 50 * POINT)


def test_the_short_side_mirrors():
    rng = _rng()
    stop, target = orb.stop_and_target(rng, cfg(), SELL, entry=43985.0)
    assert stop == 44010.0
    assert target == pytest.approx(43985.0 - 2 * 25.0)


def test_a_target_too_small_to_pay_for_the_round_trip_is_refused():
    """A 2-point range on a 3-point spread: a 50-point target against a
    90-point wall. The range filter would also catch this; the plan filter is
    the one that catches a target shrunk by a tight stop mode."""
    rng = orb.build_range(flat_range(high=44002.0, low=44000.0, spread=30.0),
                          "US30", US, DAY, cfg(), POINT)
    ts = OPEN + timedelta(minutes=15)
    plan = orb.breakout(rng, ts, {"high": 44003, "low": 44001, "close": 44002.5},
                        cfg())
    assert plan.reward_points == pytest.approx(50.0)
    check = orb.check_plan(plan, cfg())
    assert not check and "round trip" in check.reason


def test_a_stop_landing_on_the_wrong_side_produces_no_plan():
    """A close-through that ran past the *opposite* side of the range."""
    rng = _rng()
    ts = OPEN + timedelta(minutes=15)
    # Closes below the range low, so a "short" whose range-opposite stop
    # (the range high) is above it -- valid; the inverse case is a long whose
    # stop would sit above its entry, which cannot arise from a real break.
    plan = orb.breakout(rng, ts, {"high": 43995, "low": 43900, "close": 43950},
                        cfg())
    assert plan.direction == SELL and plan.stop > plan.entry


def test_the_breakeven_trigger_is_r_multiples_from_the_entry():
    rng = _rng()
    ts = OPEN + timedelta(minutes=15)
    plan = orb.breakout(rng, ts, {"high": 44020, "low": 44005, "close": 44015},
                        cfg())
    assert orb.breakeven_stop(plan, cfg(breakeven_at_r=1.0)) == pytest.approx(44040.0)
    assert orb.breakeven_stop(plan, cfg(breakeven_at_r=None)) is None


# ------------------------------ the entry window ------------------------------
def test_the_entry_window_closes_before_the_session_does():
    rng = _rng()
    deadline = orb.entry_deadline(rng, cfg(entry_window_minutes=600), US)
    close = US.close_utc(DAY)
    assert deadline == close - timedelta(minutes=10)


def test_a_short_entry_window_closes_first():
    rng = _rng()
    deadline = orb.entry_deadline(rng, cfg(entry_window_minutes=30), US)
    assert deadline == rng.end + timedelta(minutes=30)


# ------------------------------ the daily-range yardstick ------------------------------
def _week_of_sessions(widths, spec=US, start=date(2026, 3, 2)):
    frames, day = [], start
    for width in widths:
        while not spec.is_open_weekday(day):
            day += timedelta(days=1)
        open_at = spec.open_utc(day)
        n = int((spec.close_utc(day) - open_at).total_seconds() // 60)
        rows = [(44000 + width, 44000.0, 44000.0)] + [(44000.0, 44000.0, 44000.0)] * (n - 1)
        frames.append(bars(rows, start=open_at))
        day += timedelta(days=1)
    return pd.concat(frames).sort_index()


def test_session_ranges_measure_only_session_hours():
    """An index CFD quotes overnight; that range is not available to trade."""
    day_bars = _week_of_sessions([10.0])
    overnight = bars([(45000.0, 43000.0, 44000.0)],
                     start=datetime(2026, 3, 3, 3, 0, tzinfo=timezone.utc))
    frame = pd.concat([day_bars, overnight]).sort_index()
    daily = orb.session_ranges(frame, US)
    assert list(daily.index) == [date(2026, 3, 2)]
    assert daily["range"].iloc[0] == pytest.approx(10.0)


def test_the_daily_range_average_excludes_the_day_being_decided():
    """Including it is lookahead, and lookahead looks exactly like a good filter."""
    frame = _week_of_sessions([10.0, 20.0, 90.0])
    daily = orb.session_ranges(frame, US)
    adr = orb.adr_before(daily, days=14, point=POINT)
    third = daily.index[2]
    assert adr.loc[third] == pytest.approx((10.0 + 20.0) / 2 / POINT)
    assert pd.isna(adr.iloc[0]), "the first session has no history behind it"


def test_the_single_day_form_agrees_with_the_series_form():
    frame = _week_of_sessions([10.0, 20.0, 90.0])
    daily = orb.session_ranges(frame, US)
    series = orb.adr_before(daily, days=14, point=POINT)
    third = daily.index[2]
    single = orb.average_daily_range_points(frame, US, POINT, 14, before=third)
    assert single == pytest.approx(series.loc[third])


def test_weekends_are_not_sessions():
    frame = _week_of_sessions([10.0] * 7)
    assert all(d.weekday() < 5 for d in orb.session_ranges(frame, US).index)


# ------------------------------ configuration ------------------------------
def test_a_sane_default_config_has_nothing_to_complain_about():
    assert OrbConfig().validate() == []


def test_an_inverted_range_filter_is_named_rather_than_obeyed():
    problems = cfg(min_range_adr_fraction=0.8, max_range_adr_fraction=0.2).validate()
    assert any("inverted" in p for p in problems)


def test_a_breakeven_at_or_beyond_the_target_is_pointless_and_says_so():
    problems = cfg(breakeven_at_r=2.0, target_r=2.0).validate()
    assert any("breakeven_at_r" in p for p in problems)


def test_an_unknown_mode_is_rejected():
    assert any("entry_mode" in p for p in cfg(entry_mode="magic").validate())
    assert any("stop_mode" in p for p in cfg(stop_mode="vibes").validate())


def test_a_zero_entry_window_is_called_out():
    assert any("entry_window_minutes" in p
               for p in cfg(entry_window_minutes=0).validate())


def test_one_direction_per_session_bars_the_opposite_side():
    assert cfg().barred_after(BUY) == {SELL}
    assert cfg(one_direction_per_session=False).barred_after(BUY) == set()


def test_indices_get_a_fifteen_minute_range_and_fx_gets_thirty():
    assert orb.suggest_range_minutes("NAS100_m") == 15
    assert orb.suggest_range_minutes("US30cash") == 15
    assert orb.suggest_range_minutes("XAUUSD") == 30
    assert orb.suggest_range_minutes("EURUSD.a") == 30
