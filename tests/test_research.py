"""Tests for the research harness.

The two that matter most are ``test_random_walk_yields_no_findings`` and
``test_planted_edge_is_recovered``. Together they establish that the miner
has both properties a research tool needs and that most backtesters lack:
it does not invent edges in noise, and it does find one that is really
there. A tool with only the second property is a random number generator
with a progress bar.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cheese_signals.research import dataset, features, mine, randomwalk
from cheese_signals.research.stats import (
    Payout,
    benjamini_hochberg,
    bh_qvalues,
    binom_sf,
    binom_test_greater,
    required_trades,
    wilson_interval,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_candles(n=40_000, seed=0, edge_hour=None, edge_strength=0.0):
    """1-minute random-walk candles, optionally with a planted hourly edge."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-01", periods=n, freq="1min", tz="UTC")

    steps = rng.normal(0, 1e-4, n)
    if edge_hour is not None:
        biased = (idx.hour == edge_hour) & (rng.random(n) < edge_strength)
        steps = np.where(biased, np.abs(steps), steps)

    close = 1.10 + np.cumsum(steps)
    open_ = np.concatenate(([close[0]], close[:-1]))
    high = np.maximum.reduce([close + np.abs(rng.normal(0, 3e-5, n)), open_, close])
    low = np.minimum.reduce([close - np.abs(rng.normal(0, 3e-5, n)), open_, close])
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": 0.0},
        index=idx,
    )


# ---------------------------------------------------------------------------
# Option economics
# ---------------------------------------------------------------------------


def test_breakeven_is_above_a_coin_flip():
    """The whole point: 50% is not the bar, 52.08% is."""
    assert Payout(0.92).breakeven == pytest.approx(0.520833, abs=1e-5)
    assert Payout(0.80).breakeven == pytest.approx(0.555556, abs=1e-5)
    assert Payout(1.00).breakeven == pytest.approx(0.5)


def test_expectancy_is_negative_at_a_coin_flip():
    """500 wins / 500 losses at a 92% payout loses money."""
    assert Payout(0.92).expectancy(500, 500) == pytest.approx(-0.04, abs=1e-9)


def test_refunds_dilute_but_do_not_lose():
    payout = Payout(0.92)
    assert payout.expectancy(0, 0, refunds=10) == 0.0
    # A refund occupies a slot, so it pulls per-trade expectancy toward zero.
    assert payout.expectancy(60, 40, refunds=100) < payout.expectancy(60, 40)


def test_edge_over_breakeven_ignores_refunds():
    payout = Payout(0.92)
    assert payout.edge_over_breakeven(60, 40) == pytest.approx(0.6 - payout.breakeven)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def test_binom_sf_matches_known_values():
    # P(X >= 1) for n=1,p=0.5 is 0.5; P(X >= 2) for n=2,p=0.5 is 0.25.
    assert binom_sf(1, 1, 0.5) == pytest.approx(0.5, abs=1e-9)
    assert binom_sf(2, 2, 0.5) == pytest.approx(0.25, abs=1e-9)
    assert binom_sf(5, 10, 0.5) == pytest.approx(0.623046875, abs=1e-9)
    assert binom_sf(0, 10, 0.5) == 1.0
    assert binom_sf(11, 10, 0.5) == 0.0


def test_a_hot_streak_on_small_n_is_not_significant():
    """58% over 40 trades feels like an edge and is not one."""
    p = binom_test_greater(23, 40, Payout(0.92).breakeven)
    assert p > 0.20


def test_the_same_rate_on_large_n_is_significant():
    p = binom_test_greater(2300, 4000, Payout(0.92).breakeven)
    assert p < 1e-10


def test_wilson_interval_brackets_the_estimate_and_stays_in_range():
    lo, hi = wilson_interval(58, 100)
    assert 0.0 <= lo < 0.58 < hi <= 1.0
    # Degenerate counts must not escape [0, 1] the way a normal interval does.
    lo, hi = wilson_interval(0, 5)
    assert lo >= 0.0 and hi <= 1.0
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_interval_narrows_with_more_data():
    lo_small, hi_small = wilson_interval(58, 100)
    lo_big, hi_big = wilson_interval(5800, 10_000)
    assert (hi_big - lo_big) < (hi_small - lo_small)


def test_benjamini_hochberg_rejects_nothing_on_uniform_pvalues():
    """Uniform p-values are what pure noise produces; none should survive."""
    rng = np.random.default_rng(7)
    pvals = list(rng.random(1000))
    assert sum(benjamini_hochberg(pvals, alpha=0.05)) == 0


def test_benjamini_hochberg_keeps_genuine_signal():
    pvals = [1e-9] * 10 + list(np.linspace(0.05, 1.0, 990))
    keep = benjamini_hochberg(pvals, alpha=0.05)
    assert sum(keep[:10]) == 10


def test_bh_qvalues_are_monotone_in_pvalue():
    pvals = [0.001, 0.01, 0.04, 0.2, 0.9]
    q = bh_qvalues(pvals)
    assert all(q[i] <= q[i + 1] + 1e-12 for i in range(len(q) - 1))
    assert all(0.0 <= v <= 1.0 for v in q)


def test_required_trades_is_large_for_a_realistic_edge():
    """The number that ends most 'my bot wins 60%' conversations."""
    n = required_trades(0.55, Payout(0.92).breakeven)
    assert n > 1000


def test_required_trades_rejects_a_losing_rate():
    assert required_trades(0.51, Payout(0.92).breakeven) == -1


# ---------------------------------------------------------------------------
# Features: the no-lookahead guarantee
# ---------------------------------------------------------------------------


def test_features_never_look_ahead():
    """Features at bar i must not change when future bars are removed.

    This is the property that separates a backtest from a fantasy. If it
    ever fails, every result in the report is worthless, so it is asserted
    directly rather than trusted.
    """
    df = make_candles(n=3000, seed=3)
    full = features.compute(df)

    for cut in (1500, 2000, 2500):
        truncated = features.compute(df.iloc[:cut])
        last = truncated.index[-1]
        for column in full.columns:
            a, b = full.loc[last, column], truncated.loc[last, column]
            if pd.isna(a) and pd.isna(b):
                continue
            assert a == pytest.approx(b, rel=1e-9), f"{column} leaked the future"


def test_label_is_the_next_bar_move():
    df = make_candles(n=500, seed=4)
    y = features.label(df)
    delta = df["close"].shift(-1) - df["close"]
    assert (y.dropna() == np.sign(delta.dropna())).all()
    assert pd.isna(y.iloc[-1])  # the final bar has no outcome


def test_label_treats_a_flat_close_as_zero():
    idx = pd.date_range("2025-01-01", periods=3, freq="1min", tz="UTC")
    df = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": [1.0, 1.0, 1.0]}, index=idx
    )
    assert features.label(df).iloc[0] == 0.0


def test_streak_counts_consecutive_moves_with_sign():
    idx = pd.date_range("2025-01-01", periods=6, freq="1min", tz="UTC")
    close = pd.Series([1.0, 1.1, 1.2, 1.3, 1.2, 1.1], index=idx)
    streak = features._signed_streak(np.sign(close.diff()))
    assert list(streak) == [0.0, 1.0, 2.0, 3.0, -1.0, -2.0]


def test_bins_are_fit_on_training_data_only():
    """Edges from the train split must be reusable verbatim on later data."""
    df = make_candles(n=6000, seed=5)
    raw = features.compute(df)
    specs = features.fit_bins(raw.iloc[:3000])
    binned = features.apply_bins(raw, specs)

    assert specs, "expected at least one continuous feature to be binned"
    # Outer edges are open, so out-of-sample extremes still land somewhere
    # rather than becoming NaN and silently shrinking the test split.
    for spec in specs.values():
        assert spec.edges[0] == -np.inf and spec.edges[-1] == np.inf
    assert len(binned) == len(raw)


# ---------------------------------------------------------------------------
# The two tests that justify the whole package
# ---------------------------------------------------------------------------


def test_random_walk_yields_no_findings():
    """Specificity: pure noise must produce zero confirmed rules."""
    df = make_candles(n=40_000, seed=11)
    report = mine.mine_asset(df, asset="RANDOM", payout=Payout(0.92))

    assert report.n_candidates_tested > 500, "search was too small to be a real test"
    assert report.confirmed == []
    assert "No edge found" in report.verdict


def test_confirmation_requires_the_interval_to_clear_breakeven():
    """"Confirmed" must mean what the report claims it means.

    A rule whose test-split point estimate beats break-even on a handful of
    trades is not confirmed by any reasonable standard, so the Wilson lower
    bound has to clear the line too.
    """
    df = make_candles(n=40_000, seed=12, edge_hour=14, edge_strength=0.30)
    report = mine.mine_asset(df, asset="PLANTED", payout=Payout(0.92))

    breakeven = Payout(0.92).breakeven
    for candidate in report.confirmed:
        lower, _ = candidate.test.interval()
        assert lower > breakeven, f"{candidate.name} confirmed on a CI touching break-even"


def test_planted_edge_is_recovered():
    """Power: a real edge must be found, and attributed to the right feature."""
    df = make_candles(n=40_000, seed=12, edge_hour=14, edge_strength=0.30)
    report = mine.mine_asset(df, asset="PLANTED", payout=Payout(0.92))

    assert report.confirmed, "miner failed to find a planted edge"
    assert all("hour" in c.feature for c in report.confirmed), (
        "miner found the edge but attributed it to the wrong feature"
    )
    best = report.confirmed[0]
    assert best.test.win_rate > Payout(0.92).breakeven
    assert best.direction == 1  # the plant biases moves upward


# ---------------------------------------------------------------------------
# Predictability battery
# ---------------------------------------------------------------------------


def test_random_walk_shows_no_systematic_structure():
    df = make_candles(n=40_000, seed=13)
    report = randomwalk.analyze(df["close"], asset="RANDOM")
    flagged = [t for t in report.tests if t.significant]
    # A handful of tests at p<0.05 will flag by chance; the point is that it
    # is a trickle, not a flood.
    assert len(flagged) <= 3
    assert report.n_bars == len(df)


def test_trending_series_trips_the_variance_ratio():
    """A strongly autocorrelated series must be detected as non-random."""
    rng = np.random.default_rng(14)
    n = 20_000
    noise = rng.normal(0, 1e-4, n)
    ret = np.zeros(n)
    for i in range(1, n):
        ret[i] = 0.4 * ret[i - 1] + noise[i]  # AR(1): real, linear memory
    close = pd.Series(1.10 + np.cumsum(ret))

    report = randomwalk.analyze(close, asset="AR1")
    assert report.any_structure
    assert any(t.name.startswith("autocorr") and t.significant for t in report.tests)


@pytest.mark.parametrize("phi", [0.3, -0.3])
def test_variance_ratio_matches_closed_form_for_ar1(phi):
    """For an AR(1) process, VR(2) = 1 + phi. Checked against theory.

    Guards a real bug this code shipped with once: an extra factor of n in
    the robust variance shrank every z-statistic by sqrt(n), so the test
    silently never rejected and would have reported a trending feed as a
    random walk.
    """
    rng = np.random.default_rng(15)
    n = 40_000
    noise = rng.normal(0, 1e-4, n)
    ret = np.zeros(n)
    for i in range(1, n):
        ret[i] = phi * ret[i - 1] + noise[i]

    result = randomwalk.variance_ratio(ret, q=2)
    assert result.statistic == pytest.approx(1.0 + phi, abs=0.03)
    assert result.pvalue < 1e-6


def test_variance_ratio_does_not_reject_a_random_walk():
    rng = np.random.default_rng(16)
    returns = rng.normal(0, 1e-4, 40_000)
    for q in (2, 4, 8, 16):
        assert randomwalk.variance_ratio(returns, q=q).pvalue > 0.01


def test_sign_persistence_ignores_flat_candles():
    close = pd.Series([1.0, 1.0, 1.0, 1.0] * 30)
    assert randomwalk.sign_persistence(close) is None


# ---------------------------------------------------------------------------
# Dataset handling
# ---------------------------------------------------------------------------


def test_recording_gaps_are_split_apart():
    """Adjacent rows across a gap are not adjacent in time and must not pair."""
    first = pd.date_range("2025-01-01 00:00", periods=100, freq="1min", tz="UTC")
    second = pd.date_range("2025-01-01 09:00", periods=100, freq="1min", tz="UTC")
    idx = first.append(second)
    df = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0.0}, index=idx
    )

    chunks = dataset.split_sessions(df)
    assert len(chunks) == 2
    assert len(chunks[0]) == 100 and len(chunks[1]) == 100


def test_corrupt_candles_are_dropped():
    idx = pd.date_range("2025-01-01", periods=3, freq="1min", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [1.0, 1.0, 1.0],
            "high": [1.1, 0.5, 1.1],  # middle row: high below low
            "low": [0.9, 1.5, 0.9],
            "close": [1.0, 1.0, 1.0],
            "volume": [0.0, 0.0, 0.0],
        },
        index=idx,
    )
    assert len(dataset._normalize(df)) == 2


def test_missing_journal_yields_nothing_rather_than_raising(tmp_path):
    assert list(dataset.from_journal(tmp_path / "nope.db")) == []
    assert dataset.collect(db_path=tmp_path / "nope.db", csv_dir=tmp_path) == []
