"""End-to-end wiring test: detect -> schedule -> enter -> expire -> settle -> journal.

Drives the engine directly with a controlled feed and an explicit clock so the
whole lifecycle is exercised without waiting on wall-clock minutes.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from cheese_signals import storage
from cheese_signals.engine import SignalEngine
from cheese_signals.settings import Settings
from cheese_signals.strategies import DOWN, UP


def _sweep_frame(n=120, seed=0):
    """A choppy range ending in a textbook sweep of the swing highs."""
    rng = np.random.default_rng(seed)
    jitter = 0.00015
    rows = []
    price = 1.1000
    for i in range(n):
        wave = np.sin(i / 3.0) * jitter
        c = price + wave + rng.normal(0, jitter / 6)
        o = price + np.sin((i - 1) / 3.0) * jitter
        h = max(o, c) + abs(rng.normal(0, jitter / 5))
        l = min(o, c) - abs(rng.normal(0, jitter / 5))
        rows.append([o, h, l, c])

    resistance = max(r[1] for r in rows[:-6])
    o = resistance - 0.0004
    c = resistance - 0.0008
    h = resistance + 0.0010          # wick through the level
    l = c - 0.00005                  # body closes back inside
    rows.append([o, h, l, c])

    idx = pd.date_range("2026-08-03T12:00:00Z", periods=len(rows), freq="1min")
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"]).assign(
        volume=100.0
    )


class FakeFeed:
    """Returns a fixed frame; `price` is what a post-entry quote will report."""

    def __init__(self, df, price=None):
        self.df = df
        self.price = price

    def get_candles(self, count):
        if self.price is not None and count <= 3:
            tail = self.df.tail(1).copy()
            tail["close"] = self.price
            return tail
        return self.df.tail(count)


@pytest.fixture
def journal(tmp_path):
    j = storage.Journal(tmp_path / "engine.db")
    yield j
    j.close()


def _engine(journal, feed, **overrides):
    s = Settings()
    s.assets = ["EURUSD_otc"]
    s.lead_minutes = 2
    s.expiry_minutes = 1
    s.min_score = 0.4
    s.require_liquidity_sweep = True
    s.cooldown_minutes = 0
    s.use_higher_timeframe_bias = False
    for k, v in overrides.items():
        setattr(s, k, v)

    captured = {"signals": [], "results": [], "errors": []}
    eng = SignalEngine(
        settings=s,
        journal=journal,
        feed_factory=lambda a: feed,
        notifier=None,
        on_signal=captured["signals"].append,
        on_result=captured["results"].append,
        on_error=captured["errors"].append,
    )
    return eng, captured


def test_scan_detects_sweep_and_schedules_ahead(journal):
    df = _sweep_frame()
    eng, cap = _engine(journal, FakeFeed(df))
    now = datetime(2026, 8, 3, 14, 30, 20, tzinfo=timezone.utc)

    eng._scan_asset("EURUSD_otc", now)

    assert not cap["errors"]
    assert len(cap["signals"]) == 1
    sig = cap["signals"][0]
    assert sig.direction == DOWN            # sweep of highs -> SELL
    assert sig.strategy == "liquidity_sweep"
    # Announced ahead of entry, aligned to a candle boundary.
    assert sig.entry_at == datetime(2026, 8, 3, 14, 33, tzinfo=timezone.utc)
    assert sig.expiry_at == datetime(2026, 8, 3, 14, 34, tzinfo=timezone.utc)
    assert sig.lead_seconds >= 120

    # Persisted as pending, with its feature snapshot.
    assert journal.summary_counts()["pending"] == 1
    stored = journal.recent_signals(5)[0]
    assert stored.strategy == "liquidity_sweep"
    assert stored.features["profile"] == "otc"
    assert "displacement_atr" in stored.features


def test_candles_are_journalled_during_scan(journal):
    eng, _ = _engine(journal, FakeFeed(_sweep_frame()))
    eng._scan_asset("EURUSD_otc", datetime(2026, 8, 3, 14, 30, tzinfo=timezone.utc))
    assert journal.candle_count("EURUSD_otc") > 0


def test_full_lifecycle_settles_a_win_into_the_journal(journal):
    df = _sweep_frame()
    feed = FakeFeed(df)
    eng, cap = _engine(journal, feed)

    detected = datetime(2026, 8, 3, 14, 30, 20, tzinfo=timezone.utc)
    eng._scan_asset("EURUSD_otc", detected)
    sig = cap["signals"][0]

    # Enter at the scheduled minute.
    feed.price = 1.1000
    eng._enter(sig, sig.entry_at)
    assert sig.status == "active"
    assert sig.entry_price == pytest.approx(1.1000)

    # Price falls by expiry; the signal predicted DOWN, so this is a win.
    feed.price = 1.0994
    eng._settle(sig, sig.expiry_at)

    assert len(cap["results"]) == 1
    result = cap["results"][0]
    assert result.won is True
    assert result.pnl > 0
    assert result.reason.startswith("Win:")

    counts = journal.summary_counts()
    assert counts["settled"] == 1 and counts["wins"] == 1 and counts["pending"] == 0

    row = journal.joined_results()[0]
    assert row["won"] == 1
    assert row["strategy"] == "liquidity_sweep"
    assert "displacement" in row["outcome_reason"]


def test_full_lifecycle_settles_a_loss(journal):
    df = _sweep_frame()
    feed = FakeFeed(df)
    eng, cap = _engine(journal, feed)

    eng._scan_asset("EURUSD_otc", datetime(2026, 8, 3, 14, 30, 20, tzinfo=timezone.utc))
    sig = cap["signals"][0]

    feed.price = 1.1000
    eng._enter(sig, sig.entry_at)
    feed.price = 1.1006          # rose, but we predicted DOWN
    eng._settle(sig, sig.expiry_at)

    assert cap["results"][0].won is False
    assert journal.summary_counts() == {"settled": 1, "wins": 0, "losses": 1, "pending": 0}


def test_one_signal_per_asset_at_a_time(journal):
    df = _sweep_frame()
    eng, cap = _engine(journal, FakeFeed(df))

    eng._scan_asset("EURUSD_otc", datetime(2026, 8, 3, 14, 30, 20, tzinfo=timezone.utc))
    assert len(cap["signals"]) == 1

    # A second scan on a *new* candle must not stack another live signal.
    eng._last_candle_ts.clear()
    eng._scan_asset("EURUSD_otc", datetime(2026, 8, 3, 14, 31, 20, tzinfo=timezone.utc))
    assert len(cap["signals"]) == 1


def test_require_liquidity_sweep_filters_other_setups(journal):
    """With the filter on, a frame containing no sweep must produce nothing."""
    flat = pd.DataFrame(
        {"open": 1.1, "high": 1.1001, "low": 1.0999, "close": 1.1, "volume": 100.0},
        index=pd.date_range("2026-08-03T12:00:00Z", periods=150, freq="1min"),
    )
    eng, cap = _engine(journal, FakeFeed(flat), require_liquidity_sweep=True)
    eng._scan_asset("EURUSD_otc", datetime(2026, 8, 3, 14, 30, tzinfo=timezone.utc))
    assert cap["signals"] == []
    assert journal.summary_counts()["pending"] == 0
