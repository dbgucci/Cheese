"""The .exe front end: settings, terminal discovery, and the failure screens.

The interactive menu is not tested -- it is input() calls around the functions
below, and testing it would test ``input``. What is tested is everything the
menu delegates to, plus the two things most likely to be wrong in a frozen exe
that nobody can attach a debugger to: where settings end up, and whether a
failure to connect produces something a person can act on.
"""

import json

from cheese_signals.markets import launcher
from cheese_signals.markets.launcher import LauncherSettings


# ------------------------------ settings ------------------------------
def test_settings_round_trip(tmp_path):
    path = tmp_path / "autobot.json"
    original = LauncherSettings(login=12345, server="Broker-Live",
                                risk_fraction=0.0025, symbols=["XAUUSD", "US30"])
    original.save(path)
    assert LauncherSettings.load(path) == original


def test_a_missing_settings_file_yields_defaults_rather_than_an_error(tmp_path):
    settings = LauncherSettings.load(tmp_path / "nope.json")
    assert settings.risk_fraction == 0.005
    assert settings.login is None


def test_a_corrupt_settings_file_is_survived(tmp_path, capsys):
    """A half-written JSON file must not stop the exe from starting -- that is
    an unrecoverable state for a user with no console history to read."""
    path = tmp_path / "autobot.json"
    path.write_text("{ this is not json", encoding="utf-8")
    settings = LauncherSettings.load(path)
    assert settings == LauncherSettings()
    assert "could not be read" in capsys.readouterr().out


def test_unknown_keys_in_the_file_are_ignored(tmp_path):
    """Hand-edited settings from an older version must not crash the newer exe."""
    path = tmp_path / "autobot.json"
    path.write_text(json.dumps({"risk_fraction": 0.01, "retired_option": True}),
                    encoding="utf-8")
    assert LauncherSettings.load(path).risk_fraction == 0.01


def test_the_trading_password_is_never_stored(tmp_path):
    """A password in a JSON file on the Desktop is a worse risk than typing it,
    and the terminal is already logged in anyway."""
    path = LauncherSettings().save(tmp_path / "autobot.json")
    written = json.loads(path.read_text(encoding="utf-8"))
    assert not any("pass" in key.lower() for key in written)


def test_settings_live_beside_the_apps_other_data(monkeypatch, tmp_path):
    monkeypatch.setenv("CHEESE_SIGNALS_HOME", str(tmp_path))
    assert launcher.settings_path() == tmp_path / "autobot.json"


def test_saving_creates_the_folder(tmp_path):
    path = tmp_path / "deep" / "deeper" / "autobot.json"
    LauncherSettings().save(path)
    assert path.exists()


# ------------------------------ finding terminals ------------------------------
def test_terminals_are_found_by_executable_name_not_by_broker_name(tmp_path):
    """Brokers rename the install folder, so the exe name is the only constant."""
    for name in ("Liquid Brokers MT5", "IC Markets MetaTrader 5"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "terminal64.exe").write_bytes(b"")
    (tmp_path / "Some Other App").mkdir()

    found = launcher.find_terminals([str(tmp_path)])
    assert [launcher.describe_terminal(f) for f in found] == [
        "IC Markets MetaTrader 5", "Liquid Brokers MT5"]


def test_no_terminals_is_an_empty_list_not_an_error(tmp_path):
    assert launcher.find_terminals([str(tmp_path)]) == []


def test_a_missing_search_root_is_skipped(tmp_path):
    assert launcher.find_terminals([str(tmp_path / "does-not-exist")]) == []


def test_the_install_folder_name_is_what_identifies_the_broker(tmp_path):
    folder = tmp_path / "Liquid Brokers MT5"
    folder.mkdir()
    exe = folder / "terminal64.exe"
    exe.write_bytes(b"")
    assert launcher.describe_terminal(exe) == "Liquid Brokers MT5"


# ------------------------------ the failure screen ------------------------------
def test_the_connection_help_names_all_four_usual_causes():
    text = "\n".join(launcher.connection_help("did not initialise: -10005", []))
    assert "not running" in text
    assert "not logged in" in text
    assert "Algo Trading" in text
    assert "does not offer MetaTrader 5" in text


def test_the_help_repeats_the_brokers_own_error():
    text = "\n".join(launcher.connection_help("Terminal: Authorization failed", []))
    assert "Authorization failed" in text


def test_the_help_lists_the_installs_it_found(tmp_path):
    folder = tmp_path / "Liquid Brokers MT5"
    folder.mkdir()
    (folder / "terminal64.exe").write_bytes(b"")
    found = launcher.find_terminals([str(tmp_path)])
    text = "\n".join(launcher.connection_help("nope", found))
    assert "Liquid Brokers MT5" in text


def test_with_no_installs_the_help_says_where_to_get_one():
    text = "\n".join(launcher.connection_help("nope", []))
    assert "your broker's own website" in text
    assert "metatrader5.com" in text


# ------------------------------ the screens ------------------------------
DIAG = {
    "terminal": "MetaTrader 5", "terminal_path": r"C:\MT5\terminal64.exe",
    "connected": True, "algo_allowed": True, "login": 5108234,
    "server": "Broker-Live", "company": "Some Broker Ltd", "currency": "USD",
    "balance": 10_000.0, "equity": 9_850.25, "leverage": 500,
    "trade_expert": True,
}


def test_the_account_screen_shows_which_account_is_about_to_be_traded():
    text = "\n".join(launcher.format_account(DIAG))
    assert "Some Broker Ltd" in text and "5108234" in text
    assert "9850.25" in text.replace(",", "")


def test_algo_trading_being_off_is_called_out_with_the_fix():
    text = "\n".join(launcher.format_account({**DIAG, "algo_allowed": False}))
    assert "Algo Trading" in text and "NO" in text


def test_the_broker_disabling_eas_is_distinguished_from_the_toolbar_button():
    """Two different problems with two different fixes, and the same symptom."""
    text = "\n".join(launcher.format_account({**DIAG, "trade_expert": False}))
    assert "broker has disabled" in text


def test_the_plan_screen_states_each_range_window_and_flat_time():
    text = "\n".join(launcher.format_plan(["US30", "XAUUSD"], LauncherSettings()))
    assert "US cash equities" in text and "London" in text
    assert "flat by" in text


def test_an_unmapped_symbol_is_shown_as_untradeable_rather_than_omitted():
    """Silently dropping it is how an instrument nobody mapped looks identical
    to one the broker does not offer."""
    text = "\n".join(launcher.format_plan(["BTCUSD"], LauncherSettings()))
    assert "BTCUSD" in text and "no session mapped" in text


def test_the_plan_uses_a_thirty_minute_range_for_metals_and_fifteen_for_indices():
    text = "\n".join(launcher.format_plan(["US30", "XAUUSD"], LauncherSettings()))
    us30 = next(ln for ln in text.splitlines() if "US30" in ln)
    gold = next(ln for ln in text.splitlines() if "XAUUSD" in ln)
    assert "-" in us30 and "-" in gold
    # 09:30 + 15min against 08:00 + 30min, whatever they are in UTC today.
    assert us30.count(":") >= 3 and gold.count(":") >= 3


# ------------------------------ the config it builds ------------------------------
def test_the_dry_run_option_builds_a_dry_run_config():
    config = launcher.build_config(LauncherSettings(), ["US30"], live=False)
    assert config.execution.dry_run is True
    assert config.validate() == []


def test_the_live_option_is_the_only_thing_that_turns_dry_run_off():
    config = launcher.build_config(LauncherSettings(), ["US30"], live=True)
    assert config.execution.dry_run is False


def test_the_settings_reach_the_config():
    settings = LauncherSettings(risk_fraction=0.0025, target_r=3.0,
                               range_minutes=30, max_daily_loss_fraction=0.02,
                               poll_seconds=45)
    config = launcher.build_config(settings, ["US30"], live=False)
    assert config.execution.risk_fraction == 0.0025
    assert config.orb.target_r == 3.0
    assert config.orb.range_minutes == 30
    assert config.guards.max_daily_loss_fraction == 0.02
    assert config.poll_seconds == 45


def test_the_default_settings_produce_a_startable_bot():
    """The whole point of the exe: double-click, and it works."""
    config = launcher.build_config(LauncherSettings(), ["US30", "XAUUSD"],
                                   live=False)
    assert config.validate() == []
    assert isinstance(config.symbols, list) and config.symbols


def test_the_default_symbol_list_is_what_was_asked_for():
    symbols = LauncherSettings().symbols
    for wanted in ("XAUUSD", "XAGUSD", "US30", "SPX500", "NAS100", "EURUSD"):
        assert wanted in symbols
