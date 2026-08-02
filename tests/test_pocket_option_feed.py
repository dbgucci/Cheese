"""Tests for the live Pocket Option adapter's data handling.

The network client itself is stubbed -- these cover the parts that were
actually wrong in production: candle normalisation across the shapes the API
has returned, and the streaming buffer that backs the pull-based interface.
"""

import threading
import time

import pandas as pd
import pytest

from cheese_signals.data import pocket_option as po


def _candles(n=5, start=1_700_000_000, key="time"):
    return [
        {
            key: start + i * 60,
            "open": 1.1000 + i * 0.0001,
            "high": 1.1005 + i * 0.0001,
            "low": 1.0995 + i * 0.0001,
            "close": 1.1002 + i * 0.0001,
            "volume": 10 + i,
        }
        for i in range(n)
    ]


# ----------------------------- normalisation -----------------------------
def test_normalize_epoch_seconds():
    df = po._normalize(_candles(5))
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert isinstance(df.index, pd.DatetimeIndex)
    assert str(df.index.tz) == "UTC"
    assert len(df) == 5
    assert df.index.is_monotonic_increasing


def test_normalize_accepts_from_key():
    df = po._normalize(_candles(3, key="from"))
    assert len(df) == 3
    assert isinstance(df.index, pd.DatetimeIndex)


def test_normalize_accepts_iso_timestamps():
    raw = [
        {"time": "2026-08-01T12:00:00Z", "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15},
        {"time": "2026-08-01T12:01:00Z", "open": 1.15, "high": 1.25, "low": 1.05, "close": 1.2},
    ]
    df = po._normalize(raw)
    assert len(df) == 2
    assert df.index[0] < df.index[1]


def test_normalize_dedupes_and_sorts():
    raw = _candles(3)
    raw = [raw[2], raw[0], raw[1], raw[2]]  # out of order, with a duplicate
    df = po._normalize(raw)
    assert len(df) == 3
    assert df.index.is_monotonic_increasing


def test_normalize_fills_missing_volume():
    raw = [{"time": 1_700_000_000, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15}]
    df = po._normalize(raw)
    assert "volume" in df.columns


def test_normalize_empty_returns_empty():
    assert po._normalize([]).empty


def test_normalize_respects_count():
    df = po._normalize(_candles(10), count=4)
    assert len(df) == 4


# ------------------------------ dependency -------------------------------
def test_missing_dependency_message_differs_when_frozen(monkeypatch):
    exc = ImportError("No module named 'BinaryOptionsToolsV2'")

    monkeypatch.setattr(po.sys, "frozen", False, raising=False)
    source_msg = po._missing_dependency_message(exc)
    assert "pip install binaryoptionstoolsv2" in source_msg

    monkeypatch.setattr(po.sys, "frozen", True, raising=False)
    frozen_msg = po._missing_dependency_message(exc)
    # Must NOT tell a frozen-exe user to pip install -- that can never work.
    assert "cannot be fixed by installing" in frozen_msg
    assert "synthetic" in frozen_msg


# --------------------------- streaming buffer ----------------------------
class _StubFeed(po.PocketOptionFeed):
    """Bypasses __init__ so the buffer logic can be tested without a network."""

    def __init__(self, iterator):
        self.asset = "EURUSD_otc"
        self.timeframe_seconds = 60
        self.history_hours = 8.0
        self.max_rows = 500
        self._buffer = pd.DataFrame()
        self._lock = threading.Lock()
        self._first_data = threading.Event()
        self._error = None
        self._stop = threading.Event()
        self._iterator = iterator
        self._client = None
        self._thread = threading.Thread(target=self._stream, daemon=True)
        self._thread.start()

    def _stream(self):
        try:
            for closed, forming in self._iterator:
                if self._stop.is_set():
                    return
                if not closed:
                    continue
                with self._lock:
                    self._buffer = po._normalize(closed)
                self._first_data.set()
        except Exception as exc:
            self._error = exc
            self._first_data.set()


def test_buffer_serves_latest_closed_candles():
    feed = _StubFeed(iter([(_candles(200), {"open": 1.2})]))
    df = feed.get_candles(150)
    assert len(df) == 150
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    feed.close()


def test_forming_candle_is_never_included():
    """A strategy must only ever see closed candles."""
    closed = _candles(10)
    forming = {"time": 1_700_000_600, "open": 9.9, "high": 9.9, "low": 9.9, "close": 9.9}
    feed = _StubFeed(iter([(closed, forming)]))
    df = feed.get_candles(50)
    assert len(df) == 10
    assert 9.9 not in df["close"].values
    feed.close()


def test_stream_error_is_surfaced_to_caller():
    def boom():
        raise RuntimeError("authentication failed")
        yield  # pragma: no cover

    feed = _StubFeed(boom())
    with pytest.raises(RuntimeError, match="authentication failed"):
        feed.get_candles(10)
    feed.close()


def test_timeout_when_no_data_arrives(monkeypatch):
    monkeypatch.setattr(po, "FIRST_DATA_TIMEOUT", 0.2)

    def never():
        time.sleep(5)
        yield ([], None)  # pragma: no cover

    feed = _StubFeed(never())
    with pytest.raises(RuntimeError, match="No data from Pocket Option"):
        feed.get_candles(10)
    feed.close()


def test_empty_payload_raises_actionable_error():
    feed = _StubFeed(iter([]))
    feed._first_data.set()  # stream ended without ever yielding candles
    with pytest.raises(RuntimeError, match="Check the asset name"):
        feed.get_candles(10)
    feed.close()
