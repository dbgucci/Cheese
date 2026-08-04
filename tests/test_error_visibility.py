"""Failures must be readable, not truncated into uselessness.

A one-line status bar showed only the first line of an exception, and a
broker error that names every asset on the account is many lines long. The
user could see that something broke and not what. These tests pin the three
places an error now has to reach: the status callback, the Diagnostics trace
(in full), and the daily log file.

Also covered: the two conditions that silently produce no signals forever --
a feed whose history never grows past what the setup needs, and a watchlist
too long to scan within one candle.
"""

from datetime import datetime, timezone

import pandas as pd
import pytest

from cheese_signals import diagnostics, engine as engine_mod, storage
from cheese_signals.settings import Settings


class _Feed:
    """A feed that always returns the same short history."""

    def __init__(self, bars: int, grows: bool = False):
        self.bars = bars
        self.grows = grows

    def get_candles(self, count):
        if self.grows:
            self.bars += 5
        idx = pd.date_range("2026-08-01", periods=self.bars, freq="1min", tz="UTC")
        return pd.DataFrame(
            {"open": 1.1, "high": 1.11, "low": 1.09, "close": 1.105, "volume": 100.0},
            index=idx,
        )


def _engine(tmp_path, feed_factory, **overrides):
    s = Settings()
    s.assets = overrides.pop("assets", ["EURUSD_otc"])
    for k, v in overrides.items():
        setattr(s, k, v)
    errors: list[str] = []
    eng = engine_mod.SignalEngine(
        settings=s,
        journal=storage.Journal(tmp_path / "e.db"),
        feed_factory=feed_factory,
        on_error=errors.append,
    )
    return eng, errors


# ------------------------------ error reporting ------------------------------
MULTILINE = (
    "Pocket Option feed failed for 'AEDCNY_otc': asset not tradeable\n"
    "Currently tradeable OTC assets include:\n"
    "  AUDCAD_otc, AUDCHF_otc, AUDJPY_otc, AUDNZD_otc, AUDUSD_otc"
)


def test_full_error_text_reaches_diagnostics(tmp_path):
    def boom(asset):
        raise RuntimeError(MULTILINE)

    eng, errors = _engine(tmp_path, boom)
    eng._tick()

    traces = [t for t in eng.traces.recent() if t.outcome == "ERROR"]
    assert len(traces) == 1
    trace = traces[0]

    assert "asset not tradeable" in trace.summary
    # The lines the status bar cannot show must survive in the trace.
    body = trace.as_text()
    assert "AUDCAD_otc" in body
    assert "Currently tradeable" in body


def test_status_callback_still_gets_the_whole_message(tmp_path):
    """The GUI elides for display; it must not be handed a pre-truncated string."""
    def boom(asset):
        raise RuntimeError(MULTILINE)

    eng, errors = _engine(tmp_path, boom)
    eng._tick()

    assert len(errors) == 1
    assert "AUDCAD_otc" in errors[0], "the callback received a truncated message"
    assert errors[0].startswith("EURUSD_otc:")


def test_errors_survive_the_signals_only_filter(tmp_path):
    """Filtering to signals must not hide the reason there are no signals."""
    def boom(asset):
        raise RuntimeError("something broke")

    eng, _ = _engine(tmp_path, boom)
    eng._tick()

    shown = eng.traces.recent(only_fired=True)
    assert any(t.outcome == "ERROR" for t in shown)


def test_errors_are_written_to_the_log_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CHEESE_SIGNALS_HOME", str(tmp_path))
    log = diagnostics.TraceLog(write_file=True)
    log.add(diagnostics.make_trace(
        "EURUSD_otc", "ERROR", "asset not tradeable",
        checks=["AUDCAD_otc, AUDCHF_otc"],
    ))
    log.close()

    from cheese_signals import paths

    files = list(paths.logs_dir().glob("*.log"))
    assert files, "no log file written"
    text = files[0].read_text()
    assert "asset not tradeable" in text
    assert "AUDCAD_otc" in text


def test_an_exception_with_no_message_still_reports_something(tmp_path):
    def boom(asset):
        raise RuntimeError()

    eng, errors = _engine(tmp_path, boom)
    eng._tick()

    trace = [t for t in eng.traces.recent() if t.outcome == "ERROR"][0]
    assert trace.summary == "RuntimeError"
    assert errors and errors[0].strip() != "EURUSD_otc:"


# ------------------------------- warm-up stall -------------------------------
def test_a_growing_feed_reports_progress_not_a_block(tmp_path):
    eng, _ = _engine(tmp_path, lambda a: _Feed(100, grows=True))
    for _ in range(engine_mod.WARMUP_STALL_TICKS + 2):
        eng._tick()

    outcomes = {t.outcome for t in eng.traces.recent()}
    assert "WARMING UP" in outcomes
    assert "BLOCKED" not in outcomes, "a feed that is still filling is not blocked"


def test_a_stalled_feed_is_reported_as_permanently_blocked(tmp_path):
    """145 candles forever, needing 220, is not 'warming up'."""
    eng, _ = _engine(tmp_path, lambda a: _Feed(145))
    for _ in range(engine_mod.WARMUP_STALL_TICKS + 2):
        eng._tick()

    blocked = [t for t in eng.traces.recent() if t.outcome == "BLOCKED"]
    assert blocked, "a feed stuck below the requirement must say so"
    trace = blocked[0]
    assert "no signal can ever fire" in trace.summary
    # And it must say what to actually do about it.
    body = trace.as_text()
    assert "Trend EMA period" in body
    assert "support_resistance" in body


def test_the_block_is_not_repeated_every_tick(tmp_path):
    eng, _ = _engine(tmp_path, lambda a: _Feed(145))
    for _ in range(engine_mod.WARMUP_STALL_TICKS * 3):
        eng._tick()

    blocked = [t for t in eng.traces.recent() if t.outcome == "BLOCKED"]
    assert len(blocked) <= 3, f"BLOCKED repeated {len(blocked)} times; it should be occasional"


# ------------------------------ scan cycle time ------------------------------
def test_a_scan_slower_than_a_candle_is_reported(tmp_path):
    eng, _ = _engine(tmp_path, lambda a: _Feed(400), assets=[f"P{i}_otc" for i in range(57)])
    eng._check_cycle_time(370.0)      # 57 pairs at ~6.5s each

    warnings = [t for t in eng.traces.recent() if t.outcome == "CONFIG WARNING"]
    assert warnings
    text = warnings[-1].as_text()
    assert "57 pairs" in text
    assert "6.2 min" in text          # how often each pair actually gets looked at
    assert "pairs fit inside one candle" in text


def test_a_scan_inside_the_candle_is_not_reported(tmp_path):
    eng, _ = _engine(tmp_path, lambda a: _Feed(400))
    eng._check_cycle_time(12.0)
    assert not [t for t in eng.traces.recent() if t.outcome == "CONFIG WARNING"]


def test_the_slow_scan_warning_is_only_shown_once(tmp_path):
    eng, _ = _engine(tmp_path, lambda a: _Feed(400))
    for _ in range(5):
        eng._check_cycle_time(300.0)
    warnings = [t for t in eng.traces.recent() if t.outcome == "CONFIG WARNING"]
    assert len(warnings) == 1


@pytest.mark.parametrize("elapsed,expected", [(370.0, True), (59.0, False)])
def test_cycle_warning_threshold_is_the_timeframe(tmp_path, elapsed, expected):
    eng, _ = _engine(tmp_path, lambda a: _Feed(400))
    assert eng.settings.timeframe_seconds == 60
    eng._check_cycle_time(elapsed)
    got = bool([t for t in eng.traces.recent() if t.outcome == "CONFIG WARNING"])
    assert got is expected
