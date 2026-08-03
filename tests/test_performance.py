"""Guards against the app slowing down as the journal grows.

The UI used to load every settled trade into Python on each update, so the
cost of showing a stat card grew with the size of the history. These tests
pin the behaviour that fixed it: aggregates run in SQL, views fetch only the
page they display, and unchanged candles are not rewritten.
"""

import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from cheese_signals import storage


@pytest.fixture
def big_journal(tmp_path):
    j = storage.Journal(tmp_path / "big.db")
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    sig, out = [], []
    for i in range(4000):
        t = base + timedelta(minutes=i)
        sig.append((
            "EURUSD_otc", 1, 0.72, "trend_continuation", "r", t.isoformat(),
            t.isoformat(), (t + timedelta(minutes=1)).isoformat(), 120, "london",
            t.hour, "{}", "settled", None, "0.2.0",
        ))
    j._conn.executemany(
        "INSERT INTO signals(asset,direction,score,strategy,reason,detected_at,entry_at,"
        "expiry_at,lead_seconds,session,utc_hour,features,status,cancel_reason,app_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", sig)
    for i in range(1, 4001):
        t = base + timedelta(minutes=i)
        won = 1 if i % 5 < 3 else 0        # 60% win rate
        out.append((i, "EURUSD_otc", 1, 1.1, 1.1005 if won else 1.0995, won, 0.85,
                    8.5 if won else -10.0, 10.0, t.isoformat(), "reason"))
    j._conn.executemany("INSERT INTO outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?)", out)
    j._conn.commit()
    yield j
    j.close()


def test_stats_match_a_full_scan(big_journal):
    """The fast path must agree with the slow one it replaced."""
    rows = big_journal.joined_results()
    st = big_journal.stats()

    assert st["trades"] == len(rows)
    assert st["wins"] == sum(1 for r in rows if r["won"])
    assert st["pnl"] == pytest.approx(sum(r["pnl"] for r in rows))
    assert st["win_rate"] == pytest.approx(st["wins"] / st["trades"])
    assert st["losses"] == st["trades"] - st["wins"]


def test_stats_are_cheap_regardless_of_history(big_journal):
    big_journal.stats()  # warm
    t = time.perf_counter()
    for _ in range(20):
        big_journal.stats()
    per_call_ms = (time.perf_counter() - t) / 20 * 1000
    assert per_call_ms < 25, f"stats() took {per_call_ms:.1f} ms; it must not scan the journal"


def test_limited_query_is_much_cheaper_than_the_full_one(big_journal):
    big_journal.joined_results(limit=500)
    t = time.perf_counter()
    for _ in range(5):
        big_journal.joined_results(limit=500)
    limited = (time.perf_counter() - t) / 5

    t = time.perf_counter()
    for _ in range(5):
        big_journal.joined_results()
    full = (time.perf_counter() - t) / 5

    assert limited < full / 2, "the paged query should cost a fraction of the full one"


def test_limit_returns_the_most_recent_rows_oldest_first(big_journal):
    everything = big_journal.joined_results()
    page = big_journal.joined_results(limit=500)

    assert len(page) == 500
    # Same ordering convention as the unlimited call.
    assert [r["entry_at"] for r in page] == sorted(r["entry_at"] for r in page)
    # And it is the *newest* 500, not the oldest.
    assert page[-1]["entry_at"] == everything[-1]["entry_at"]
    assert page[0]["entry_at"] == everything[-500]["entry_at"]


def test_signals_today_counted_in_sql(big_journal):
    day = "2026-08-01"
    counted = big_journal.count_signals_since(day)
    expected = sum(
        1 for s in big_journal.recent_signals(10_000) if s.detected_at.startswith(day)
    )
    assert counted >= expected > 0


# ------------------------------ candle writes ------------------------------
def _candles(start, n, tz="UTC"):
    idx = pd.date_range(start, periods=n, freq="1min", tz=tz)
    return pd.DataFrame(
        {"open": 1.1, "high": 1.11, "low": 1.09, "close": 1.105, "volume": 100.0},
        index=idx,
    )


def test_unchanged_candles_are_not_rewritten(tmp_path):
    """The engine offers a 50-candle tail every minute; only new ones count."""
    j = storage.Journal(tmp_path / "c.db")
    df = _candles("2026-08-01T00:00:00Z", 50)

    assert j.record_candles("EURUSD_otc", df) == 50
    assert j.record_candles("EURUSD_otc", df) == 0, "re-offering the same tail must write nothing"

    # A tail that overlaps by 49 bars and adds one new bar writes exactly one.
    nxt = _candles("2026-08-01T00:01:00Z", 50)
    assert j.record_candles("EURUSD_otc", nxt) == 1
    assert j.candle_count("EURUSD_otc") == 51
    j.close()


def test_watermark_survives_reopening_the_database(tmp_path):
    path = tmp_path / "c2.db"
    df = _candles("2026-08-01T00:00:00Z", 20)

    j = storage.Journal(path)
    assert j.record_candles("EURUSD_otc", df) == 20
    j.close()

    reopened = storage.Journal(path)
    assert reopened.record_candles("EURUSD_otc", df) == 0, "watermark must come from the DB"
    assert reopened.candle_count("EURUSD_otc") == 20
    reopened.close()


def test_assets_have_independent_watermarks(tmp_path):
    j = storage.Journal(tmp_path / "c3.db")
    df = _candles("2026-08-01T00:00:00Z", 10)
    assert j.record_candles("EURUSD_otc", df) == 10
    assert j.record_candles("GBPUSD_otc", df) == 10, "a second asset must not be skipped"
    j.close()


def test_wal_mode_is_enabled(tmp_path):
    j = storage.Journal(tmp_path / "w.db")
    mode = j._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    j.close()
