"""The impulse continuation setup and its preset.

This rule set came from outside the project, forward-tested by someone else
on USDCAD OTC. Reimplemented here from its published description so it can be
run on this app's feed as a second, independent evidence stream.

The rules, as specified:

  * Heikin Ashi close on the correct side of the Keltner EMA-20 midline
  * raw close on the correct side of EMA 200
  * EMA 20 five-bar slope >= 0.05 ATR in the trade direction
  * ADX 14 in [20, 35)
  * signal candle range >= 1.00 ATR 14
  * close in the directional outer 30% of the candle
  * three complete cooldown candles between accepted signals
  * entry on the confirmed close, 1-minute expiry

Only the slope gate is new; the rest already existed as the trend setup, the
momentum trigger and the ADX band. What matters most here is that the preset
turns *off* the filters the rule set does not include -- the confidence gate
and the higher-timeframe bias would otherwise silently change the strategy.
"""

from dataclasses import fields

import numpy as np
import pandas as pd
import pytest

from cheese_signals import setups, triggers
from cheese_signals.settings import PRESET_IMPULSE_USDCAD, PRESETS, Settings
from cheese_signals.strategies import DOWN, FLAT, UP


def _frame(closes, rng=0.0006):
    """A frame whose bars close at their extreme, so the trigger is satisfiable."""
    idx = pd.date_range("2026-08-04", periods=len(closes), freq="1min", tz="UTC")
    c = np.asarray(closes, dtype=float)
    up = np.r_[True, np.diff(c) >= 0]
    high = np.where(up, c, c + rng)
    low = np.where(up, c - rng, c)
    return pd.DataFrame(
        {"open": np.r_[c[0], c[:-1]], "high": high, "low": low, "close": c,
         "volume": 100.0},
        index=idx,
    )


def _stalled():
    """A frame that passes the trend rules but whose mid-line has stopped.

    A plain plateau does not test this: the EMA-20 keeps catching up to price
    for a long time after a rise, so its slope stays well above the threshold.
    What actually stalls it is chop -- price oscillating around a level long
    enough for the catch-up to finish.
    """
    rise = [1.10 + 0.0002 * i for i in range(250)]
    top = rise[-1]
    chop = [top + (0.0004 if i % 2 == 0 else -0.0004) for i in range(40)]
    return _frame(rise + chop)


def _disagreeing():
    """A frame the trend setup refuses: Keltner says BUY, the EMA 200 says SELL."""
    fall = [1.16 - 0.0002 * i for i in range(280)]
    bounce = [fall[-1] + 0.0006 * i for i in range(1, 21)]
    return _frame(fall + bounce)


def _cfg(**over):
    cfg = setups.SetupConfig(kind=setups.SETUP_IMPULSE)
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


# ------------------------------ the slope gate ------------------------------
def test_a_travelling_mid_line_passes():
    df = _frame([1.10 + 0.0002 * i for i in range(300)])
    checks = []
    assert setups._bias_impulse(df, _cfg(), checks) == UP
    assert any(c.name == "mid-line slope" and c.passed for c in checks)


def test_a_flat_mid_line_is_rejected():
    """The whole point: on the right side of a mid-line that is not moving."""
    checks = []
    assert setups._bias_impulse(_stalled(), _cfg(), checks) == FLAT
    slope = [c for c in checks if c.name == "mid-line slope"]
    assert slope and not slope[0].passed
    # The trend rules on their own were satisfied -- only the gate stopped it.
    assert all(c.passed for c in checks if c.name != "mid-line slope")


def test_the_slope_must_point_the_way_the_trade_does():
    df = _frame([1.10 - 0.0002 * i for i in range(300)])
    checks = []
    assert setups._bias_impulse(df, _cfg(), checks) == DOWN


def test_raising_the_threshold_rejects_a_gentle_slope():
    df = _frame([1.10 + 0.00002 * i for i in range(300)])
    gentle = setups._bias_impulse(df, _cfg(impulse_slope_atr=0.01), [])
    strict = setups._bias_impulse(df, _cfg(impulse_slope_atr=0.60), [])
    assert gentle == UP and strict == FLAT


def test_it_is_trend_continuation_plus_the_gate_and_nothing_else():
    """Whatever the trend setup refuses, the impulse setup must also refuse."""
    df = _disagreeing()
    assert setups._bias_trend(df, _cfg(kind=setups.SETUP_TREND), []) == FLAT
    checks = []
    assert setups._bias_impulse(df, _cfg(), checks) == FLAT
    assert not any(c.name == "mid-line slope" for c in checks), (
        "the slope was evaluated for a setup that had already been refused"
    )


def test_the_slope_is_reported_even_when_it_fails():
    """Diagnostics has to show the number, or the filter is unauditable."""
    checks = []
    setups._bias_impulse(_stalled(), _cfg(), checks)
    detail = [c.detail for c in checks if c.name == "mid-line slope"][0]
    assert "ATR over 5 bars" in detail and "need" in detail


def test_it_is_selectable_and_documented():
    assert setups.SETUP_IMPULSE in setups.SETUPS
    assert setups.SETUP_HELP[setups.SETUP_IMPULSE]
    assert setups.SetupConfig(kind=setups.SETUP_IMPULSE).min_bars() == 220


def test_evaluate_routes_to_it():
    df = _frame([1.10 + 0.0002 * i for i in range(300)])
    trig = triggers.TriggerConfig(kind=triggers.TRIGGER_MOMENTUM,
                                  momentum_range_atr=0.1, momentum_close_pct=0.6)
    sig, checks = setups.evaluate(df, _cfg(), trig)
    assert sig.direction == UP
    assert any(c.name == "mid-line slope" for c in checks)


# --------------------------------- the preset ---------------------------------
def test_the_preset_reproduces_the_published_rules():
    s = Settings()
    s.apply_preset(PRESET_IMPULSE_USDCAD)
    assert s.assets == ["USDCAD_otc"]
    assert s.strategy == "impulse_continuation"
    assert s.trigger == "momentum"
    assert (s.adx_min, s.adx_max) == (20.0, 35.0)
    assert s.momentum_range_atr == 1.00
    assert s.momentum_close_pct == 0.70      # close in the outer 30%
    assert (s.impulse_slope_bars, s.impulse_slope_atr) == (5, 0.05)
    assert s.cooldown_minutes == 4           # three complete candles between signals
    assert s.expiry_minutes == 1 and not s.adaptive_expiry
    assert s.lead_minutes == 0               # act on the confirmed close


def test_the_preset_turns_off_what_the_rules_do_not_include():
    """The failure mode this exists to prevent: a silently different strategy."""
    s = Settings()
    assert s.min_score == 0.60 and s.use_higher_timeframe_bias
    s.apply_preset(PRESET_IMPULSE_USDCAD)
    assert s.min_score == 0.0, "the confidence gate would filter their signals"
    assert not s.use_higher_timeframe_bias
    assert not s.require_liquidity_sweep
    assert not s.restrict_to_sessions
    assert s.win_cooldown_minutes == 0 and s.loss_cooldown_minutes == 0
    assert not s.martingale_enabled


def test_the_preset_is_observation_only():
    s = Settings()
    s.trade_mode = "live"
    s.live_confirmed = True
    s.apply_preset(PRESET_IMPULSE_USDCAD)
    assert s.trade_mode == "off" and not s.live_confirmed


def test_the_preset_reports_what_it_changed():
    s = Settings()
    changed = s.apply_preset(PRESET_IMPULSE_USDCAD)
    assert any(c.startswith("strategy:") for c in changed)
    assert s.apply_preset(PRESET_IMPULSE_USDCAD) == [], "applying twice changed something"


def test_every_preset_key_is_a_real_setting():
    known = {f.name for f in fields(Settings)}
    for name, spec in PRESETS.items():
        unknown = set(spec["settings"]) - known
        assert not unknown, f"{name} sets fields that do not exist: {unknown}"
        assert spec["note"]


def test_an_unknown_preset_is_an_error():
    with pytest.raises(KeyError):
        Settings().apply_preset("no such preset")


def test_the_preset_survives_a_save_and_load(tmp_path, monkeypatch):
    from cheese_signals import paths

    monkeypatch.setattr(paths, "settings_path", lambda: tmp_path / "settings.json")
    s = Settings()
    s.apply_preset(PRESET_IMPULSE_USDCAD)
    s.save()
    back = Settings.load()
    assert back.strategy == "impulse_continuation"
    assert back.assets == ["USDCAD_otc"]
    assert (back.impulse_slope_bars, back.impulse_slope_atr) == (5, 0.05)


# --------------------------- the warnings that guard it ---------------------------
def _preset_settings():
    s = Settings()
    s.apply_preset(PRESET_IMPULSE_USDCAD)
    return s


def test_the_preset_as_loaded_raises_no_complaints():
    assert _preset_settings().conflicts() == []


def test_editing_a_rule_says_the_test_is_no_longer_the_test():
    s = _preset_settings()
    s.momentum_range_atr = 0.5
    assert any("not the impulse rule set as specified" in c for c in s.conflicts())


def test_moving_the_adx_band_is_called_out_specifically():
    s = _preset_settings()
    s.adx_max = 40.0
    text = " ".join(s.conflicts())
    assert "most fragile part" in text and "54.9%" in text


def test_turning_execution_on_is_called_out():
    s = _preset_settings()
    s.trade_mode = "paper"
    assert any("still under test" in c for c in s.conflicts())


def test_adding_pairs_is_called_out_with_how_they_measured():
    s = _preset_settings()
    s.assets = ["USDCAD_otc", "EURUSD_otc"]
    assert any("51.8%" in c for c in s.conflicts())


# ------------------------------ through the engine ------------------------------
def test_the_preset_produces_a_signal_end_to_end(tmp_path):
    """The rules firing in isolation is not the same as the engine firing.

    The preset switches off the confidence gate and the higher-timeframe
    bias precisely so the engine does not filter signals the rule set would
    have taken; this checks that it really doesn't.
    """
    from datetime import datetime, timezone

    from cheese_signals import storage
    from cheese_signals.engine import SignalEngine

    df = _frame([1.10 + 0.0002 * i for i in range(300)])
    now = datetime(2026, 8, 4, 5, 0, tzinfo=timezone.utc)

    s = Settings()
    s.apply_preset(PRESET_IMPULSE_USDCAD)
    s.adx_min, s.adx_max = 0.0, 100.0     # the synthetic ramp has no ADX in band

    captured = {"signals": [], "errors": []}

    class _Feed:
        def get_candles(self, count):
            return df.tail(count)

    eng = SignalEngine(
        settings=s, journal=storage.Journal(tmp_path / "i.db"),
        feed_factory=lambda a: _Feed(), notifier=None,
        on_signal=captured["signals"].append, on_error=captured["errors"].append,
    )
    eng._scan_asset("USDCAD_otc", now)

    assert not captured["errors"]
    assert captured["signals"], "the preset produced no signal on a clean impulse"
    sig = captured["signals"][0]
    assert sig.direction == UP
    assert sig.strategy.startswith("impulse_continuation")
    assert sig.lead_seconds == 0, "the rules act on the confirmed close"
    assert (sig.expiry_at - sig.entry_at).total_seconds() == 60


def test_the_confidence_gate_would_otherwise_have_blocked_it(tmp_path):
    """Shows the preset's min_score=0.0 is load-bearing, not decoration."""
    df = _frame([1.10 + 0.0002 * i for i in range(300)])
    trig = triggers.TriggerConfig(kind=triggers.TRIGGER_MOMENTUM,
                                  momentum_range_atr=1.0, momentum_close_pct=0.70)
    sig, _ = setups.evaluate(df, _cfg(), trig)
    if sig.direction != FLAT:
        assert sig.score < Settings().min_score, (
            "if impulse signals scored above the default gate this test proves nothing"
        )
