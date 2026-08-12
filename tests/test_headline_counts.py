"""The two numbers on Live Signals must describe what the user saw.

Both were wrong in the same way -- they counted database rows rather than
reality:

* "Awaiting entry" read `status IN ('pending','active')` from the journal. A
  signal is written pending the moment it fires and only resolved when the
  engine reaches it, so closing the app stranded every in-flight signal as
  pending forever. Those orphans accumulated across sessions, and the tile
  showed a number with nothing on screen to match it.

* "Signals today" counted every row including cancelled ones. With a noisy
  trigger most signals are withdrawn before entry, so the headline read 566
  against a couple of dozen the user was actually asked to trade.
"""

from datetime import datetime, timedelta, timezone

import pytest

from cheese_signals import storage

DAY = "2026-08-04"
NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _signal(j, status, detected=NOW, asset="EURUSD_otc"):
    sid = j.record_signal(
        asset=asset, direction=1, score=0.7, strategy="trend_continuation+bos",
        reason="r", detected_at=detected, entry_at=detected + timedelta(minutes=1),
        expiry_at=detected + timedelta(minutes=2), session="tokyo",
        utc_hour=detected.hour, features={},
    )
    if status != "pending":
        j._conn.execute("UPDATE signals SET status = ? WHERE id = ?", (status, sid))
        j._conn.commit()
    return sid


# ----------------------------- signals today -----------------------------
def test_cancelled_signals_are_not_counted_as_signals(tmp_path):
    j = storage.Journal(tmp_path / "a.db")
    for _ in range(3):
        _signal(j, "settled")
    for _ in range(20):
        _signal(j, "cancelled")

    assert j.count_signals_since(DAY) == 3, "cancelled signals inflated the headline"
    assert j.count_cancelled_since(DAY) == 20
    assert j.count_signals_since(DAY, include_cancelled=True) == 23
    j.close()


def test_pending_and_active_signals_still_count(tmp_path):
    """They were announced; they just have not resolved yet."""
    j = storage.Journal(tmp_path / "b.db")
    _signal(j, "pending")
    _signal(j, "active")
    _signal(j, "settled")
    _signal(j, "cancelled")
    assert j.count_signals_since(DAY) == 3
    j.close()


def test_yesterdays_signals_are_not_counted_today(tmp_path):
    j = storage.Journal(tmp_path / "c.db")
    _signal(j, "settled", detected=NOW - timedelta(days=1))
    _signal(j, "settled")
    assert j.count_signals_since(DAY) == 1
    j.close()


# --------------------------- stale pending rows ---------------------------
def test_signals_left_pending_by_a_previous_session_are_closed_out(tmp_path):
    j = storage.Journal(tmp_path / "d.db")
    for _ in range(7):
        _signal(j, "pending", detected=NOW - timedelta(hours=3))
    assert j.stats()["pending"] == 7

    closed = j.abandon_stale_signals(NOW.isoformat())
    assert closed == 7
    assert j.stats()["pending"] == 0, "orphans still counted as awaiting entry"
    j.close()


def test_abandoning_leaves_a_reason_on_the_record(tmp_path):
    j = storage.Journal(tmp_path / "e.db")
    _signal(j, "pending", detected=NOW - timedelta(hours=1))
    j.abandon_stale_signals(NOW.isoformat())

    row = j._conn.execute(
        "SELECT status, cancel_reason FROM signals ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "abandoned"
    assert "app closed" in row["cancel_reason"]
    j.close()


def test_abandoning_does_not_touch_signals_from_this_session(tmp_path):
    """A signal in flight right now must survive the startup sweep."""
    j = storage.Journal(tmp_path / "f.db")
    cutoff = NOW - timedelta(minutes=30)
    _signal(j, "pending", detected=NOW)          # after the cutoff
    assert j.abandon_stale_signals(cutoff.isoformat()) == 0
    assert j.stats()["pending"] == 1
    j.close()


def test_abandoning_does_not_touch_settled_or_cancelled_rows(tmp_path):
    j = storage.Journal(tmp_path / "g.db")
    _signal(j, "settled", detected=NOW - timedelta(hours=2))
    _signal(j, "cancelled", detected=NOW - timedelta(hours=2))
    assert j.abandon_stale_signals(NOW.isoformat()) == 0
    j.close()


def test_abandoned_signals_do_not_reappear_as_signals_today(tmp_path):
    j = storage.Journal(tmp_path / "h.db")
    _signal(j, "pending", detected=NOW - timedelta(hours=2))
    _signal(j, "settled")
    j.abandon_stale_signals(NOW.isoformat())
    assert j.count_signals_since(DAY) == 2, (
        "an abandoned signal was still announced, so it should still count"
    )
    j.close()


# ------------------------------ the GUI tiles ------------------------------
@pytest.fixture(scope="module")
def app():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # PySide6 can be installed and still unimportable: on a headless machine its
    # Qt libraries (libEGL) are often absent, which raises a plain ImportError.
    # pytest.importorskip only skips on ModuleNotFoundError, so it re-raises that
    # and fails collection instead of skipping.
    try:
        import PySide6.QtWidgets  # noqa: F401
    except ImportError as exc:
        pytest.skip(f"PySide6 is unusable here: {exc}")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_awaiting_entry_is_zero_with_no_engine(app, tmp_path, monkeypatch):
    """The tile is a live fact; a stopped engine has nothing awaiting entry."""
    monkeypatch.setenv("CHEESE_SIGNALS_HOME", str(tmp_path))
    from cheese_signals.gui.app import MainWindow

    w = MainWindow()
    try:
        # Strand some rows the way a crash would.
        for _ in range(5):
            _signal(w.journal, "pending")
        assert w.journal.stats()["pending"] == 5
        assert w._awaiting_entry() == 0, "the tile read the journal, not the scheduler"
    finally:
        w.close()


def test_awaiting_entry_follows_the_scheduler(app, tmp_path, monkeypatch):
    monkeypatch.setenv("CHEESE_SIGNALS_HOME", str(tmp_path / "two"))
    from cheese_signals.gui.app import MainWindow
    from cheese_signals.scheduler import schedule_signal
    from cheese_signals.strategies import UP

    w = MainWindow()
    try:
        class _Eng:
            is_running = True

            class scheduler:
                @staticmethod
                def awaiting_entry(now):
                    return [
                        schedule_signal("EURUSD_otc", UP, 0.8, "s", "r",
                                        datetime.now(timezone.utc), "tokyo")
                    ]

        w.engine = _Eng()
        assert w._awaiting_entry() == 1
    finally:
        w.engine = None
        w.close()


def test_cancelled_cards_are_dropped_from_the_live_list(app, tmp_path, monkeypatch):
    monkeypatch.setenv("CHEESE_SIGNALS_HOME", str(tmp_path / "three"))
    from cheese_signals.gui.app import LivePage, MainWindow
    from cheese_signals.scheduler import schedule_signal
    from cheese_signals.strategies import UP

    w = MainWindow()
    try:
        old = schedule_signal("EURUSD_otc", UP, 0.8, "s", "r",
                              datetime.now(timezone.utc) - timedelta(minutes=10),
                              "tokyo", lead_minutes=0)
        live = schedule_signal("GBPUSD_otc", UP, 0.8, "s", "r",
                               datetime.now(timezone.utc), "tokyo", lead_minutes=1)
        w.live_page.add_signal(old)
        w.live_page.add_signal(live)
        assert len(w.live_page.cards) == 2

        old.status = "cancelled"
        w.live_page.refresh()
        assert len(w.live_page.cards) == 1, "a stale cancellation stayed on screen"
        assert w.live_page.cards[0].signal is live
    finally:
        w.close()


def test_a_fresh_cancellation_is_still_shown(app, tmp_path, monkeypatch):
    """You need to see that it cancelled, and why."""
    monkeypatch.setenv("CHEESE_SIGNALS_HOME", str(tmp_path / "four"))
    from cheese_signals.gui.app import MainWindow
    from cheese_signals.scheduler import schedule_signal
    from cheese_signals.strategies import UP

    w = MainWindow()
    try:
        sig = schedule_signal("EURUSD_otc", UP, 0.8, "s", "r",
                              datetime.now(timezone.utc), "tokyo", lead_minutes=1)
        w.live_page.add_signal(sig)
        sig.status = "cancelled"
        w.live_page.refresh()
        assert len(w.live_page.cards) == 1
    finally:
        w.close()
