"""Diagnostics log, condition explanations, and Telegram delivery reporting."""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from cheese_signals import diagnostics, trend
from cheese_signals.notifiers.telegram import TelegramNotifier, _strip_markdown


def _frame(n, seed=0, step=0.00012):
    rng = np.random.default_rng(seed)
    c = [1.10 + i * step + np.sin(i / 5.0) * 0.00035 + rng.normal(0, 0.00012) for i in range(n)]
    idx = pd.date_range("2026-08-01", periods=n, freq="1min", tz="UTC")
    cs = pd.Series(c, index=idx)
    o = cs.shift(1).fillna(cs.iloc[0])
    return pd.DataFrame({
        "open": o, "high": np.maximum(o, cs) + 0.0002,
        "low": np.minimum(o, cs) - 0.0002, "close": cs, "volume": 100.0,
    })


# ------------------------------- explain ---------------------------------
def test_explain_reports_short_history_explicitly():
    """The silent-forever failure mode must name itself."""
    sig, checks = trend.explain(_frame(50))
    assert not sig.is_actionable
    history = next(c for c in checks if c.name == "history")
    assert not history.passed
    assert "need" in history.detail and "candles" in history.detail


def test_explain_lists_every_condition_when_there_is_no_signal():
    _, checks = trend.explain(_frame(600))
    names = {c.name for c in checks}
    assert {"history", "HA vs Keltner mid", "price vs EMA200",
            "trend agreement", "fractal trigger", "result"} <= names


def test_explain_names_a_stale_fractal_as_the_blocker():
    """This is what actually suppressed live signals for 30 minutes."""
    _, checks = trend.explain(_frame(600), fractal_max_age=2)
    frac = next(c for c in checks if c.name == "fractal trigger")
    if not frac.passed:
        assert "bars ago" in frac.detail


def test_explain_agrees_with_the_strategy():
    df = _frame(600)
    for i in range(trend.MIN_BARS, len(df), 25):
        window = df.iloc[: i + 1]
        sig, checks = trend.explain(window)
        assert sig.direction == trend.trend_continuation(window).direction
        result = next(c for c in checks if c.name == "result")
        assert result.passed == sig.is_actionable


def test_wider_trigger_window_produces_more_signals():
    df = _frame(600)
    def count(age):
        return sum(
            trend.trend_continuation(df.iloc[: i + 1], fractal_max_age=age).is_actionable
            for i in range(trend.MIN_BARS, len(df))
        )
    assert count(2) < count(10)


# ------------------------------ trace log --------------------------------
def test_trace_log_is_bounded(tmp_path, monkeypatch):
    log = diagnostics.TraceLog(maxlen=50, write_file=False)
    for i in range(500):
        log.add(diagnostics.make_trace("EURUSD_otc", "NO SETUP", f"n{i}"))
    assert len(log.recent(limit=1000)) == 50, "the log must not grow without bound"


def test_trace_filters():
    log = diagnostics.TraceLog(write_file=False)
    log.add(diagnostics.make_trace("EURUSD_otc", "FIRED", "buy"))
    log.add(diagnostics.make_trace("GBPUSD_otc", "NO SETUP", "nothing"))

    assert len(log.recent()) == 2
    assert len(log.recent(asset="EURUSD_otc")) == 1
    fired = log.recent(only_fired=True)
    assert len(fired) == 1 and fired[0].asset == "EURUSD_otc"
    assert log.counts() == {"FIRED": 1, "NO SETUP": 1}


def test_trace_text_includes_checks():
    t = diagnostics.make_trace(
        "EURUSD_otc", "NO SETUP", "no setup", checks=["[fail] fractal trigger: 40 bars ago"]
    )
    text = t.as_text()
    assert "EURUSD_otc" in text and "NO SETUP" in text and "fractal trigger" in text


def test_trace_log_writes_a_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CHEESE_SIGNALS_HOME", str(tmp_path))
    from cheese_signals import paths

    log = diagnostics.TraceLog(write_file=True)
    log.add(diagnostics.make_trace("EURUSD_otc", "FIRED", "buy now"))
    log.close()
    files = list((paths.logs_dir()).glob("engine-*.log"))
    assert files, "a log file should be written to the data folder"
    assert "EURUSD_otc" in files[0].read_text()


def test_logging_failure_never_raises(monkeypatch):
    log = diagnostics.TraceLog(write_file=True)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", boom)
    log.add(diagnostics.make_trace("EURUSD_otc", "FIRED", "x"))  # must not raise
    assert len(log.recent()) == 1


# ------------------------------- telegram --------------------------------
class _Resp:
    def __init__(self, status=200, text=""):
        self.status_code = status
        self.text = text
        self.ok = status < 400

    def raise_for_status(self):
        if not self.ok:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


def test_markdown_rejection_falls_back_to_plain_text(monkeypatch):
    """A 400 must not silently discard the result message."""
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(json)
        return _Resp(400, "can't parse entities") if len(calls) == 1 else _Resp(200)

    monkeypatch.setattr("requests.post", fake_post)
    ok, note = TelegramNotifier("t", "c").send_verbose("*bad _markdown*")
    assert ok
    assert "plain text" in note
    assert len(calls) == 2
    assert "parse_mode" not in calls[1]


def test_delivery_failure_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp(400, "chat not found"))
    ok, err = TelegramNotifier("t", "c").send_verbose("hello")
    assert not ok
    assert "chat not found" in err


def test_network_error_is_reported(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr("requests.post", boom)
    ok, err = TelegramNotifier("t", "c").send_verbose("hello")
    assert not ok and "no route" in err


def test_send_result_returns_status(monkeypatch):
    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp(200))

    class _Sig:
        asset = "EURUSD_otc"
        side = "BUY"

    class _Out:
        signal = _Sig()
        won = True
        entry_price = 1.1
        exit_price = 1.1005
        move_pips = 5.0
        pnl = 8.5
        reason = "Win: trend_continuation (0.9 ATR)"
        result_word = "WIN"

    ok, err = TelegramNotifier("t", "c").send_result(_Out())
    assert ok and err == ""


def test_strip_markdown_removes_formatting():
    assert _strip_markdown("*bold* _it_ `code`") == "bold it code"
