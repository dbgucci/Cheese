"""The cost wall gate.

This is the check that the Pocket Option project never had. There, the house
edge was 2.2 points wider than any edge the signals could produce, and 1,543
trades were needed to establish something that arithmetic could have said up
front. On CFDs the same quantity is measurable before a strategy exists, so
it is measured first and the rest of the package sits behind it.
"""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from cheese_signals.markets import costs
from cheese_signals.markets.mt5_bridge import (
    M1, ReplayFeed, SymbolSpec, fetch_all, normalise,
)


def _bars(n=2000, step=1.0, spread=2.0, seed=0, start="2026-01-05 08:00"):
    """A random walk with a fixed spread, so the ratio is analytically known."""
    rng = np.random.default_rng(seed)
    close = 10_000 + np.cumsum(rng.normal(0, step, n))
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {"open": close, "high": close + step, "low": close - step,
         "close": close, "spread": float(spread), "tick_volume": 100.0},
        index=idx,
    )


# ------------------------------ the arithmetic ------------------------------
def test_round_trip_charges_the_spread_in_full():
    """Halving the spread is the usual way a doomed strategy looks viable."""
    c = costs.measure(_bars(spread=2.0), "NAS100", horizon_minutes=5)
    assert c.round_trip_points == pytest.approx(2.0)


def test_commission_is_added_to_the_spread():
    c = costs.measure(_bars(spread=2.0), "NAS100", 5, commission_points=1.5)
    assert c.round_trip_points == pytest.approx(3.5)


def test_a_longer_horizon_offers_a_bigger_move_against_the_same_cost():
    """The core reason scalping is the hardest version of the problem."""
    bars = _bars(n=4000)
    short = costs.measure(bars, "NAS100", 1)
    long_ = costs.measure(bars, "NAS100", 60)
    assert long_.move_points_median > short.move_points_median
    assert long_.ratio > short.ratio
    # A random walk's expected absolute move grows with the square root of
    # time, so the ratio does too -- cost is fixed, opportunity is not.
    assert long_.ratio / short.ratio > 3


def test_the_move_measured_is_close_to_close_not_the_bar_range():
    """Using the high-low range credits a strategy with the perfect exit."""
    bars = _bars(n=500)
    c = costs.measure(bars, "X", 10)
    reach = (bars.close.shift(-10) - bars.close).abs().median()
    assert c.move_points_median == pytest.approx(reach, rel=1e-9)


# ------------------------------ the verdicts ------------------------------
def test_a_wide_spread_on_a_quiet_instrument_is_unviable():
    c = costs.measure(_bars(step=0.05, spread=4.0), "EURGBP", 1)
    assert c.ratio < costs.COST_RATIO_UNVIABLE
    assert c.verdict == "unviable"


def test_a_tight_spread_on_a_moving_instrument_is_viable():
    c = costs.measure(_bars(step=3.0, spread=1.0), "NAS100", 30)
    assert c.verdict == "viable"


def test_the_break_even_win_rate_is_the_number_that_killed_the_last_project():
    """Stated in the same units as the 54.05% that made binaries unwinnable."""
    # Cost equal to a quarter of the typical move.
    c = costs.measure(_bars(step=1.0, spread=0.5), "X", 60)
    be = c.breakeven_win_rate
    assert 0.5 < be < 1.0
    # Free trading would be a coin flip; cost pushes the requirement up.
    free = costs.measure(_bars(step=1.0, spread=0.0), "X", 60)
    assert free.breakeven_win_rate == pytest.approx(0.5)
    assert be > free.breakeven_win_rate


def test_break_even_rises_as_the_horizon_shortens():
    bars = _bars(n=4000, step=1.0, spread=2.0)
    assert (costs.measure(bars, "X", 1).breakeven_win_rate
            > costs.measure(bars, "X", 60).breakeven_win_rate)


# ------------------------------ session filtering ------------------------------
def test_measuring_only_the_hours_that_will_be_traded():
    """A spread averaged over the dead session describes a market nobody trades."""
    bars = _bars(n=1440, start="2026-01-05 00:00")
    bars.loc[bars.index.hour < 8, "spread"] = 40.0     # overnight widening
    all_day = costs.measure(bars, "US30", 15)
    london = costs.measure(bars, "US30", 15, session_hours=(8, 17))
    assert london.bars == 9 * 60 < all_day.bars
    assert london.spread_points_p90 == 2.0
    assert all_day.spread_points_p90 == 40.0


def test_the_median_spread_hides_overnight_widening_and_p90_does_not():
    """Why both are reported.

    Eight hours of a twenty-point spread do not move a twenty-four hour
    median at all -- it reads exactly as if they never happened. A strategy
    sized on that median gets a fifth of its trades filled at a cost it was
    never told about, which is the quiet version of the mistake this whole
    module exists to prevent.
    """
    bars = _bars(n=1440, start="2026-01-05 00:00")
    bars.loc[bars.index.hour < 8, "spread"] = 40.0
    c = costs.measure(bars, "US30", 15)
    assert c.spread_points_median == 2.0, "the median saw nothing"
    assert c.spread_points_p90 == 40.0, "the p90 is what shows it"


def test_a_session_window_that_wraps_midnight():
    bars = _bars(n=1440, start="2026-01-05 00:00")
    c = costs.measure(bars, "USDJPY", 15, session_hours=(22, 6))
    assert c.bars == 8 * 60


def test_an_empty_window_is_an_error_not_a_silent_zero():
    bars = _bars(n=120, start="2026-01-05 08:00")
    with pytest.raises(ValueError, match="no bars"):
        costs.measure(bars, "X", 5, session_hours=(20, 21))


# ------------------------------ spread sources ------------------------------
def test_bid_ask_columns_are_accepted_when_there_is_no_spread_column():
    bars = _bars(n=200).drop(columns=["spread"])
    bars["bid"] = bars.close
    bars["ask"] = bars.close + 0.03
    sp = costs.spread_points(bars, point=0.01)
    assert sp.median() == pytest.approx(3.0)


def test_a_feed_with_no_cost_information_refuses_to_guess():
    bars = _bars(n=50).drop(columns=["spread"])
    with pytest.raises(KeyError, match="neither"):
        costs.spread_points(bars)


def test_spread_by_hour_finds_the_expensive_hours():
    bars = _bars(n=1440, start="2026-01-05 00:00")
    bars.loc[bars.index.hour == 22, "spread"] = 30.0
    table = costs.spread_by_hour(bars)
    assert table.loc[22, "median"] == 30.0
    assert table.loc[10, "median"] == 2.0
    assert table["bars"].sum() == 1440


# ------------------------------ the decision table ------------------------------
def test_the_sweep_covers_every_instrument_and_horizon():
    frames = {"NAS100": _bars(n=3000, step=3.0, spread=1.5),
              "EURGBP": _bars(n=3000, step=0.05, spread=1.2, seed=1)}
    table = costs.sweep(frames, horizons=(1, 15, 60))
    assert len(table) == 6
    assert set(table.symbol) == {"NAS100", "EURGBP"}
    assert list(table.columns[:4]) == ["symbol", "horizon_min", "cost_pts", "spread_p90"]


def test_the_sweep_ratio_rises_with_the_horizon_for_each_instrument():
    frames = {"NAS100": _bars(n=4000, step=2.0, spread=2.0)}
    table = costs.sweep(frames, horizons=(1, 5, 30, 120))
    assert table.ratio.is_monotonic_increasing


def test_the_report_says_do_not_trade_this_when_nothing_clears():
    frames = {"EURGBP": _bars(n=2000, step=0.02, spread=5.0)}
    lines = costs.verdict_lines(costs.sweep(frames, horizons=(1, 5, 15)))
    assert any("Do not trade this" in ln for ln in lines)


def test_the_report_names_the_shortest_horizon_that_works():
    frames = {"NAS100": _bars(n=6000, step=2.0, spread=2.0)}
    table = costs.sweep(frames, horizons=(1, 5, 30, 240))
    line = costs.verdict_lines(table)[0]
    shortest = table[table.verdict != "unviable"].horizon_min.min()
    assert f"viable from {shortest}min" in line


def test_verdict_lines_on_an_empty_table_say_so():
    assert costs.verdict_lines(pd.DataFrame()) == ["No cost measurements available."]


# ------------------------------ the broker bridge ------------------------------
def test_mt5_rates_normalise_to_utc_open_stamped_bars():
    raw = pd.DataFrame({
        "time": [1767600000, 1767600060],       # unix seconds
        "open": [1.0, 2.0], "high": [1.5, 2.5], "low": [0.5, 1.5],
        "close": [1.2, 2.2], "tick_volume": [10, 20], "spread": [2, 3],
        "real_volume": [0, 0],
    })
    df = normalise(raw)
    assert df.index.tz is not None and str(df.index.tz) == "UTC"
    assert df.index[0] == pd.Timestamp("2026-01-05 08:00:00", tz="UTC")
    assert (df.index[1] - df.index[0]).total_seconds() == 60
    assert list(df.columns) == ["open", "high", "low", "close", "tick_volume",
                                "spread", "real_volume"]


def test_the_replay_feed_satisfies_the_broker_interface():
    frames = {"XAUUSD": _bars(n=100)}
    feed = ReplayFeed(frames, {"XAUUSD": SymbolSpec(
        name="XAUUSD", point=0.01, digits=2, contract_size=100.0, tick_value=1.0,
        volume_min=0.01, volume_step=0.01, volume_max=50.0, spread_current=25.0)})
    assert feed.symbols() == ["XAUUSD"]
    assert feed.spec("XAUUSD").point == 0.01
    got = feed.history("XAUUSD", M1,
                       datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc),
                       datetime(2026, 1, 5, 8, 30, tzinfo=timezone.utc))
    assert len(got) == 31


def test_fetch_all_names_the_symbols_it_could_not_get():
    """A symbol the broker does not offer must not vanish from the study."""
    class _Partial(ReplayFeed):
        def history(self, symbol, timeframe, start, end):
            if symbol == "US30":
                raise KeyError("not available on this account")
            return super().history(symbol, timeframe, start, end)

    feed = _Partial({"NAS100": _bars(n=100), "SPX500": _bars(n=100)})
    frames, problems = _Partial.history and fetch_all(
        feed, ["NAS100", "US30", "SPX500"],
        datetime(2026, 1, 5, tzinfo=timezone.utc),
        datetime(2026, 1, 6, tzinfo=timezone.utc), M1)
    assert set(frames) == {"NAS100", "SPX500"}
    assert len(problems) == 1 and "US30" in problems[0]


def test_an_empty_range_is_reported_rather_than_returned_as_success():
    feed = ReplayFeed({"NAS100": _bars(n=100, start="2026-01-05 08:00")})
    frames, problems = fetch_all(
        feed, ["NAS100"],
        datetime(2020, 1, 1, tzinfo=timezone.utc),
        datetime(2020, 1, 2, tzinfo=timezone.utc), M1)
    assert frames == {}
    assert problems and "no bars" in problems[0]
