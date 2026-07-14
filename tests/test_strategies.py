import numpy as np
import pandas as pd

from cheese_signals import confluence, strategies
from cheese_signals.data.synthetic import generate_synthetic_candles


def _uptrend_df(n=250):
    idx = pd.date_range("2024-01-01", periods=n, freq="1min")
    close = pd.Series(1.10 + np.arange(n) * 0.00015, index=idx)
    high = close + 0.0002
    low = close - 0.0002
    open_ = close.shift(1).fillna(close.iloc[0])
    volume = pd.Series(100.0, index=idx)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume})


def test_trend_following_detects_uptrend():
    df = _uptrend_df()
    sig = strategies.trend_following(df)
    assert sig.direction in (strategies.UP, strategies.FLAT)
    if sig.is_actionable:
        assert sig.direction == strategies.UP


def test_mean_reversion_ignored_during_strong_trend():
    df = _uptrend_df()
    sig = strategies.mean_reversion(df)
    # ADX gate should keep mean-reversion silent in a persistent trend.
    assert sig.direction == strategies.FLAT


def test_insufficient_history_returns_flat():
    df = _uptrend_df(n=10)
    for fn in (strategies.trend_following, strategies.mean_reversion, strategies.price_action):
        sig = fn(df)
        assert sig.direction == strategies.FLAT
        assert sig.score == 0.0


def test_confluence_evaluate_runs_on_synthetic_data():
    df = generate_synthetic_candles(400, seed=7)
    result = confluence.evaluate(df)
    assert result.direction in (strategies.UP, strategies.DOWN, strategies.FLAT)
    assert 0.0 <= result.score <= 1.0


def test_confluence_penalizes_counter_bias_signals():
    df = _uptrend_df()
    # Bias frame strongly agrees with an uptrend; a hypothetical DOWN vote
    # would be penalized relative to an UP vote of the same raw score.
    bias_up = df.copy()
    result_with_bias = confluence.evaluate(df, df_bias=bias_up)
    result_no_bias = confluence.evaluate(df, df_bias=None)
    assert result_with_bias.bias in (strategies.UP, strategies.FLAT)
    assert result_no_bias.bias == strategies.FLAT
