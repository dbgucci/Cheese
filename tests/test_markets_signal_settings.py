"""The settings the window saves, and the Telegram chat-ID lookup.

The window itself is not tested here -- rendering Qt needs a display this
container does not have. What is tested is everything the window delegates to,
which is where the behaviour lives: what gets persisted, what gets refused, and
whether a misconfigured Telegram setup explains itself.
"""

import json
from datetime import datetime, timezone

import pytest

from cheese_signals.markets import signal_settings, signals
from cheese_signals.markets.execution import BUY
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


# ------------------------------ the results file ------------------------------
def result(symbol="US30", kind=signals.WIN, r_net=1.95, r_gross=2.0,
           ambiguous=False):
    return signals.Outcome(
        symbol=symbol, direction=BUY, result=kind, entry=44010.0, stop=43990.0,
        target=44050.0, exit_price=44050.0,
        opened_at=datetime(2026, 3, 2, 14, 46, tzinfo=timezone.utc),
        closed_at=datetime(2026, 3, 2, 14, 52, tzinfo=timezone.utc),
        risk_points=200.0, points=400.0, cost_points=10.0, r_gross=r_gross,
        r_net=r_net, session_label="US cash equities",
        reason="the target was reached", ambiguous=ambiguous, digits=1)


def test_a_result_is_appended_with_a_header_on_the_first_write(tmp_path):
    path = tmp_path / "orb-results.csv"
    signal_settings.append_result(result(), path)
    signal_settings.append_result(result(kind=signals.LOSS, r_net=-1.05,
                                        r_gross=-1.0), path)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3, "one header, two results"
    assert lines[0].startswith("closed_at,symbol,side,result")
    assert "win" in lines[1] and "US30" in lines[1]
    assert "loss" in lines[2]


def test_the_running_record_survives_a_restart(tmp_path):
    """A hit rate that resets whenever the app is reopened is not a hit rate."""
    path = tmp_path / "orb-results.csv"
    for kind, net, gross in ((signals.WIN, 1.95, 2.0), (signals.WIN, 1.95, 2.0),
                             (signals.LOSS, -1.05, -1.0)):
        signal_settings.append_result(result(kind=kind, r_net=net,
                                            r_gross=gross), path)
    tally = signal_settings.load_tally(path)
    assert (tally.wins, tally.losses) == (2, 1)
    assert tally.r_net == pytest.approx(2.85)
    assert tally.win_rate == pytest.approx(2 / 3)


def test_a_missing_results_file_is_an_empty_record_not_an_error(tmp_path):
    tally = signal_settings.load_tally(tmp_path / "nothing.csv")
    assert tally.resolved == 0
    assert tally.summary() == "no results yet"


def test_a_truncated_last_line_does_not_stop_the_app_starting(tmp_path):
    """Killed from the taskbar mid-write. The rows that did land still count."""
    path = tmp_path / "orb-results.csv"
    signal_settings.append_result(result(), path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("2026-03-03 15:01,XAUUSD,BUY,wi")
    tally = signal_settings.load_tally(path)
    assert tally.wins == 1 and tally.resolved == 1


def test_an_ambiguous_result_is_marked_in_the_file(tmp_path):
    """So the share of the record that rests on the pessimistic assumption can
    be checked rather than taken on trust."""
    path = tmp_path / "orb-results.csv"
    signal_settings.append_result(result(ambiguous=True), path)
    signal_settings.append_result(result(), path)
    assert signal_settings.load_tally(path).ambiguous == 1


def test_tracking_off_with_result_alerts_on_is_named_as_a_contradiction():
    s = signal_settings.SignalSettings(track_outcomes=False, alert_on_result=True)
    assert any("no result will ever be worked out" in p for p in s.problems())


def test_the_signal_config_carries_the_tracking_switch():
    on = signal_settings.SignalSettings().signal_config(["US30"])
    off = signal_settings.SignalSettings(track_outcomes=False).signal_config(["US30"])
    assert on.track_outcomes is True
    assert off.track_outcomes is False


# ------------------------------ the watchlist ------------------------------
def test_the_default_watchlist_is_stocks_indices_and_metals():
    symbols = signal_settings.DEFAULT_SYMBOLS
    assert {"US30", "SPX500", "NAS100"} <= set(symbols)
    assert {"XAUUSD", "XAGUSD"} <= set(symbols)
    assert len(symbols) == 15


def test_no_currency_pairs_are_watched_by_default():
    """An opening range is a bet on an opening auction. Spot FX has none -- its
    "open" is a gradual handover of liquidity, so the first fifteen minutes are
    not special and what comes out is mostly spread."""
    pairs = {"EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "EURGBP"}
    assert not pairs & set(signal_settings.DEFAULT_SYMBOLS)


def test_the_console_and_the_window_watch_the_same_things():
    """Two copies of a watchlist is two watchlists, and the one nobody edits is
    the one that quietly disagrees."""
    assert signals.DEFAULT_SYMBOLS is signal_settings.DEFAULT_SYMBOLS


def test_a_stock_gets_the_fifteen_minute_range_not_the_forex_thirty():
    """The per-instrument suggestion is off by default, but wrong-by-default is
    a trap laid for whoever turns it on: a stock opens on an auction, so it
    belongs with the indices."""
    from cheese_signals.markets.orb import suggest_range_minutes

    for stock in ("AAPL", "NVDA", "TSLA"):
        assert suggest_range_minutes(stock) == 15
    assert suggest_range_minutes("NAS100") == 15
    assert suggest_range_minutes("XAUUSD") == 30
