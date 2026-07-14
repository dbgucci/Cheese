import numpy as np
import pandas as pd

from cheese_signals import indicators as ind


def _flat_series(n=50, value=1.1):
    return pd.Series([value] * n)


def _trending_series(n=200, start=1.0, step=0.001):
    return pd.Series(start + np.arange(n) * step)


def test_rsi_flat_price_has_no_losses():
    # No up or down moves at all -> avg_loss is 0, which our implementation
    # treats as maximally strong (100), matching common RSI conventions.
    close = _flat_series()
    r = ind.rsi(close)
    assert r.iloc[-1] == 100.0


def test_rsi_uptrend_is_high():
    close = _trending_series()
    r = ind.rsi(close)
    assert r.iloc[-1] > 60


def test_rsi_downtrend_is_low():
    close = _trending_series(step=-0.001)
    r = ind.rsi(close)
    assert r.iloc[-1] < 40


def test_ema_converges_to_constant():
    close = _flat_series(value=2.0)
    e = ind.ema(close, 10)
    assert abs(e.iloc[-1] - 2.0) < 1e-9


def test_macd_columns_present():
    close = _trending_series()
    m = ind.macd(close)
    assert set(m.columns) == {"macd", "signal", "hist"}
    assert not m["macd"].iloc[-1] != m["macd"].iloc[-1]  # not NaN


def test_bollinger_bands_ordering():
    close = pd.Series(np.sin(np.linspace(0, 10, 100)) + 1.1)
    bb = ind.bollinger_bands(close, period=20)
    valid = bb.dropna()
    assert (valid["upper"] >= valid["mid"]).all()
    assert (valid["mid"] >= valid["lower"]).all()


def test_stochastic_bounds():
    high = pd.Series(np.random.default_rng(0).uniform(1.1, 1.2, 100))
    low = high - 0.01
    close = (high + low) / 2
    s = ind.stochastic(high, low, close)
    valid = s.dropna()
    assert (valid["k"] >= 0).all() and (valid["k"] <= 100).all()


def test_atr_nonnegative():
    rng = np.random.default_rng(1)
    close = pd.Series(1.1 + rng.normal(0, 0.001, 100).cumsum())
    high = close + 0.001
    low = close - 0.001
    a = ind.atr(high, low, close)
    assert (a.dropna() >= 0).all()


def test_adx_bounds():
    rng = np.random.default_rng(2)
    close = pd.Series(1.1 + np.arange(100) * 0.0005 + rng.normal(0, 0.0002, 100))
    high = close + 0.0005
    low = close - 0.0005
    a = ind.adx(high, low, close)
    valid = a.dropna()
    # DX can round to 100 + a hair of floating-point error when -DI hits 0 exactly.
    assert (valid >= 0).all() and (valid <= 100 + 1e-9).all()
