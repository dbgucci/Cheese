"""The config file that a double-clicked exe is driven by."""

import json

import pytest

from cheese_signals.markets.config import TraderConfig


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("KPS_MARKETS_HOME", str(tmp_path))
    return tmp_path


# ------------------------------ the live switch ------------------------------
def test_live_is_off_by_default():
    """There is no dialog that can turn trading on -- only editing the file."""
    assert TraderConfig().live is False


def test_a_fresh_install_writes_a_config_that_does_not_trade(_home):
    cfg = TraderConfig.load()
    written = json.loads((_home / "trader.json").read_text())
    assert written["live"] is False
    assert cfg.live is False


def test_a_corrupt_config_falls_back_to_safe_defaults(_home):
    (_home / "trader.json").write_text("{not json")
    assert TraderConfig.load().live is False


def test_unknown_keys_from_an_older_build_are_ignored(_home):
    (_home / "trader.json").write_text(json.dumps({"live": True, "wat": 1}))
    cfg = TraderConfig.load()
    assert cfg.live is True and not hasattr(cfg, "wat")


def test_settings_survive_a_round_trip(_home):
    cfg = TraderConfig()
    cfg.risk = 0.0025
    cfg.symbols = ["XAUUSD"]
    cfg.max_spread_points = {"XAUUSD": 30.0}
    cfg.save()
    back = TraderConfig.load()
    assert back.risk == 0.0025
    assert back.symbols == ["XAUUSD"]
    assert back.max_spread_points == {"XAUUSD": 30.0}


# ------------------------------ the warnings ------------------------------
def test_a_clean_default_config_has_nothing_to_warn_about():
    assert TraderConfig().problems() == []


def test_going_live_with_no_spread_gate_is_called_out():
    """Without it the bot trades at spreads the strategy never saw."""
    cfg = TraderConfig()
    cfg.live = True
    assert any("spread limits" in p for p in cfg.problems())
    cfg.max_spread_points = {"US30": 40.0}
    assert not any("spread limits" in p for p in cfg.problems())


def test_an_oversized_risk_is_stated_as_a_losing_streak():
    cfg = TraderConfig()
    cfg.risk = 0.05
    cfg.max_daily_loss = 0.5
    warning = " ".join(cfg.problems())
    assert "five losses" in warning and "23%" in warning


def test_a_risk_bigger_than_the_daily_limit_is_contradictory():
    cfg = TraderConfig()
    cfg.risk = 0.04
    cfg.max_daily_loss = 0.03
    assert any("one loss stops the day" in p for p in cfg.problems())


def test_turning_the_daily_limit_off_is_called_out():
    cfg = TraderConfig()
    cfg.max_daily_loss = 0.0
    assert any("bad day into a bounded day" in p for p in cfg.problems())


def test_an_empty_symbol_list_is_called_out():
    cfg = TraderConfig()
    cfg.symbols = []
    assert any("nothing will be traded" in p.lower() for p in cfg.problems())


# ------------------------------ the summary ------------------------------
def test_the_summary_states_the_mode_first():
    assert "dry run" in TraderConfig().describe()
    cfg = TraderConfig()
    cfg.live = True
    assert "LIVE" in cfg.describe()


def test_the_readme_is_written_beside_the_config(_home):
    from cheese_signals.markets.config import write_readme

    p = write_readme()
    assert p.exists() and "live" in p.read_text()


# ------------------------------ wiring into the runner ------------------------------
def test_configured_spread_limits_reach_the_executor(monkeypatch):
    """Configured against 'XAUUSD', applied to the broker's 'XAUUSD.r'.

    Matching those exactly would silently disable the gate, which is the
    failure mode that matters here.
    """
    import numpy as np
    import pandas as pd

    from cheese_signals.markets import run as run_mod
    from cheese_signals.markets.mt5_bridge import ReplayFeed, SymbolSpec

    idx = pd.date_range("2026-03-02 13:30", periods=100, freq="1min", tz="UTC")
    frame = pd.DataFrame({"open": 2400.0, "high": 2401.0, "low": 2399.0,
                          "close": 2400.0, "tick_volume": 10.0, "spread": 25.0},
                         index=idx)
    feed = ReplayFeed({"XAUUSD.r": frame},
                      {"XAUUSD.r": SymbolSpec("XAUUSD.r", 0.01, 2, 100.0, 1.0,
                                              0.01, 0.01, 50.0, 25.0)})

    class _Args:
        live = False
        symbols = ["XAUUSD"]
        strategy = "intraday_momentum"
        risk = 0.005
        max_daily_loss = 0.03
        max_positions = 2
        max_trades = 6
        blackout_minutes = 15
        evaluate_every = 30
        max_spread_points = {"XAUUSD": 30.0}

    trader, _, _ = run_mod.make_trader(feed, _Args())
    assert trader.executor.config.max_spread_points == {"XAUUSD.r": 30.0}
