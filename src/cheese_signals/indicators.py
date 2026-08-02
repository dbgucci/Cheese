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


def heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """Heikin Ashi candles.

        HA_close = (O + H + L + C) / 4
        HA_open  = (previous HA_open + previous HA_close) / 2
        HA_high  = max(H, HA_open, HA_close)
        HA_low   = min(L, HA_open, HA_close)

    Smoothing makes a trend far easier to read, but two cautions apply and
    both are handled by the callers here:

    * HA_open depends only on *previous* bars, so there is no lookahead --
      but that also means HA lags real price. A colour flip arrives after
      the turn, not at it.
    * A binary option settles on the **real** close, not the HA close. HA
      can stay green while real price is falling, so HA must only ever be
      used to read direction; outcomes are always scored on real prices.
    """
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    ha_close = (o + h + l + c) / 4.0

    ha_open = np.empty(len(df))
    if len(df):
        ha_open[0] = (o.iloc[0] + c.iloc[0]) / 2.0
    for i in range(1, len(df)):
        ha_open[i] = (ha_open[i - 1] + ha_close.iloc[i - 1]) / 2.0
    ha_open_s = pd.Series(ha_open, index=df.index)

    ha_high = pd.concat([h, ha_open_s, ha_close], axis=1).max(axis=1)
    ha_low = pd.concat([l, ha_open_s, ha_close], axis=1).min(axis=1)

    return pd.DataFrame(
        {"open": ha_open_s, "high": ha_high, "low": ha_low, "close": ha_close}
    )


def keltner_channel(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    ema_period: int = 20,
    atr_period: int = 10,
    multiplier: float = 1.0,
) -> pd.DataFrame:
    """Keltner Channel: EMA mid-line with ATR-scaled bands.

    Defaults match the Pocket Option setup being modelled (EMA 20, ATR 10,
    multiplier 1).
    """
    mid = ema(close, ema_period)
    rng = atr(high, low, close, period=atr_period)
    return pd.DataFrame(
        {"mid": mid, "upper": mid + multiplier * rng, "lower": mid - multiplier * rng}
    )


def fractals(high: pd.Series, low: pd.Series, period: int = 7) -> pd.DataFrame:
    """Bill Williams style fractals over a ``period``-bar window.

    A period of 7 means a centre bar with 3 bars either side. The centre bar
    is an up-fractal when its high is the highest of the window, and a
    down-fractal when its low is the lowest.

    **The result is shifted forward by the wing size**, which is the single
    most important detail here. A fractal centred on bar *i* cannot be known
    until bar *i + wing* has closed, because the bars to its right are part
    of the test. Marking it at bar *i* -- which is where a charting platform
    draws it -- would let a backtest act on information it could not have
    had, and produce results that cannot be reproduced live.

    Returns boolean columns ``up`` and ``down`` indexed at the bar where the
    fractal became **knowable**, plus ``up_price``/``down_price`` carrying the
    price of the fractal itself.
    """
    wing = max(period // 2, 1)
    n = len(high)
    up = np.zeros(n, dtype=bool)
    down = np.zeros(n, dtype=bool)
    up_price = np.full(n, np.nan)
    down_price = np.full(n, np.nan)

    hv, lv = high.to_numpy(), low.to_numpy()
    for i in range(wing, n - wing):
        window_h = hv[i - wing: i + wing + 1]
        window_l = lv[i - wing: i + wing + 1]
        if hv[i] == window_h.max():
            up[i] = True
            up_price[i] = hv[i]
        if lv[i] == window_l.min():
            down[i] = True
            down_price[i] = lv[i]

    out = pd.DataFrame(
        {"up": up, "down": down, "up_price": up_price, "down_price": down_price},
        index=high.index,
    )
    # Shift so each fractal is reported at the bar it could first be confirmed.
    shifted = out.shift(wing)
    shifted["up"] = shifted["up"].fillna(False).astype(bool)
    shifted["down"] = shifted["down"].fillna(False).astype(bool)
    return shifted


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
