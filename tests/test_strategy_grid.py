"""The grid search, checked against series whose answer is known in advance.

A search that cannot find a planted edge is useless. A search that finds one
in a random walk is dangerous. Both are tested, because this module's only
value is that its "nothing survived" can be believed.
"""

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("scipy", reason='install with: pip install -e ".[research]"')

from cheese_signals.research import strategy_grid as sg

START = pd.Timestamp("2026-08-04 00:00", tz="UTC")


def _panel(closes, asset="TEST_otc", start=START):
    c = np.asarray(closes, dtype=float)
    ts = pd.date_range(start, periods=len(c), freq="1min", tz="UTC")
    o = np.concatenate([[c[0]], c[:-1]])
    return {asset: [pd.DataFrame({
        "asset": asset, "ts": ts, "open": o,
        "high": np.maximum(o, c) + 1e-4, "low": np.minimum(o, c) - 1e-4,
        "close": c, "volume": 0.0})]}


def _random_walk(n=6000, seed=0, step=2e-4, price=1.1):
    rng = np.random.default_rng(seed)
    return price + np.cumsum(rng.normal(0, step, n))


def _mean_reverting(n=6000, seed=0, phi=-0.45, step=2e-4, price=1.1):
    """AR(1) returns with a negative coefficient: a real, findable edge."""
    rng = np.random.default_rng(seed)
    r = np.zeros(n)
    for i in range(1, n):
        r[i] = phi * r[i - 1] + rng.normal(0, step)
    return price + np.cumsum(r)


# ---------------------------------------------------------------- mechanics
def test_signals_are_causal():
    """A signal at t must not move when a later bar changes."""
    c = _random_walk(500, seed=3)
    a = sg.build_signals(_panel(c)["TEST_otc"][0])
    c2 = c.copy()
    c2[400:] += 0.01
    b = sg.build_signals(_panel(c2)["TEST_otc"][0])
    for name in a:
        assert np.array_equal(a[name][:399], b[name][:399]), f"{name} looks ahead"


def test_signal_values_are_trinary():
    a = sg.build_signals(_panel(_random_walk(400, seed=1))["TEST_otc"][0])
    for name, arr in a.items():
        assert set(np.unique(arr)) <= {-1, 0, 1}, f"{name} emitted {set(np.unique(arr))}"


def test_segments_never_span_a_gap():
    ts = list(pd.date_range(START, periods=300, freq="1min", tz="UTC"))
    ts += list(pd.date_range(START + pd.Timedelta(days=3), periods=300, freq="1min", tz="UTC"))
    frame = pd.DataFrame({"asset": "X_otc", "ts": ts, "open": 1.1, "high": 1.2,
                          "low": 1.0, "close": 1.1, "volume": 0.0})
    runs = sg.segments(frame)["X_otc"]
    assert len(runs) == 2 and all(len(r) == 300 for r in runs)


def test_refunds_are_not_losses():
    flat = _panel(np.full(400, 1.1))
    t = sg.backtest(flat, expiries=(1,))
    assert len(t) > 0
    assert (t.res == 0).all(), "an unchanged price must refund, not lose"


def test_entry_uses_the_next_open_not_the_signal_close():
    """Entry is open[t+1]; a jump between close[t] and open[t+1] must be borne."""
    n = 300
    c = np.full(n, 1.1)
    c[200:] = 1.2                    # price gaps up starting at bar 200
    panel = _panel(c)
    bars = panel["TEST_otc"][0]
    # bar 199 closes at 1.1, bar 200 opens at 1.1 and closes at 1.2
    assert bars.open.iloc[200] == pytest.approx(1.1)
    t = sg.backtest(panel, expiries=(1,), warmup=60)
    call = t[(t.strategy == "always_call")]
    assert not call.empty


# ---------------------------------------------------------------- the answer
def test_finds_a_planted_edge():
    """Strong mean reversion must show up, or the search proves nothing."""
    trades = sg.backtest(_panel(_mean_reverting(seed=11)), expiries=(1,))
    by = (trades.groupby("strategy").res
          .agg(n="size", w=lambda s: int((s > 0).sum()), l=lambda s: int((s < 0).sum())))
    by = by[(by.w + by.l) >= 300]
    best = (by.w / (by.w + by.l)).max()
    assert best > 0.60, f"planted AR(1) edge not found (best {best:.3f})"


def _multi_walk_panel(assets=6, n=9000, seed=5):
    """Several independent walks, as the real feed has six independent pairs.

    One walk is not enough to test this claim: a single realised path has a
    net displacement that is the same in both halves, so ``always_call`` can
    survive a holdout on drift alone. Averaging over independent paths is what
    removes that, and is why the real study pools six pairs.
    """
    panel = {}
    for i in range(assets):
        name = f"P{i}_otc"
        panel.update(_panel(_random_walk(n, seed=seed + 100 * i), asset=name))
    return panel


def test_a_random_walk_yields_nothing_out_of_sample():
    """In-sample rank must carry no information about out-of-sample rank.

    The assertion is on the *correlation across every cell*, not on the mean
    of the top 20. That distinction is the lesson this test encodes: the top
    cells of a grid search cluster into a handful of pair/hour slots (4-7 of
    them, measured), so their out-of-sample mean is two or three correlated
    bets and swings from 0.38 to 0.58 with the seed. The full-grid correlation
    is stable at zero across every seed tried, and is the statistic any claim
    about "the backtest held up" should be made on.
    """
    trades = sg.backtest(_multi_walk_panel())
    g = sg.grid(trades, min_train=100, min_test=40)
    assert len(g) > 200
    assert g.head(20).train_win.mean() > 0.55, "search should overfit in-sample"
    assert abs(g.train_win.corr(g.test_win)) < 0.10, (
        f"in-sample rank predicted out-of-sample rank on noise "
        f"({g.train_win.corr(g.test_win):+.3f})")


def test_surviving_both_halves_happens_at_the_chance_rate():
    """Cells clearing 55% in both halves must be no more numerous than chance."""
    trades = sg.backtest(_multi_walk_panel(seed=31))
    g = sg.grid(trades, min_train=100, min_test=40)
    both = int(((g.train_win > 0.55) & (g.test_win > 0.55)).sum())
    chance = (g.train_win > 0.55).mean() * (g.test_win > 0.55).mean() * len(g)
    assert both < 1.5 * chance, f"{both} survived, chance predicts {chance:.0f}"


def test_walk_forward_on_a_random_walk_is_a_coin():
    trades = sg.backtest(_panel(_random_walk(12000, seed=8)))
    wf = sg.walk_forward(trades, folds=8, min_trades=80, top_k=5)
    assert wf.n > 100
    assert abs(wf.win_rate - 0.5) < 0.08, f"got {wf.win_rate:.3f}"
    assert wf.ev(0.92) < 0, "a coin cannot have positive EV at a 92% payout"


def test_walk_forward_selection_uses_only_past_folds():
    """The last fold must never inform an earlier fold's pick."""
    trades = sg.backtest(_panel(_random_walk(9000, seed=2)))
    a = sg.walk_forward(trades, folds=6, min_trades=60, top_k=3)
    tail = pd.to_datetime(trades.ts, utc=True).quantile(0.85)
    trimmed = trades[pd.to_datetime(trades.ts, utc=True) <= tail]
    b = sg.walk_forward(trimmed, folds=6, min_trades=60, top_k=3)
    assert a.n != b.n or a.wins != b.wins  # different data, different forward set


def test_synthetic_null_is_centred_on_a_coin():
    """The null pipeline must not itself manufacture an edge."""
    panel = _panel(_random_walk(4000, seed=4))
    scores = sg.synthetic_null(
        panel, reps=3, score=lambda t: float((t.res > 0).mean()))
    assert np.all(np.abs(scores - 0.5) < 0.05), scores


def test_breakeven_matches_the_payout_arithmetic():
    assert sg.breakeven(1.00) == pytest.approx(0.50)
    assert sg.breakeven(0.92) == pytest.approx(0.5208, abs=1e-4)
    assert sg.breakeven(0.85) == pytest.approx(0.5405, abs=1e-4)
