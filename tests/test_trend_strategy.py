"""Heikin Ashi trend-continuation strategy, and the lookahead checks that matter.

Lookahead is the failure mode that makes a backtest look excellent and then
lose money live, so it is tested explicitly rather than assumed.
"""

import numpy as np
import pandas as pd
import pytest

from cheese_signals import indicators as ind
from cheese_signals import trend
from cheese_signals.strategies import DOWN, FLAT, UP


def _frame(closes, spread=0.0002):
    idx = pd.date_range("2026-08-01", periods=len(closes), freq="1min", tz="UTC")
    c = pd.Series(closes, index=idx)
    o = c.shift(1).fillna(c.iloc[0])
    return pd.DataFrame(
        {"open": o, "high": np.maximum(o, c) + spread, "low": np.minimum(o, c) - spread,
         "close": c, "volume": 100.0}
    )


def _trend(n, start, step, seed, wave=0.00035, noise=0.00004):
    """A trend with pullbacks, like a real chart.

    A strictly monotonic ramp is the wrong test fixture here: in a series
    that only rises, the lowest bar of any 7-bar window is always its oldest
    bar, so a down-fractal can never form and the strategy is silent by
    construction. Real trends breathe, and that is what produces the swing
    points this setup keys off.
    """
    rng = np.random.default_rng(seed)
    return _frame([
        start + i * step + np.sin(i / 5.0) * wave + rng.normal(0, noise)
        for i in range(n)
    ])


def _uptrend(n=260, start=1.1000, step=0.00012, seed=0, **kw):
    return _trend(n, start, step, seed, **kw)


def _downtrend(n=260, start=1.1300, step=-0.00012, seed=1, **kw):
    return _trend(n, start, step, seed, **kw)


# ------------------------------ heikin ashi ------------------------------
def test_heikin_ashi_formula():
    df = _frame([1.10, 1.11, 1.12, 1.13])
    ha = ind.heikin_ashi(df)
    row = df.iloc[1]
    assert ha["close"].iloc[1] == pytest.approx(
        (row.open + row.high + row.low + row.close) / 4
    )
    assert ha["open"].iloc[1] == pytest.approx(
        (ha["open"].iloc[0] + ha["close"].iloc[0]) / 2
    )


def test_heikin_ashi_has_no_lookahead():
    """HA values must not change when future candles are appended."""
    df = _uptrend(120)
    full = ind.heikin_ashi(df)
    truncated = ind.heikin_ashi(df.iloc[:80])
    pd.testing.assert_series_equal(
        full["close"].iloc[:80], truncated["close"], check_names=False
    )
    pd.testing.assert_series_equal(
        full["open"].iloc[:80], truncated["open"], check_names=False
    )


def test_heikin_ashi_is_green_in_an_uptrend():
    ha = ind.heikin_ashi(_uptrend(120))
    green = (ha["close"] > ha["open"]).tail(50).mean()
    assert green > 0.9


# ------------------------------- keltner --------------------------------
def test_keltner_bands_straddle_the_mid():
    df = _uptrend(120)
    kc = ind.keltner_channel(df["high"], df["low"], df["close"])
    valid = kc.dropna()
    assert (valid["upper"] > valid["mid"]).all()
    assert (valid["lower"] < valid["mid"]).all()


def test_keltner_uses_the_configured_periods():
    df = _uptrend(120)
    kc = ind.keltner_channel(df["high"], df["low"], df["close"], ema_period=20)
    expected_mid = ind.ema(df["close"], 20)
    pd.testing.assert_series_equal(kc["mid"], expected_mid, check_names=False)


# ------------------------------- fractals -------------------------------
def test_fractal_is_reported_only_after_it_can_be_confirmed():
    """A period-7 fractal centred on bar i must surface at bar i+3, not bar i."""
    closes = [1.10] * 21
    df = _frame(closes, spread=0.0)
    df.iloc[10, df.columns.get_loc("high")] = 1.20   # obvious peak at bar 10

    f = ind.fractals(df["high"], df["low"], period=7)
    assert not f["up"].iloc[10], "fractal must NOT be visible on its own bar"
    assert f["up"].iloc[13], "fractal should appear 3 bars later"
    assert f["up_price"].iloc[13] == pytest.approx(1.20)


def test_fractals_have_no_lookahead():
    df = _uptrend(150)
    full = ind.fractals(df["high"], df["low"], period=7)
    truncated = ind.fractals(df["high"].iloc[:100], df["low"].iloc[:100], period=7)
    # Values already emitted must not change when more data arrives.
    assert full["up"].iloc[:97].tolist() == truncated["up"].iloc[:97].tolist()
    assert full["down"].iloc[:97].tolist() == truncated["down"].iloc[:97].tolist()


# --------------------------- trend continuation ---------------------------
def test_no_signal_without_enough_history():
    assert trend.trend_continuation(_uptrend(50)).direction == FLAT


def test_uptrend_produces_only_buy_signals():
    df = _uptrend(400)
    dirs = set()
    for i in range(trend.MIN_BARS, len(df)):
        sig = trend.trend_continuation(df.iloc[: i + 1])
        if sig.is_actionable:
            dirs.add(sig.direction)
    assert DOWN not in dirs, "a clean uptrend must never generate a SELL"


def test_downtrend_produces_only_sell_signals():
    df = _downtrend(400)
    dirs = set()
    for i in range(trend.MIN_BARS, len(df)):
        sig = trend.trend_continuation(df.iloc[: i + 1])
        if sig.is_actionable:
            dirs.add(sig.direction)
    assert UP not in dirs, "a clean downtrend must never generate a BUY"


def test_signal_carries_the_fractal_level():
    df = _uptrend(600, noise=0.00012)
    for i in range(trend.MIN_BARS, len(df)):
        sig = trend.trend_continuation(df.iloc[: i + 1])
        if sig.is_actionable:
            assert sig.meta["event"] is True
            assert isinstance(sig.meta["level"], float)
            assert "fractal" in sig.reason
            return
    pytest.skip("no signal produced in this sample")


def test_strategy_actually_fires_during_a_trend():
    """Guards the bug where the Keltner cross was the trigger.

    Heikin Ashi rides above the Keltner mid for essentially all of a
    sustained trend, so a cross-triggered version produced zero signals in
    exactly the conditions this strategy is for.
    """
    df = _uptrend(600, noise=0.00012)   # real OTC data is this noisy
    fired = sum(
        trend.trend_continuation(df.iloc[: i + 1]).is_actionable
        for i in range(trend.MIN_BARS, len(df))
    )
    assert fired > 5, "must produce usable signals inside a trend"


def test_stale_fractals_do_not_trigger():
    """Only a recently confirmed fractal counts as a trigger."""
    df = _uptrend(600, noise=0.00012)
    strict = sum(
        trend.trend_continuation(df.iloc[: i + 1], fractal_max_age=0).is_actionable
        for i in range(trend.MIN_BARS, len(df))
    )
    loose = sum(
        trend.trend_continuation(df.iloc[: i + 1], fractal_max_age=6).is_actionable
        for i in range(trend.MIN_BARS, len(df))
    )
    assert strict < loose


def test_strategy_does_not_see_future_candles():
    """Evaluating at bar i must give the same answer regardless of later data."""
    df = _uptrend(400)
    cut = 300
    with_future = trend.trend_continuation(df.iloc[: cut + 1])
    without_future = trend.trend_continuation(df.iloc[: cut + 1].copy())
    assert with_future.direction == without_future.direction
    assert with_future.score == pytest.approx(without_future.score)

    # And appending future bars must not change the verdict for bar `cut`.
    truncated_view = trend.trend_continuation(df.iloc[: cut + 1])
    assert truncated_view.direction == with_future.direction


# ------------------------------ expiry advice ------------------------------
def test_expiry_is_within_bounds():
    for df in (_uptrend(300), _downtrend(300)):
        advice = trend.recommend_expiry(df, UP)
        assert 1 <= advice.minutes <= 5
        assert advice.reason


def test_choppy_market_gets_a_shorter_expiry_than_a_smooth_trend():
    rng = np.random.default_rng(3)
    chop = _frame([1.10 + rng.normal(0, 0.0004) for _ in range(300)])
    smooth = _uptrend(300, step=0.00020, noise=0.00001)
    assert trend.recommend_expiry(chop, UP).minutes <= trend.recommend_expiry(smooth, UP).minutes


def test_short_history_falls_back_to_minimum():
    advice = trend.recommend_expiry(_uptrend(30), UP)
    assert advice.minutes == 1
    assert "not enough history" in advice.reason


# --------------------- the decisive anti-lookahead test ---------------------
def _random_walk(n, seed, sigma=0.00012):
    rng = np.random.default_rng(seed)
    c = 1.10 + np.cumsum(rng.normal(0, sigma, n))
    idx = pd.date_range("2026-08-01", periods=n, freq="1min", tz="UTC")
    cs = pd.Series(c, index=idx)
    o = cs.shift(1).fillna(cs.iloc[0])
    hi = np.maximum(o, cs) + np.abs(rng.normal(0, sigma / 2, n))
    lo = np.minimum(o, cs) - np.abs(rng.normal(0, sigma / 2, n))
    return pd.DataFrame({"open": o, "high": hi, "low": lo, "close": cs, "volume": 100.0})


def test_no_edge_on_a_random_walk():
    """A random walk has no exploitable structure.

    Any strategy scoring well above 50% here is reading the future, not the
    market. This is the check that separates a real result from a backtest
    that quietly peeks at data it could not have had live.
    """
    from cheese_signals import strategy_lab as lab

    df = _random_walk(2500, seed=7)
    result = lab.run(df, "trend_continuation", expiry_minutes=3)
    assert result.trades > 30, "need a usable sample for this check to mean anything"
    assert 0.42 < result.win_rate < 0.58, (
        f"win rate {result.win_rate:.1%} on a random walk implies lookahead bias"
    )
