"""Setups, triggers, and the promise that every setting actually does something.

The last item matters most: a control that cannot change behaviour is worse
than a missing one, because it invites the user to tune something inert.
"""

import numpy as np
import pandas as pd
import pytest

from cheese_signals import setups, triggers as trig
from cheese_signals.settings import Settings
from cheese_signals.strategies import DOWN, FLAT, UP


def _frame(closes, spread=0.0002):
    idx = pd.date_range("2026-08-01", periods=len(closes), freq="1min", tz="UTC")
    c = pd.Series(closes, index=idx)
    o = c.shift(1).fillna(c.iloc[0])
    return pd.DataFrame({
        "open": o, "high": np.maximum(o, c) + spread,
        "low": np.minimum(o, c) - spread, "close": c, "volume": 100.0,
    })


def _trend_frame(n=400, step=0.00012, seed=0, noise=0.00012):
    rng = np.random.default_rng(seed)
    return _frame([1.10 + i * step + np.sin(i / 5.0) * 0.00035 + rng.normal(0, noise)
                   for i in range(n)])


def _range_frame(n=300, seed=1):
    rng = np.random.default_rng(seed)
    return _frame([1.10 + np.sin(i / 8.0) * 0.0012 + rng.normal(0, 0.00008) for i in range(n)])


# -------------------------------- triggers --------------------------------
def test_bos_fires_on_the_breaking_candle_not_later():
    """The whole point of BOS: no lag beyond the bar that breaks."""
    closes = [1.1000] * 40
    closes += [1.1010, 1.1005, 1.1002, 1.1004, 1.1006]   # swing high then pullback
    df = _frame(closes + [1.1030])                        # decisive break
    cfg = trig.TriggerConfig(kind=trig.TRIGGER_BOS, bos_lookback=40)
    res = trig.evaluate(df, UP, cfg)
    assert res.fired
    assert "broke structure" in res.detail


def test_bos_does_not_fire_before_the_break():
    closes = [1.1000] * 40 + [1.1010, 1.1005, 1.1002, 1.1004, 1.1006]
    df = _frame(closes)
    res = trig.evaluate(df, UP, trig.TriggerConfig(kind=trig.TRIGGER_BOS, bos_lookback=40))
    assert not res.fired


def test_bos_buffer_requires_a_wider_break():
    # The level is a fractal HIGH (which includes the wick), so the breaking
    # close has to clear that, not merely the prior close.
    closes = [1.1000] * 40 + [1.1010, 1.1005, 1.1002, 1.1004, 1.1006, 1.1013]
    df = _frame(closes)
    loose = trig.evaluate(df, UP, trig.TriggerConfig(kind=trig.TRIGGER_BOS, bos_lookback=40))
    strict = trig.evaluate(
        df, UP, trig.TriggerConfig(kind=trig.TRIGGER_BOS, bos_lookback=40, bos_buffer_atr=2.0))
    assert loose.fired and not strict.fired


def test_momentum_needs_both_range_and_close_position():
    df = _trend_frame(120)
    # A doji-ish final bar: wide but closing mid-range.
    df.iloc[-1, df.columns.get_loc("open")] = 1.1000
    df.iloc[-1, df.columns.get_loc("close")] = 1.1000
    df.iloc[-1, df.columns.get_loc("high")] = 1.1020
    df.iloc[-1, df.columns.get_loc("low")] = 1.0980
    res = trig.evaluate(df, UP, trig.TriggerConfig(kind=trig.TRIGGER_MOMENTUM))
    assert not res.fired
    assert "range" in res.detail or "extreme" in res.detail


def test_momentum_fires_on_a_decisive_bar():
    df = _trend_frame(120)
    df.iloc[-1, df.columns.get_loc("open")] = 1.1000
    df.iloc[-1, df.columns.get_loc("low")] = 1.0999
    df.iloc[-1, df.columns.get_loc("high")] = 1.1040
    df.iloc[-1, df.columns.get_loc("close")] = 1.1039
    res = trig.evaluate(df, UP, trig.TriggerConfig(
        kind=trig.TRIGGER_MOMENTUM, momentum_range_atr=0.5, momentum_close_pct=0.7))
    assert res.fired


def test_fractal_trigger_respects_its_window():
    df = _trend_frame(400)
    tight = trig.evaluate(df, UP, trig.TriggerConfig(kind=trig.TRIGGER_FRACTAL, fractal_max_age=0))
    loose = trig.evaluate(df, UP, trig.TriggerConfig(kind=trig.TRIGGER_FRACTAL, fractal_max_age=50))
    assert loose.fired or not tight.fired   # widening can only ever help


def test_unknown_trigger_is_refused():
    res = trig.evaluate(_trend_frame(120), UP, trig.TriggerConfig(kind="telepathy"))
    assert not res.fired and "unknown" in res.detail


def test_flat_bias_never_fires():
    for kind in trig.TRIGGERS:
        assert not trig.evaluate(_trend_frame(300), FLAT, trig.TriggerConfig(kind=kind)).fired


# --------------------------------- setups ---------------------------------
def test_every_setup_runs_and_explains_itself():
    df = _trend_frame(400)
    for kind in setups.SETUPS:
        sig, checks = setups.evaluate(df, setups.SetupConfig(kind=kind), trig.TriggerConfig())
        assert checks, f"{kind} produced no explanation"
        assert any(c.name == "history" for c in checks)
        assert sig.direction in (UP, DOWN, FLAT)


def test_trend_setup_only_buys_in_an_uptrend():
    df = _trend_frame(500)
    cfg = setups.SetupConfig(kind=setups.SETUP_TREND)
    dirs = set()
    for i in range(cfg.min_bars(), len(df), 5):
        sig, _ = setups.evaluate(df.iloc[:i + 1], cfg, trig.TriggerConfig())
        if sig.is_actionable:
            dirs.add(sig.direction)
    assert DOWN not in dirs


def test_short_history_is_reported_not_silent():
    sig, checks = setups.evaluate(_frame([1.1] * 30), setups.SetupConfig(), trig.TriggerConfig())
    assert not sig.is_actionable
    hist = next(c for c in checks if c.name == "history")
    assert not hist.passed and "need" in hist.detail


def test_adx_band_blocks_and_says_so():
    df = _trend_frame(400)
    cfg = setups.SetupConfig(kind=setups.SETUP_TREND, adx_min=99, adx_max=100)
    sig, checks = setups.evaluate(df, cfg, trig.TriggerConfig())
    assert not sig.is_actionable
    assert any(c.name == "ADX band" and not c.passed for c in checks)


def test_setup_and_trigger_are_independent():
    """Any setup must be pairable with any trigger."""
    df = _trend_frame(500)
    for setup_kind in setups.SETUPS:
        for trigger_kind in trig.TRIGGERS:
            sig, checks = setups.evaluate(
                df, setups.SetupConfig(kind=setup_kind), trig.TriggerConfig(kind=trigger_kind))
            assert sig.direction in (UP, DOWN, FLAT)
            assert checks


# ------------------------- settings actually apply -------------------------
def test_settings_build_matching_configs():
    s = Settings()
    s.strategy = "reversal"; s.trigger = "momentum"
    s.rsi_period = 9; s.momentum_range_atr = 1.4
    assert s.setup_config().kind == "reversal"
    assert s.setup_config().rsi_period == 9
    assert s.trigger_config().kind == "momentum"
    assert s.trigger_config().momentum_range_atr == 1.4


def _volatile_frame(n=400, seed=5):
    """Choppy with sharp swings, so oscillator and channel periods actually bite."""
    rng = np.random.default_rng(seed)
    return _frame([
        1.10 + np.sin(i / 7.0) * 0.0018 + np.sin(i / 2.3) * 0.0007 + rng.normal(0, 0.0002)
        for i in range(n)
    ])


@pytest.mark.parametrize("field,value,other,kind", [
    ("ema_trend", 50, 200, setups.SETUP_TREND),
    ("keltner_ema", 4, 40, setups.SETUP_TREND),
    ("sr_lookback", 10, 60, setups.SETUP_SR),
    ("rsi_period", 3, 21, setups.SETUP_REVERSAL),
    ("rsi_oversold", 45.0, 15.0, setups.SETUP_REVERSAL),
    ("sr_reject_pct", 0.15, 0.85, setups.SETUP_SR),
])
def test_setup_parameters_change_behaviour(field, value, other, kind):
    """A parameter that changes nothing is a lie in the UI."""
    df = _volatile_frame(400)

    def signature(v):
        cfg = setups.SetupConfig(kind=kind, **{field: v})
        out = []
        for i in range(cfg.min_bars(), len(df), 2):
            sig, checks = setups.evaluate(
                df.iloc[:i + 1], cfg, trig.TriggerConfig(kind=trig.TRIGGER_MOMENTUM,
                                                        momentum_range_atr=0.3,
                                                        momentum_close_pct=0.55))
            # Compare the reasoning too: a parameter can legitimately change
            # which conditions pass without flipping the final verdict.
            out.append((sig.direction, round(sig.score, 3), tuple(c.passed for c in checks)))
        return out

    assert signature(value) != signature(other), f"changing {field} had no effect"


def test_trigger_choice_changes_behaviour():
    df = _trend_frame(500)
    cfg = setups.SetupConfig(kind=setups.SETUP_TREND)

    def count(kind):
        return sum(
            setups.evaluate(df.iloc[:i + 1], cfg, trig.TriggerConfig(kind=kind))[0].is_actionable
            for i in range(cfg.min_bars(), len(df), 3)
        )

    counts = {k: count(k) for k in trig.TRIGGERS}
    assert len(set(counts.values())) > 1, f"all triggers behaved identically: {counts}"


# ------------------------------- conflicts --------------------------------
def test_inverted_adx_band_is_flagged():
    s = Settings(); s.adx_min = 60; s.adx_max = 10
    assert any("inverted" in c for c in s.conflicts())


def test_inert_setting_is_flagged():
    s = Settings(); s.trigger = "bos"; s.fractal_max_age = 12
    assert any("only affects the fractal trigger" in c for c in s.conflicts())


def test_live_without_confirmation_is_flagged():
    s = Settings(); s.trade_mode = "live"; s.live_confirmed = False
    assert any("unconfirmed" in c for c in s.conflicts())


def test_balance_below_floor_is_flagged():
    s = Settings(); s.trade_mode = "paper"; s.account_balance = 40; s.min_balance = 50
    assert any("floor" in c for c in s.conflicts())


def test_session_restriction_without_sessions_is_flagged():
    s = Settings(); s.restrict_to_sessions = True; s.allowed_sessions = []
    assert any("no sessions are selected" in c for c in s.conflicts())


def test_default_settings_have_no_conflicts():
    """A fresh install must not open with a warning banner.

    If the defaults trip a conflict check, one of the two is wrong -- and a
    warning the user sees before touching anything trains them to ignore all
    of them.
    """
    assert Settings().conflicts() == []


# ------------------------------ the lab ----------------------------------
def _random_walk(n=2500, seed=7, sigma=0.00035):
    """A walk with *varying* wicks.

    Constant-width wicks would make the wick-fraction tests (rejection,
    momentum close position) degenerate, and the setup would simply never fire
    -- which would make this whole check pass vacuously.
    """
    rng = np.random.default_rng(seed)
    closes = 1.10 + np.cumsum(rng.normal(0, sigma, n))
    idx = pd.date_range("2026-08-01", periods=n, freq="1min", tz="UTC")
    c = pd.Series(closes, index=idx)
    o = c.shift(1).fillna(c.iloc[0])
    hi = np.maximum(o, c) + np.abs(rng.normal(0, sigma, n))
    lo = np.minimum(o, c) - np.abs(rng.normal(0, sigma, n))
    return pd.DataFrame({"open": o, "high": hi, "low": lo, "close": c, "volume": 100.0})


@pytest.mark.parametrize("trigger_kind", list(trig.TRIGGERS))
def test_no_edge_on_a_random_walk(trigger_kind):
    """Every trigger must score ~50% on a random walk.

    A walk has no exploitable structure, so anything scoring well above chance
    is reading data it could not have had live. This is the check that catches
    lookahead bias in a *trigger*, which is exactly where it would hide: the
    fractal that defines a BOS level is centred on a past bar, and using it one
    bar too early would look like skill here.
    """
    from cheese_signals import strategy_lab as lab

    df = _random_walk()
    name = lab.combo_name(setups.SETUP_TREND, trigger_kind)
    result = lab.run(df, name, expiry_minutes=3)
    assert result.trades >= 30, (
        f"{name} produced only {result.trades} trades -- too few for this check to mean anything"
    )
    assert 0.40 < result.win_rate < 0.60, (
        f"{name} scored {result.win_rate:.1%} on a random walk -- implies lookahead bias"
    )


def test_every_setup_trigger_pair_is_registered_in_the_lab():
    from cheese_signals import strategy_lab as lab

    for s_kind in setups.SETUPS:
        for t_kind in trig.TRIGGERS:
            name = lab.combo_name(s_kind, t_kind)
            assert name in lab.STRATEGIES, f"{name} missing from the lab"
            assert name in lab.WARMUP, f"{name} has no warmup"


def test_lab_combos_follow_saved_settings():
    """Registering from settings must change what the lab measures."""
    from cheese_signals import strategy_lab as lab

    df = _random_walk(1200)
    name = lab.combo_name(setups.SETUP_TREND, trig.TRIGGER_MOMENTUM)

    s = Settings()
    s.momentum_range_atr = 6.0     # a bar this wide essentially never occurs
    lab.register_combos(s.setup_config(), s.trigger_config())
    strict = lab.run(df, name, expiry_minutes=2)

    lab.register_combos()          # back to defaults
    loose = lab.run(df, name, expiry_minutes=2)
    assert strict.trades < loose.trades, (
        f"saved settings were ignored: {strict.trades} vs {loose.trades} trades"
    )


# --------------------------- the confidence score ---------------------------
def test_the_score_does_not_use_adx(tmp_path):
    """ADX is a regime filter, not a confidence input.

    Adding it made the score anti-calibrated: over 815 live trades ADX
    rank-correlated -0.08 with winning, so a formula that rewarded it
    produced its best results in the *lowest* confidence bucket, in three
    successive journals. It has its own setting (adx_min/adx_max); it does
    not belong in the score as well.
    """
    import inspect

    source = inspect.getsource(setups._score)
    body = source.split('"""')[2]        # skip the docstring
    assert "adx" not in body.lower(), "ADX is back in the score calculation"


def test_the_score_is_documented_as_unvalidated():
    """A number that looks like a probability invites being filtered on."""
    assert "do not gate on it" in (setups._score.__doc__ or "").lower()


def test_scores_still_vary_with_the_candle(tmp_path):
    """Removing ADX must not flatten the score to a constant."""
    seen = set()
    df = _trend_frame(400)
    cfg = setups.SetupConfig()
    for i in range(cfg.min_bars(), len(df), 7):
        seen.add(round(setups._score(df.iloc[: i + 1], UP, cfg), 3))
    assert len(seen) > 5, f"score collapsed to {seen}"


def test_scores_stay_inside_zero_and_one():
    df = _volatile_frame(300) if "_volatile_frame" in dir() else _trend_frame(300)
    cfg = setups.SetupConfig()
    for i in range(cfg.min_bars(), len(df), 5):
        s = setups._score(df.iloc[: i + 1], UP, cfg)
        assert 0.0 <= s <= 1.0
