"""Separating results by build, so a fix can actually be measured.

The journal lives on the user's Desktop and survives upgrades, so trades
produced by a buggy build otherwise pool with trades from the fixed build and
hide the difference.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from cheese_signals import __version__, analytics, storage


@pytest.fixture
def journal(tmp_path):
    j = storage.Journal(tmp_path / "v.db")
    yield j
    j.close()


def _add(journal, entry_at=None, won=True):
    now = entry_at or datetime.now(timezone.utc)
    sid = journal.record_signal(
        "EURUSD_otc", 1, 0.7, "liquidity_sweep", "r",
        now - timedelta(minutes=2), now, now + timedelta(minutes=1),
        "london", now.hour, {},
    )
    journal.record_outcome(
        sid, "EURUSD_otc", 1, 1.1000, 1.1005 if won else 1.0995,
        won, 0.85, 10.0, now + timedelta(minutes=1), "reason",
    )
    return sid


def test_new_signals_are_stamped_with_the_build(journal):
    _add(journal)
    assert journal.joined_results()[0]["app_version"] == __version__


def test_migration_preserves_an_older_database(tmp_path):
    """Upgrading in place must keep history, not reset it."""
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, asset TEXT NOT NULL,
            direction INTEGER NOT NULL, score REAL NOT NULL, strategy TEXT,
            reason TEXT, detected_at TEXT NOT NULL, entry_at TEXT NOT NULL,
            expiry_at TEXT NOT NULL, lead_seconds INTEGER NOT NULL, session TEXT,
            utc_hour INTEGER, features TEXT,
            status TEXT NOT NULL DEFAULT 'pending', cancel_reason TEXT);
        CREATE TABLE outcomes (
            signal_id INTEGER PRIMARY KEY, asset TEXT NOT NULL,
            direction INTEGER NOT NULL, entry_price REAL NOT NULL,
            exit_price REAL NOT NULL, won INTEGER NOT NULL, payout REAL NOT NULL,
            pnl REAL NOT NULL, stake REAL NOT NULL, settled_at TEXT NOT NULL,
            reason TEXT);
        INSERT INTO signals(asset,direction,score,strategy,reason,detected_at,
            entry_at,expiry_at,lead_seconds,session,utc_hour,features,status)
        VALUES ('EURUSD_otc',1,0.7,'liquidity_sweep','r','2026-08-01T00:00:00+00:00',
            '2026-08-01T00:02:00+00:00','2026-08-01T00:03:00+00:00',120,'london',0,'{}','settled');
        INSERT INTO outcomes VALUES (1,'EURUSD_otc',1,1.1,1.0995,0,0.85,-10.0,10.0,
            '2026-08-01T00:03:00+00:00','old loss');
        """
    )
    con.commit()
    con.close()

    j = storage.Journal(path)
    rows = j.joined_results()
    assert len(rows) == 1, "existing history must survive the upgrade"
    assert rows[0]["app_version"] is None, "old rows carry no version stamp"

    _add(j)   # a new trade on the upgraded schema
    rows = j.joined_results()
    assert len(rows) == 2
    assert {r["app_version"] for r in rows} == {None, __version__}
    j.close()


def test_this_build_filter_excludes_older_rows():
    rows = [
        {"app_version": None, "won": 1, "entry_at": "2026-08-01T00:00:00+00:00"},
        {"app_version": "0.1.0", "won": 0, "entry_at": "2026-08-01T00:00:00+00:00"},
        {"app_version": __version__, "won": 1, "entry_at": "2026-08-01T00:00:00+00:00"},
    ]
    kept = analytics.filter_rows(rows, analytics.FILTER_THIS_BUILD)
    assert len(kept) == 1
    assert kept[0]["app_version"] == __version__


def test_all_filter_keeps_everything():
    rows = [{"app_version": None}, {"app_version": "9.9.9"}]
    assert len(analytics.filter_rows(rows, analytics.FILTER_ALL)) == 2


def test_time_filters_use_entry_time():
    now = datetime.now(timezone.utc)
    rows = [
        {"entry_at": (now - timedelta(hours=2)).isoformat(), "won": 1},
        {"entry_at": (now - timedelta(days=3)).isoformat(), "won": 0},
        {"entry_at": (now - timedelta(days=30)).isoformat(), "won": 0},
    ]
    assert len(analytics.filter_rows(rows, analytics.FILTER_24H)) == 1
    assert len(analytics.filter_rows(rows, analytics.FILTER_7D)) == 2


def test_time_filter_tolerates_bad_timestamps():
    rows = [{"entry_at": "not-a-date"}, {"entry_at": None}]
    assert analytics.filter_rows(rows, analytics.FILTER_24H) == []


def test_describe_versions_labels_untagged_rows():
    text = analytics.describe_versions(
        [{"app_version": None}, {"app_version": None}, {"app_version": "0.2.0"}]
    )
    assert "before version tracking" in text
    assert "0.2.0" in text
