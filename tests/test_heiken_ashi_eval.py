"""The Heiken Ashi and S/R bots, pinned against the behaviour they actually have.

Most of these assert properties of the code rather than a win rate, because
that is where the problems are: a filter that is satisfied 95% of the time, a
score whose meaning changes with the price of the instrument, and a settlement
that measures a different trade from the one the alert tells you to place.
"""

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("scipy", reason='install with: pip install -e ".[research]"')

from cheese_signals.research import heiken_ashi_eval as hae


# ---------------------------------------------------------------------------
# settlement: the card says one candle, the scheduler measures two
# ---------------------------------------------------------------------------
def test_the_two_settlement_conventions_are_different_trades():
    """`expiry_at = execute_at + 1 candle`, then the close of *that* candle."""
    idx = pd.date_range("2026-08-04", periods=6, freq="1min", tz="UTC")
    # open[k] rises, then the bar after the entry bar collapses.
    df = pd.DataFrame(
        {"open":  [1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
         "high":  [1.1, 1.1, 1.1, 1.1, 1.1, 1.1],
         "low":   [0.9, 0.9, 0.9, 0.9, 0.9, 0.9],
         "close": [1.0, 1.0, 1.05, 0.90, 1.0, 1.0],
         "volume": [1.0] * 6},
        index=idx,
    )
    sig = [hae.Signal(index=1, direction=hae.BUY, confidence=80)]

    one = hae.settle(df, sig, settle_offset=0)    # enter open[2], exit close[2]
    two = hae.settle(df, sig, settle_offset=1)    # enter open[2], exit close[3]

    assert one.wins == 1 and one.losses == 0, "1-candle trade wins"
    assert two.wins == 0 and two.losses == 1, "2-candle hold loses"


def test_flat_settlement_is_a_refund():
    idx = pd.date_range("2026-08-04", periods=4, freq="1min", tz="UTC")
    df = pd.DataFrame(
        {"open": [1.0] * 4, "high": [1.0] * 4, "low": [1.0] * 4,
         "close": [1.0] * 4, "volume": [1.0] * 4},
        index=idx,
    )
    s = hae.settle(df, [hae.Signal(1, hae.BUY, 80)], settle_offset=0)
    assert s.refunds == 1 and s.n == 0


# ---------------------------------------------------------------------------
# the confidence score
# ---------------------------------------------------------------------------
def _strong_bull_row(scale=1.0):
    """A strong bullish HA candle: body/range > 0.6 and no lower wick."""
    return pd.Series({"HA_Open": 1.0 * scale, "HA_Close": 1.8 * scale,
                      "HA_High": 1.9 * scale, "HA_Low": 1.0 * scale})


def test_wick_term_is_always_thirty_when_a_signal_fires():
    """is_strong_ha is a precondition of firing, so its 30 points are constant."""
    row = _strong_bull_row()
    assert hae.is_strong_ha(row, "bullish")
    # ema far away -> ema term maxed; body/range ~0.888 -> body term ~35.5
    high = hae.confidence(row, ema_value=0.0)
    assert high == pytest.approx(round(35.5 + 30 + 30), abs=1)


def test_confidence_has_a_floor_of_54_not_zero():
    """body>0.6*range (24 pts) + wick (30 pts) are both already guaranteed."""
    row = pd.Series({"HA_Open": 1.0, "HA_Close": 1.0 + 0.601,
                     "HA_High": 1.0 + 1.0, "HA_Low": 1.0})
    assert hae.is_strong_ha(row, "bullish")
    assert hae.confidence(row, ema_value=1.0 + 0.601) >= 54


def test_the_ema_term_saturates_on_a_yen_priced_instrument():
    """The 0.001 divisor is an absolute price, so it means different things."""
    eur = _strong_bull_row(scale=1.0)      # ~1.x prices
    jpy = _strong_bull_row(scale=150.0)    # ~150.x prices
    # The *same relative* distance: one basis point below HA_Close.
    eur_c = hae.confidence(eur, ema_value=float(eur["HA_Close"]) * 0.9999)
    jpy_c = hae.confidence(jpy, ema_value=float(jpy["HA_Close"]) * 0.9999)
    assert jpy_c > eur_c, "the same relative distance scores higher on JPY"
    # On the JPY-priced series that distance is already past the cap, so
    # moving the EMA a hundred times further away changes nothing.
    assert jpy_c == hae.confidence(jpy, ema_value=float(jpy["HA_Close"]) * 0.99)


# ---------------------------------------------------------------------------
# the S/R bot's filters
# ---------------------------------------------------------------------------
def test_near_support_or_resistance_is_almost_always_true():
    """A filter satisfied 9 times in 10 is not filtering anything."""
    df = hae.driftless_walk(20000, start_price=1.08, seed=3)
    g = hae.sr_gate_rate(df, samples=120, seed=1)
    assert g["near_support"] > 0.85
    assert g["near_resistance"] > 0.85


def test_the_star_doji_threshold_is_inert_on_forex_prices():
    """`abs(c2 - o2) < 0.1` is ~1000 pips on EURUSD, so every bar passes."""
    df = hae.driftless_walk(5000, start_price=1.08, seed=4)
    assert hae.star_doji_rate(df) == 1.0


# ---------------------------------------------------------------------------
# martingale
# ---------------------------------------------------------------------------
def test_martingale_does_not_change_expected_value():
    """It reshapes the distribution. The house edge is untouched."""
    payout = 0.85
    for p in (0.45, 0.50, 0.55):
        m = hae.martingale_math(p, payout)
        edge = p * payout - (1 - p)
        assert m["ev_per_unit_staked"] == pytest.approx(edge, abs=1e-9)


def test_martingale_inflates_the_reported_win_rate():
    m = hae.martingale_math(0.50, 0.85)
    assert m["displayed_win_rate"] == pytest.approx(0.875)
    assert m["ev_per_sequence"] < 0, "87.5% of sequences win and it still loses"


# ---------------------------------------------------------------------------
# the strategy itself
# ---------------------------------------------------------------------------
def test_heiken_ashi_open_is_causal():
    """HA_Open depends only on prior bars, so poisoning the future changes nothing."""
    df = hae.driftless_walk(1200, seed=9)
    base = hae.heiken_ashi(df.iloc[:900])
    poisoned = df.copy()
    poisoned.iloc[900:] = 999.0
    after = hae.heiken_ashi(poisoned.iloc[:900])
    assert np.allclose(base["HA_Open"].to_numpy(), after["HA_Open"].to_numpy())


def test_scores_a_coin_flip_on_a_driftless_walk():
    rates = []
    for seed in range(6):
        df = hae.driftless_walk(20000, seed=600 + seed)
        sc = hae.settle(df, hae.ha_signals(df), settle_offset=0)
        if sc.n:
            rates.append(sc.win_rate)
    assert len(rates) == 6
    assert abs(float(np.mean(rates)) - 0.5) < 0.02
