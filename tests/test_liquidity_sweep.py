import numpy as np
import pandas as pd

from cheese_signals import profiles, strategies
from cheese_signals.strategies import DOWN, FLAT, UP


def _frame(rows):
    idx = pd.date_range("2026-08-01", periods=len(rows), freq="1min", tz="UTC")
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"]).assign(volume=100.0)


def _base_series(n=80, price=1.1000, jitter=0.00015, seed=0):
    """A choppy range that produces confirmed swing highs/lows."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        wave = np.sin(i / 3.0) * jitter
        c = price + wave + rng.normal(0, jitter / 6)
        o = price + np.sin((i - 1) / 3.0) * jitter
        h = max(o, c) + abs(rng.normal(0, jitter / 5))
        l = min(o, c) - abs(rng.normal(0, jitter / 5))
        rows.append([o, h, l, c])
    return rows


def test_no_signal_without_enough_history():
    df = _frame(_base_series(20))
    assert strategies.liquidity_sweep(df).direction == FLAT


def test_sweep_of_highs_gives_sell_signal():
    rows = _base_series(80)
    resistance = max(r[1] for r in rows[:-6])
    # Final candle: wick far above the swing high, body closes back below it.
    o = resistance - 0.0004
    c = resistance - 0.0008
    h = resistance + 0.0010
    l = c - 0.00005
    rows.append([o, h, l, c])
    sig = strategies.liquidity_sweep(_frame(rows))
    assert sig.direction == DOWN
    assert "buy-side liquidity" in sig.reason
    assert sig.tags == ["liquidity_sweep"]


def test_sweep_of_lows_gives_buy_signal():
    rows = _base_series(80)
    support = min(r[2] for r in rows[:-6])
    o = support + 0.0004
    c = support + 0.0008
    l = support - 0.0010
    h = c + 0.00005
    rows.append([o, h, l, c])
    sig = strategies.liquidity_sweep(_frame(rows))
    assert sig.direction == UP
    assert "sell-side liquidity" in sig.reason


def test_close_beyond_level_is_a_breakout_not_a_sweep():
    """A candle that closes past the level is a breakout -- must not fire a reversal."""
    rows = _base_series(80)
    resistance = max(r[1] for r in rows[:-6])
    o = resistance - 0.0002
    c = resistance + 0.0010          # closes ABOVE the level
    h = c + 0.0002
    l = o - 0.0001
    rows.append([o, h, l, c])
    assert strategies.liquidity_sweep(_frame(rows)).direction != DOWN


def test_weak_displacement_is_rejected():
    """A sweep on an indecisive (tiny-body) candle is noise, not a setup."""
    rows = _base_series(80)
    resistance = max(r[1] for r in rows[:-6])
    o = resistance - 0.0002
    c = resistance - 0.00021         # near-zero body => tiny displacement
    h = resistance + 0.0010
    l = c - 0.00005
    rows.append([o, h, l, c])
    assert strategies.liquidity_sweep(_frame(rows)).direction == FLAT


# ------------------------------- profiles -------------------------------
def test_otc_profile_demotes_trend_and_favours_reversion():
    p = profiles.profile_for("EURUSD_otc")
    assert p.name == "otc"
    assert p.weights["trend"] < p.weights["mean_reversion"]
    assert p.weights["liquidity_sweep"] >= p.weights["mean_reversion"]
    # OTC demands a stronger ADX before believing a trend at all.
    assert p.adx_trend_min > profiles.LIVE_PROFILE.adx_trend_min


def test_live_profile_trusts_trend():
    p = profiles.profile_for("EURUSD")
    assert p.name == "live"
    assert p.weights["trend"] == 1.0
