"""The pivot reconstruction and its backtester, checked where it can be wrong.

The indicator tests are exact: the formula was recovered from three levels
read off a chart, so it either reproduces them to the cent or it is the wrong
formula.

The backtester tests are the ones that matter more. A stop/target system on a
driftless random walk must return zero expectancy minus costs -- that is
optional stopping, not an opinion -- so a harness that reports anything else
is measuring its own bugs. That check is here, and it is the reason the
trailing-stop default was changed after it failed once.
"""

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("scipy", reason='install with: pip install -e ".[research]"')

from cheese_signals import pivots
from cheese_signals.research import pivot_retest as pr
from cheese_signals.research.precision_reversal import driftless_walk


# --------------------------------------------------------------------------
# the indicator, against the levels actually visible in the recording
# --------------------------------------------------------------------------
def test_reproduces_the_levels_read_off_the_chart():
    lv = pivots.levels_for(30181.30, 29932.74, 30095.72)
    assert lv["R38.2"] == pytest.approx(30164.87, abs=0.01)
    assert lv["R61.8"] == pytest.approx(30223.53, abs=0.01)
    assert lv["R1"] == pytest.approx(30207.10, abs=0.01)


def test_pivot_is_the_mean_of_high_low_close():
    lv = pivots.levels_for(110.0, 90.0, 100.0)
    assert lv["P"] == pytest.approx(100.0)
    assert lv["R1"] == pytest.approx(2 * 100.0 - 90.0)
    assert lv["S1"] == pytest.approx(2 * 100.0 - 110.0)
    assert lv["R61.8"] == pytest.approx(100.0 + 0.618 * 20.0)


def test_levels_use_yesterdays_session_not_todays():
    """The single thing that would make every backtest below meaningless."""
    idx = pd.date_range("2026-08-01", periods=3, freq="1D", tz="UTC")
    daily = pd.DataFrame({"high": [110.0, 999.0, 120.0],
                          "low": [90.0, 1.0, 100.0],
                          "close": [100.0, 500.0, 110.0]}, index=idx)
    lv = pivots.daily_levels(daily)
    # Day 2's levels must come from day 1, and must not know about day 2.
    assert lv.loc[idx[1], "P"] == pytest.approx(100.0)
    # Day 3's levels come from day 2's extreme bar.
    assert lv.loc[idx[2], "P"] == pytest.approx((999.0 + 1.0 + 500.0) / 3.0)


# --------------------------------------------------------------------------
# the backtester
# --------------------------------------------------------------------------
def _null(seed=5, n=60000):
    return driftless_walk(n, start_price=30000.0, vol=0.0004, seed=seed)


def test_a_stop_loss_costs_exactly_one_r():
    bt = pr.backtest(_null(), stop_buffer_atr=2.0, spread_points=0.0,
                     trail_after_r=1e9)
    stopped = [t.r_multiple for t in bt.trades if t.reason == "stop"]
    assert len(stopped) > 50
    assert all(r == pytest.approx(-1.0) for r in stopped)


def test_zero_expectancy_on_a_driftless_walk():
    """Optional stopping: no exit rule extracts anything from a martingale."""
    bt = pr.backtest(_null(), stop_buffer_atr=2.0, spread_points=0.0,
                     trail_after_r=1e9)
    rs = np.array([t.r_multiple for t in bt.trades])
    se = rs.std(ddof=1) / np.sqrt(len(rs))
    assert abs(rs.mean()) < 3 * se, f"expectancy {rs.mean():+.4f} is {rs.mean()/se:.1f} SE from zero"


def test_win_rate_matches_the_payoff_it_is_paired_with():
    """A 26% win rate at 3.5R targets is not a bad strategy -- it is a fair coin."""
    bt = pr.backtest(_null(), stop_buffer_atr=2.0, spread_points=0.0,
                     trail_after_r=1e9)
    reward = np.mean([abs(t.target - t.entry) / abs(t.entry - t.stop) for t in bt.trades])
    fair = 1.0 / (1.0 + reward)
    assert abs(bt.win_rate - fair) < 0.06


def test_trailing_stop_buys_win_rate_with_expectancy():
    """The trade-off the bot advertises as a feature, measured."""
    kw = dict(stop_buffer_atr=2.0, spread_points=0.0)
    off = pr.backtest(_null(), trail_after_r=1e9, **kw)
    on = pr.backtest(_null(), trail_after_r=1.0, trail_atr=2.0, **kw)
    assert on.win_rate > off.win_rate + 0.10, "trailing should raise win rate"
    assert on.expectancy < off.expectancy, "and should lower expectancy"


def test_tie_break_brackets_the_result():
    kw = dict(stop_buffer_atr=1.0, spread_points=0.0, trail_after_r=1e9)
    pess = pr.backtest(_null(), tie_break="stop", **kw)
    opt = pr.backtest(_null(), tie_break="target", **kw)
    assert pess.expectancy <= opt.expectancy


def test_spread_is_charged_once_per_trade():
    kw = dict(stop_buffer_atr=2.0, trail_after_r=1e9)
    free = pr.backtest(_null(), spread_points=0.0, **kw)
    paid = pr.backtest(_null(), spread_points=10.0, **kw)
    assert paid.n == free.n
    mean_risk = np.mean([abs(t.entry - t.stop) for t in free.trades])
    assert free.expectancy - paid.expectancy == pytest.approx(10.0 / mean_risk, rel=0.1)


def test_no_lookahead_in_the_backtest():
    df = _null(n=20000)
    cut = 15000
    base = pr.backtest(df.iloc[:cut], stop_buffer_atr=2.0)
    poisoned = df.copy()
    poisoned.iloc[cut:] = 999999.0
    after = pr.backtest(poisoned.iloc[:cut], stop_buffer_atr=2.0)
    assert base.n == after.n and base.n > 0
    assert [t.entry_time for t in base.trades] == [t.entry_time for t in after.trades]


def test_skipping_a_level_raises_reward_to_risk():
    """The lever for the 1:1 problem: aim past the nearest level."""
    kw = dict(spread_points=0.0, trail_after_r=1e9)
    near = pr.backtest(_null(), target_skip=0, **kw)
    far = pr.backtest(_null(), target_skip=2, **kw)
    assert far.reward_risk > near.reward_risk * 1.5
    assert far.win_rate < near.win_rate, "further target, hit less often"


def test_min_rr_filters_out_the_cramped_setups():
    kw = dict(spread_points=0.0, trail_after_r=1e9)
    loose = pr.backtest(_null(), min_rr=0.5, **kw)
    tight = pr.backtest(_null(), min_rr=3.0, **kw)
    assert tight.n < loose.n
    assert tight.reward_risk > loose.reward_risk


def test_neither_lever_manufactures_an_edge_on_a_null():
    """Asymmetry is not edge. Both must still lose on a random walk."""
    for skip in (0, 1, 2):
        bt = pr.backtest(_null(), target_skip=skip, min_rr=2.0,
                         spread_points=0.0, trail_after_r=1e9)
        rs = np.array([t.r_multiple for t in bt.trades])
        se = rs.std(ddof=1) / np.sqrt(len(rs))
        assert rs.mean() < 3 * se, f"skip={skip} invented {rs.mean():+.3f}R"
