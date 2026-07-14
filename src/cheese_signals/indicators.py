"""Hand-rolled technical indicators over OHLCV pandas DataFrames.

Every function takes a DataFrame with at least a ``close`` column (``high``/``low``
are needed for a few of them) and returns a pandas Series aligned to the input
index. No TA-Lib dependency: this keeps the project installable with plain
pip on any OS/arch.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    # Wilder's smoothing (equivalent to an EMA with alpha = 1/period).
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    result = 100 - (100 / (1 + rs))
    return result.where(avg_loss != 0, 100.0)


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    fast_ema = ema(close, fast)
    slow_ema = ema(close, slow)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return pd.DataFrame(
        {"macd": macd_line, "signal": signal_line, "hist": histogram}
    )


def bollinger_bands(
    close: pd.Series, period: int = 20, num_std: float = 2.0
) -> pd.DataFrame:
    mid = sma(close, period)
    std = close.rolling(window=period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    # %B: where price sits within the bands, 0 = lower band, 1 = upper band.
    pct_b = (close - lower) / (upper - lower).replace(0.0, np.nan)
    bandwidth = (upper - lower) / mid.replace(0.0, np.nan)
    return pd.DataFrame(
        {"mid": mid, "upper": upper, "lower": lower, "pct_b": pct_b, "bandwidth": bandwidth}
    )


def stochastic(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    k_period: int = 5,
    k_smooth: int = 3,
    d_period: int = 3,
) -> pd.DataFrame:
    lowest_low = low.rolling(window=k_period).min()
    highest_high = high.rolling(window=k_period).max()
    raw_k = 100 * (close - lowest_low) / (highest_high - lowest_low).replace(0.0, np.nan)
    k = raw_k.rolling(window=k_smooth).mean()
    d = k.rolling(window=d_period).mean()
    return pd.DataFrame({"k": k, "d": d})


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = atr(high, low, close, period=1)  # raw true range (period=1 -> no smoothing yet)
    atr_smooth = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    plus_dm_smooth = pd.Series(plus_dm, index=high.index).ewm(
        alpha=1 / period, min_periods=period, adjust=False
    ).mean()
    minus_dm_smooth = pd.Series(minus_dm, index=high.index).ewm(
        alpha=1 / period, min_periods=period, adjust=False
    ).mean()

    plus_di = 100 * plus_dm_smooth / atr_smooth.replace(0.0, np.nan)
    minus_di = 100 * minus_dm_smooth / atr_smooth.replace(0.0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
