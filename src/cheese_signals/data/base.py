"""Common interface every data feed implements: pull OHLCV candles as a DataFrame."""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

REQUIRED_COLUMNS = ["open", "high", "low", "close", "volume"]


class CandleFeed(ABC):
    """A source of OHLCV candles for one asset/timeframe pair."""

    @abstractmethod
    def get_candles(self, count: int) -> pd.DataFrame:
        """Return the most recent ``count`` closed candles, oldest first.

        Must be indexed by candle open time and contain at least
        open/high/low/close columns (volume may be zero-filled if unavailable,
        e.g. OTC synthetic instruments).
        """
        raise NotImplementedError


def validate_candles(df: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in ("open", "high", "low", "close") if c not in df.columns]
    if missing:
        raise ValueError(f"candle frame missing required columns: {missing}")
    if "volume" not in df.columns:
        df = df.copy()
        df["volume"] = 0.0
    return df
