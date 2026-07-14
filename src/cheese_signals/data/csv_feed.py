"""Load OHLCV candles from a CSV file, for backtesting against real exported history."""

from __future__ import annotations

import pandas as pd

from .base import CandleFeed, validate_candles


class CsvFeed(CandleFeed):
    """Reads a CSV with columns: timestamp, open, high, low, close[, volume].

    ``timestamp`` may be epoch seconds or anything ``pandas.to_datetime`` parses.
    """

    def __init__(self, path: str):
        df = pd.read_csv(path)
        if "timestamp" not in df.columns:
            raise ValueError("CSV must have a 'timestamp' column")
        try:
            ts = pd.to_datetime(df["timestamp"], unit="s")
        except (ValueError, TypeError):
            ts = pd.to_datetime(df["timestamp"])
        df = df.drop(columns=["timestamp"]).set_index(ts).sort_index()
        self._df = validate_candles(df)

    def get_candles(self, count: int) -> pd.DataFrame:
        return self._df.tail(count)
