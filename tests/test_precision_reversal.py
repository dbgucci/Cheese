"""The precision-reversal port, pinned against the behaviour it actually has.

These tests exist to stop the evaluation being re-litigated from memory. The
strategy's structure -- not its win rate -- is what most of them assert:
that the confluence count cannot fall below three, that the confidence is a
relabelled volume flag, and that the wick test is order-dependent. Those are
properties of the code, so they are checkable exactly, and they are the
reason the win rate comes out where it does.

The lookahead test is the one that would invalidate everything else if it
failed, so it is written to fail loudly: the future is overwritten with
garbage and every signal must be identical.
"""

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("scipy", reason='install with: pip install -e ".[research]"')

from cheese_signals.research import precision_reversal as pr

START = pd.Timestamp("2026-08-04 00:00", tz="UTC")


def _frame(rows):
    """rows: list of (open, high, low, close, volume)."""
    arr = np.asarray(rows, dtype=float)
    idx = pd.date_range(START, periods=len(arr), freq="1min", tz="UTC")
    return pd.DataFrame(
        {"open": arr[:, 0], "high": arr[:, 1], "low": arr[:, 2],
         "close": arr[:, 3], "volume": arr[:, 4]},
        index=idx,
    )


# ---------------------------------------------------------------------------
# the EMA the original actually used
# ---------------------------------------------------------------------------
def test_talib_ema_warms_up_like_talib():
    vals = np.arange(1, 301, dtype=float)
    out = pr.talib_ema(vals, 200)
    assert np.all(np.isnan(out[:199])), "TA-Lib emits period-1 NaNs"
    assert out[199] == pytest.approx(vals[:200].mean()), "seeded with an SMA"
    k = 2.0 / 201.0
    assert out[200] == pytest.approx((vals[200] - out[199]) * k + out[199])


def test_talib_ema_is_not_pandas_ewm():
    """The difference decides which bars are tradeable at all, so it matters."""
    from cheese_signals import indicators as ind

    vals = np.arange(1, 301, dtype=float)
    pandas_ema = ind.ema(pd.Series(vals), 200)
    assert not np.isnan(pandas_ema.iloc[0]), "ewm is defined from bar 0"
    assert np.isnan(pr.talib_ema(vals, 200)[0]), "TA-Lib is not"


# ---------------------------------------------------------------------------
# the structure of the confluence count
# ---------------------------------------------------------------------------
def _uptrend_with_hammer(n=260, volume_spike=False):
    """A rising series ending in a hammer at a new low, above its EMA200."""
    rows = []
    price = 1.0
    for _ in range(n - 1):
        price *= 1.0004
        rows.append((price, price * 1.0001, price * 0.9999, price, 100.0))
    # Hammer: tiny body, long lower wick, low under every recent close.
    low = price * 0.990
    rows.append((price, price * 1.0001, low, price * 1.00005,
                 400.0 if volume_spike else 100.0))
    return _frame(rows)


def test_confluence_count_can_never_be_below_three():
    """Three of the four confluences are mandatory gates, so `>= 3` is dead code."""
    df = _uptrend_with_hammer()
    ema = pr.talib_ema(df["close"].to_numpy(), 200)
    setup = pr.check_signal(df, len(df) - 1, ema)
    assert setup is not None
    assert len(setup.reason.split(" + ")) == 3
    assert setup.confidence == 70


def test_confidence_is_only_ever_70_or_80():
    """So "confidence" carries exactly one bit: did volume spike."""
    for spike, expected in ((False, 70), (True, 80)):
        df = _uptrend_with_hammer(volume_spike=spike)
        ema = pr.talib_ema(df["close"].to_numpy(), 200)
        setup = pr.check_signal(df, len(df) - 1, ema)
        assert setup is not None
        assert setup.confidence == expected


def test_no_signal_ever_reports_a_confidence_outside_that_pair():
    df = pr.driftless_walk(20000, seed=3)
    result = pr.replay(df)
    assert result.n > 0
    assert {o.confidence for o in result.outcomes} <= {70, 80}


# ---------------------------------------------------------------------------
# the wick test is order-dependent
# ---------------------------------------------------------------------------
def test_dominant_upper_wick_is_still_called_a_buy():
    """`if lower_wick > body*1.5` is tested first, so it wins ties it should lose."""
    # body 1.0, lower wick 2.0, upper wick 8.0 -- clearly a bearish rejection.
    o, c = 100.0, 101.0
    row = (o, c + 8.0, o - 2.0, c, 100.0)
    body = abs(c - o)
    assert (o - (o - 2.0)) > body * 1.5 and ((c + 8.0) - c) > body * 1.5

    df = _uptrend_with_hammer()
    rows = df.to_numpy().tolist()
    rows[-1] = list(row)
    df2 = _frame(rows)
    ema = pr.talib_ema(df2["close"].to_numpy(), 200)

    # The original reads it as a BUY purely because of statement order.
    loose = pr.check_signal(df2, len(df2) - 1, ema, strict=False)
    strict = pr.check_signal(df2, len(df2) - 1, ema, strict=True)
    assert loose is None or loose.direction == pr.BUY
    assert strict is None or strict.direction == pr.SELL


def test_doji_makes_every_wick_bullish():
    """body == 0 => `wick > 0` passes, and the lower wick is tested first."""
    df = _uptrend_with_hammer()
    rows = df.to_numpy().tolist()
    price = rows[-2][3]
    # A perfect doji whose upper wick dwarfs the lower one.
    rows[-1] = [price, price * 1.02, price * 0.999, price, 100.0]
    df2 = _frame(rows)
    ema = pr.talib_ema(df2["close"].to_numpy(), 200)
    setup = pr.check_signal(df2, len(df2) - 1, ema, strict=False)
    if setup is not None:
        assert setup.direction == pr.BUY, "a doji is always read as bullish"


# ---------------------------------------------------------------------------
# the thing that would invalidate the whole evaluation
# ---------------------------------------------------------------------------
def test_no_lookahead():
    """Every signal must be identical when the future is replaced with garbage."""
    df = pr.driftless_walk(4000, seed=17)
    cut = 3000

    base = pr.replay(df.iloc[:cut])

    poisoned = df.copy()
    tail = poisoned.iloc[cut:].index
    poisoned.loc[tail, ["open", "high", "low", "close"]] = 999.0
    poisoned.loc[tail, "volume"] = 1e9
    after = pr.replay(poisoned.iloc[:cut])

    assert base.n == after.n and base.n > 0
    assert [(o.index, o.direction, o.confidence) for o in base.outcomes] == \
           [(o.index, o.direction, o.confidence) for o in after.outcomes]


def test_flat_close_is_a_refund_not_a_loss():
    """Pocket Option returns the stake on a tie, so ties stay out of the denominator."""
    df = pr.driftless_walk(3000, seed=5)
    flat = df.copy()
    # Freeze every close so each settlement is an exact tie.
    flat["close"] = flat["close"].iloc[0]
    result = pr.replay(flat)
    assert result.n == 0
    assert np.isnan(result.win_rate)


# ---------------------------------------------------------------------------
# the harness must not find an edge that is not there
# ---------------------------------------------------------------------------
def test_scores_a_coin_flip_on_a_driftless_walk():
    """Averaged over independent nulls, the rules extract nothing. As they must."""
    rates = []
    for seed in range(8):
        r = pr.replay(pr.driftless_walk(30000, seed=200 + seed))
        if r.n:
            rates.append(r.win_rate)
    assert len(rates) == 8
    assert abs(float(np.mean(rates)) - 0.5) < 0.015


def test_required_trades_refuses_to_size_a_non_edge():
    assert pr.required_trades(0.51, 0.5208) is None
    need = pr.required_trades(0.56, 0.5208)
    assert need is not None and 800 < need < 1600
