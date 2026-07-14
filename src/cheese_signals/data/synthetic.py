"""Synthetic OHLCV generator for demoing and backtesting without any broker credentials.

Real price series alternate between trending and ranging regimes; this
generator explicitly switches between the two on a random schedule so the
regime-gated strategies in ``strategies.py`` have both kinds of market to
react to, rather than pure noise (which would make trend-following and
mean-reversion equally useless and tell you nothing about the confluence
engine's logic).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .base import CandleFeed


def generate_synthetic_candles(
    count: int,
    start_price: float = 1.10000,
    seconds_per_candle: int = 60,
    seed: int | None = None,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)

    closes = np.empty(count)
    price = start_price

    regime_len = 0
    drift = 0.0
    vol = 0.00012

    for i in range(count):
        if regime_len <= 0:
            regime_len = int(rng.integers(20, 120))
            regime_roll = rng.random()
            if regime_roll < 0.4:
                drift = rng.uniform(0.00002, 0.00008) * rng.choice([-1, 1])  # trending
                vol = rng.uniform(0.00008, 0.00016)
            else:
                drift = 0.0  # ranging
                vol = rng.uniform(0.00006, 0.00014)

        shock = rng.normal(drift, vol)
        price = max(price * (1 + shock), 1e-6)
        closes[i] = price
        regime_len -= 1

    closes = pd.Series(closes)
    opens = closes.shift(1).fillna(start_price)

    intrabar_vol = closes.pct_change().abs().fillna(0.0005).clip(lower=0.00005) * closes
    wick_up = np.abs(rng.normal(0, 1, count)) * intrabar_vol
    wick_down = np.abs(rng.normal(0, 1, count)) * intrabar_vol

    highs = np.maximum(opens, closes) + wick_up
    lows = np.minimum(opens, closes) - wick_down
    volumes = rng.integers(50, 500, count).astype(float)

    end = pd.Timestamp.now("UTC").floor("min")
    index = pd.date_range(end=end, periods=count, freq=f"{seconds_per_candle}s")

    df = pd.DataFrame(
        {
            "open": opens.to_numpy(),
            "high": highs.to_numpy(),
            "low": lows.to_numpy(),
            "close": closes.to_numpy(),
            "volume": volumes,
        },
        index=index,
    )
    return df


class SyntheticFeed(CandleFeed):
    """Deterministic (given a seed) synthetic feed, useful for demos and unit tests."""

    def __init__(self, start_price: float = 1.10000, seconds_per_candle: int = 60, seed: int | None = None):
        self.start_price = start_price
        self.seconds_per_candle = seconds_per_candle
        self.seed = seed

    def get_candles(self, count: int) -> pd.DataFrame:
        return generate_synthetic_candles(
            count, start_price=self.start_price, seconds_per_candle=self.seconds_per_candle, seed=self.seed
        )
