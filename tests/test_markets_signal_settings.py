"""The settings the window saves, and the Telegram chat-ID lookup.

The window itself is not tested here -- rendering Qt needs a display this
container does not have. What is tested is everything the window delegates to,
which is where the behaviour lives: what gets persisted, what gets refused, and
whether a misconfigured Telegram setup explains itself.
"""

import json

import pytest

from cheese_signals.markets.signal_settings import SignalSettings, settings_path
from cheese_signals.markets.signals import BREAK, RETEST
from cheese_signals.notifiers import telegram


# ------------------------------ persistence ------------------------------
def test_settings_round_trip(tmp_path):
    original = SignalSettings(symbols=["XAUUSD247", "NAS100"], range_minutes=15,
                              telegram_token="123:abc", telegram_chat_id="99")
    original.save(tmp_path / "s.json")
    assert SignalSettings.load(tmp_path / "s.json") == original


def test_a_missing_file_yields_defaults(tmp_path):
    settings = SignalSettings.load(tmp_path / "nope.json")
    assert settings.range_minutes == 15
    assert settings.telegram_token == ""


def test_a_corrupt_file_does_not_stop_the_app_starting(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{ not json", encoding="utf-8")
    assert SignalSettings.load(path) == SignalSettings()


def test_unknown_keys_from_an_older_version_are_ignored(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"range_minutes": 30, "gone": 1}), encoding="utf-8")
    assert SignalSettings.load(path).range_minutes == 30


def test_the_bot_token_is_saved_but_no_trading_password_is(tmp_path):
    """A leaked bot token only lets someone send messages as the bot. A trading
    password is a different category, and is still never stored."""
    path = SignalSettings(telegram_token="123:abc").save(tmp_path / "s.json")
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["telegram_token"] == "123:abc"
    assert not any("password" in key.lower() for key in written)


def test_settings_live_with_the_apps_other_data(monkeypatch, tmp_path):
    monkeypatch.setenv("CHEESE_SIGNALS_HOME", str(tmp_path))
    assert settings_path() == tmp_path / "orb-signals.json"


# ------------------------------ readiness ------------------------------
def test_telegram_is_not_ready_until_both_halves_are_present():
    assert SignalSettings().telegram_ready is False
    assert SignalSettings(telegram_token="t").telegram_ready is False
    assert SignalSettings(telegram_chat_id="1").telegram_ready is False
    assert SignalSettings(telegram_token="t", telegram_chat_id="1").telegram_ready


def test_disabling_telegram_overrides_valid_credentials():
    s = SignalSettings(telegram_token="t", telegram_chat_id="1",
                       telegram_enabled=False)
    assert s.telegram_ready is False


def test_each_stage_can_be_alerted_separately():
    s = SignalSettings(alert_on_break=False, alert_on_retest=True)
    assert s.wants(BREAK) is False
    assert s.wants(RETEST) is True


# ------------------------------ validation ------------------------------
def test_a_token_without_a_chat_id_is_called_out():
    problems = SignalSettings(telegram_token="123:abc").problems()
    assert any("nowhere to go" in p for p in problems)


def test_switching_both_alerts_off_is_called_out():
    problems = SignalSettings(alert_on_break=False, alert_on_retest=False).problems()
    assert any("nothing will ever be sent" in p for p in problems)


def test_no_instruments_is_called_out():
    assert any("nothing to watch" in p for p in SignalSettings(symbols=[]).problems())


def test_an_impossible_tolerance_is_called_out():
    problems = SignalSettings(retest_tolerance_fraction=1.5).problems()
    assert any("tolerance" in p for p in problems)


def test_sane_defaults_have_nothing_to_complain_about():
    assert SignalSettings().problems() == []


# ------------------------------ the derived config ------------------------------
def test_the_settings_reach_the_bot_config():
    s = SignalSettings(range_minutes=30, target_r=3.0, poll_seconds=45,
                       retest_tolerance_fraction=0.25, apply_filters=False,
                       entry_window_minutes=90)
    config = s.signal_config()
    assert config.orb.range_minutes == 30
    assert config.orb.target_r == 3.0
    assert config.orb.entry_window_minutes == 90
    assert config.poll_seconds == 45
    assert config.retest_tolerance_fraction == 0.25
    assert config.apply_filters is False
    assert config.validate() == []


def test_the_range_length_is_not_silently_overridden_per_instrument():
    """The rule is 15 minutes for everything; autobot's per-instrument tweak
    would quietly make gold 30 and contradict the Settings screen."""
    assert SignalSettings().signal_config().per_symbol_range_minutes is False


def test_resolved_broker_symbols_can_replace_the_configured_ones():
    s = SignalSettings(symbols=["XAUUSD"])
    assert s.signal_config(["XAUUSD247"]).symbols == ["XAUUSD247"]


# ------------------------------ chat ID discovery ------------------------------
class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_a_single_chat_is_found(monkeypatch):
    payload = {"ok": True, "result": [
        {"message": {"chat": {"id": 987654321, "first_name": "Z", "last_name": "G"}}}]}
    monkeypatch.setattr(telegram.requests, "get",
                        lambda *a, **k: FakeResponse(payload=payload))
    chats, error = telegram.discover_chat_ids("123:abc")
    assert error == ""
    assert chats == [{"id": "987654321", "name": "Z G"}]


def test_duplicate_updates_collapse_to_one_chat(monkeypatch):
    """Telegram returns every message, not every chat."""
    payload = {"ok": True, "result": [
        {"message": {"chat": {"id": 5, "first_name": "A"}}},
        {"message": {"chat": {"id": 5, "first_name": "A"}}},
        {"channel_post": {"chat": {"id": 9, "title": "Alerts"}}}]}
    monkeypatch.setattr(telegram.requests, "get",
                        lambda *a, **k: FakeResponse(payload=payload))
    chats, _ = telegram.discover_chat_ids("123:abc")
    assert sorted(c["id"] for c in chats) == ["5", "9"]


def test_no_messages_explains_the_one_step_that_cannot_be_automated(monkeypatch):
    monkeypatch.setattr(telegram.requests, "get",
                        lambda *a, **k: FakeResponse(payload={"ok": True, "result": []}))
    chats, error = telegram.discover_chat_ids("123:abc")
    assert chats == []
    assert "send your bot any message" in error


def test_a_bad_token_is_reported_as_a_bad_token(monkeypatch):
    monkeypatch.setattr(telegram.requests, "get",
                        lambda *a, **k: FakeResponse(status_code=404))
    chats, error = telegram.discover_chat_ids("nope")
    assert chats == [] and "rejected" in error


def test_a_network_failure_is_reported_rather_than_raised(monkeypatch):
    def boom(*a, **k):
        raise telegram.requests.RequestException("no route to host")

    monkeypatch.setattr(telegram.requests, "get", boom)
    chats, error = telegram.discover_chat_ids("123:abc")
    assert chats == [] and "no route to host" in error


def test_telegram_saying_not_ok_is_surfaced(monkeypatch):
    payload = {"ok": False, "description": "Unauthorized"}
    monkeypatch.setattr(telegram.requests, "get",
                        lambda *a, **k: FakeResponse(payload=payload))
    chats, error = telegram.discover_chat_ids("123:abc")
    assert chats == [] and error == "Unauthorized"


def test_a_group_chat_is_named_by_its_title(monkeypatch):
    payload = {"ok": True, "result": [
        {"message": {"chat": {"id": -100123, "title": "Trading group"}}}]}
    monkeypatch.setattr(telegram.requests, "get",
                        lambda *a, **k: FakeResponse(payload=payload))
    chats, _ = telegram.discover_chat_ids("123:abc")
    assert chats == [{"id": "-100123", "name": "Trading group"}]
