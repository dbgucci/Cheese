"""What happens to an alert between the bot finding it and Telegram getting it.

Written after a live failure: signals were being generated, a test message went
through, and no alert arrived for hours with nothing on screen explaining why.
Two things caused that and both are covered here -- credentials that were typed
but never saved, and an exception inside a Qt slot, which goes to stderr and
therefore nowhere at all in a windowed exe.

These drive the window rather than the bot: the bug was never in the strategy.
"""

import pytest

try:
    from PySide6.QtWidgets import QApplication
except ImportError as exc:  # pragma: no cover - environment dependent
    pytest.skip(f"PySide6 is unusable here: {exc}", allow_module_level=True)

from datetime import datetime, timedelta, timezone  # noqa: E402

from cheese_signals.gui import theme  # noqa: E402
from cheese_signals.gui import signals_app  # noqa: E402
from cheese_signals.gui.signals_app import SignalsWindow  # noqa: E402
from cheese_signals.markets import signals as sig  # noqa: E402
from cheese_signals.markets.execution import BUY  # noqa: E402

AT = datetime(2026, 8, 20, 13, 46, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def app():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    instance = QApplication.instance() or QApplication([])
    instance.setStyleSheet(theme.stylesheet())
    return instance


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setenv("CHEESE_SIGNALS_HOME", str(tmp_path))
    win = SignalsWindow()
    win.settings.attach_chart = False        # the picture has its own tests
    yield win
    win.close()


def a_signal(kind=sig.RETEST):
    return sig.Signal(
        kind=kind, symbol="AAPL", direction=BUY, at=AT, entry=232.10,
        stop=231.40, target=233.50, range_low=231.40, range_high=232.10,
        range_points=70.0, risk_points=70.0, reward_points=140.0,
        cost_points=4.0, session_label="US cash equities", session_open=AT,
        flat_by=AT + timedelta(hours=6), reason="held it", digits=2,
        session_tz="America/New_York", session_key="us_cash", range_minutes=15,
        ref="AAPL-0820-RETEST")


class FakeBot:
    """Stands in for TelegramNotifier, recording what it was asked to send."""

    sent: list = []
    fail_with = None

    def __init__(self, token, chat_id, timeout=10):
        self.token, self.chat_id = token, chat_id

    def send_verbose(self, text):
        if FakeBot.fail_with:
            raise FakeBot.fail_with
        FakeBot.sent.append((self.token, self.chat_id, text))
        return True, ""

    def send_photo(self, image, caption="", filename="chart.png"):
        return self.send_verbose(caption)


@pytest.fixture(autouse=True)
def notifier(monkeypatch):
    FakeBot.sent = []
    FakeBot.fail_with = None
    import cheese_signals.notifiers as notifiers

    monkeypatch.setattr(notifiers, "TelegramNotifier", FakeBot)
    return FakeBot


def log_of(window):
    return window.activity.view.toPlainText()


# ------------------------ credentials typed but not saved ------------------------
def test_an_alert_uses_the_saved_credentials_not_the_boxes(window):
    """The trap: the test button sends with what is typed, alerts send with what
    was saved. Typing a token, testing it, and never pressing Save is a day of
    signals that go nowhere."""
    window.settings_page.tg_token.setText("123:typed-not-saved")
    window.settings_page.tg_chat.setText("999")
    window._on_signal(a_signal())
    assert FakeBot.sent == []
    assert "NOT SENT" in log_of(window)


def test_the_reason_it_was_not_sent_is_named(window):
    window._on_signal(a_signal())
    assert "no bot token is saved" in log_of(window)
    assert "not being sent" in window.status_label.text()


def test_switching_telegram_off_is_named_as_the_reason(window):
    window.settings.telegram_token = "123:abc"
    window.settings.telegram_chat_id = "99"
    window.settings.telegram_enabled = False
    window._on_signal(a_signal())
    assert FakeBot.sent == []
    assert "switched off" in log_of(window)


def test_a_saved_setup_actually_sends(window):
    window.settings.telegram_token = "123:abc"
    window.settings.telegram_chat_id = "99"
    window._on_signal(a_signal())
    assert len(FakeBot.sent) == 1
    token, chat, text = FakeBot.sent[0]
    assert (token, chat) == ("123:abc", "99")
    assert "RETEST" in text and "AAPL" in text
    assert "→ sent" in log_of(window)


def test_a_stage_switched_off_says_so_rather_than_going_quiet(window):
    """Silence is the one thing that must never mean 'working as intended'."""
    window.settings.telegram_token = "123:abc"
    window.settings.telegram_chat_id = "99"
    window.settings.alert_on_retest = False
    window._on_signal(a_signal())
    assert FakeBot.sent == []
    assert "switched off in Settings" in log_of(window)


# ------------------------ failures that used to vanish ------------------------
def test_an_unexpected_error_while_sending_is_reported_not_swallowed(window):
    """A Qt slot prints its traceback to stderr, and a windowed exe has no
    stderr. Anything raised here used to disappear completely."""
    window.settings.telegram_token = "123:abc"
    window.settings.telegram_chat_id = "99"
    FakeBot.fail_with = MemoryError("out of memory building the request")
    window._on_signal(a_signal())
    text = log_of(window)
    assert "Telegram failed" in text
    assert "MemoryError" in text


def test_a_broken_slot_lands_in_the_log_with_its_traceback(window):
    def explode(_signal):
        raise ValueError("something in the table blew up")

    window.feed.add_signal = explode
    window._on_signal(a_signal())
    text = log_of(window)
    assert "failed" in text
    assert "ValueError" in text
    assert "Traceback" in text, "the traceback is the part that identifies it"


def test_a_chart_failure_still_lets_the_alert_go_out(window):
    """The picture is the optional half of an alert."""
    window.settings.telegram_token = "123:abc"
    window.settings.telegram_chat_id = "99"
    window.settings.attach_chart = True
    window.worker.draw = lambda item, kind: (_ for _ in ()).throw(
        RuntimeError("no bars"))
    window._on_signal(a_signal())
    assert len(FakeBot.sent) == 1, "the text must survive a broken chart"
    assert "chart step failed" in log_of(window)


# ------------------------------ the pre-flight ------------------------------
def test_starting_to_watch_says_alerts_will_not_be_sent(window):
    window._on_running(True)
    assert "NOTHING WILL BE SENT" in log_of(window)


def test_starting_to_watch_confirms_what_will_be_sent(window):
    window.settings.telegram_token = "123:abc"
    window.settings.telegram_chat_id = "99"
    window._on_running(True)
    text = log_of(window)
    assert "alerts will be sent for: break, retest, result" in text
