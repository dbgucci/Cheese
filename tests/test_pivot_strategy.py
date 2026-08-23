"""The live pivot strategy, checked against the things that lose money silently.

A backtest that is wrong prints a bad number. A live strategy that is wrong
places an order. So the tests here are about geometry and information rather
than performance: a stop on the wrong side of price, a target that is not in
profit, or a decision that moved when the future changed would each be a real
order at a real broker.
"""

import numpy as np
import pandas as pd
import pytest

from cheese_signals.markets.execution import BUY, SELL
from cheese_signals.markets.strategy import PivotConfig, PivotRetest
from cheese_signals.research.precision_reversal import driftless_walk


@pytest.fixture(scope="module")
def series():
    return driftless_walk(6000, start_price=30000.0, vol=0.0004, seed=3)


def _all_intents(df, cfg=None, step=5):
    s = PivotRetest(cfg or PivotConfig(min_bars=120))
    out = []
    for i in range(300, len(df), step):
        it = s.evaluate(df.iloc[: i + 1], df.index[i], trades_today=0)
        if it:
            out.append((df.index[i], it))
    return out


def test_it_actually_fires(series):
    assert len(_all_intents(series)) > 10


def test_stop_and_target_are_on_the_right_sides_of_price(series):
    """A stop above entry on a long is an instant loss dressed as a trade."""
    for ts, it in _all_intents(series):
        px = float(series.loc[ts, "close"])
        if it.direction == BUY:
            assert it.stop_loss < px < it.take_profit, f"{ts} long geometry"
        else:
            assert it.take_profit < px < it.stop_loss, f"{ts} short geometry"


def test_reward_to_risk_never_below_the_configured_floor(series):
    cfg = PivotConfig(min_bars=120, min_rr=2.5)
    for ts, it in _all_intents(series, cfg):
        px = float(series.loc[ts, "close"])
        rr = abs(it.take_profit - px) / abs(px - it.stop_loss)
        assert rr >= 2.5 - 1e-9


def test_the_future_cannot_change_the_decision(series):
    """The one failure that would make every measurement of this meaningless."""
    i = 3000
    now = series.index[i]
    s = PivotRetest(PivotConfig(min_bars=120))
    clean = s.evaluate(series.iloc[: i + 1], now, 0)
    poisoned = series.copy()
    poisoned.iloc[i + 1:] = 999999.0
    dirty = PivotRetest(PivotConfig(min_bars=120)).evaluate(poisoned, now, 0)
    assert (clean.direction, clean.stop_loss, clean.take_profit) == \
           (dirty.direction, dirty.stop_loss, dirty.take_profit)


def test_it_is_stateless_across_calls(series):
    """The runner fires on a timer, so the same moment must give the same answer."""
    i = 3000
    now = series.index[i]
    s = PivotRetest(PivotConfig(min_bars=120))
    for j in (500, 1500, 2500):          # warm it up with earlier calls
        s.evaluate(series.iloc[: j + 1], series.index[j], 0)
    warmed = s.evaluate(series.iloc[: i + 1], now, 0)
    fresh = PivotRetest(PivotConfig(min_bars=120)).evaluate(series.iloc[: i + 1], now, 0)
    assert (warmed.direction, warmed.stop_loss) == (fresh.direction, fresh.stop_loss)


def test_the_session_trade_cap_is_obeyed(series):
    s = PivotRetest(PivotConfig(min_bars=120, max_trades_per_session=3))
    assert not s.evaluate(series.iloc[:3001], series.index[3000], trades_today=3)


def test_short_history_refuses_rather_than_guesses(series):
    s = PivotRetest(PivotConfig(min_bars=120))
    it = s.evaluate(series.iloc[:50], series.index[49], 0)
    assert not it and "history" in it.reason


def test_the_take_profit_reaches_the_executor():
    """Intent carried a target that trader.py used to drop on the floor."""
    import inspect
    from cheese_signals.markets import trader
    src = inspect.getsource(trader)
    assert "take_profit=intent.take_profit" in src
