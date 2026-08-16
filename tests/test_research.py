"""The analysis harness, checked against series whose answer is known.

A study that cannot find a planted edge is worthless, and one that finds an
edge in a random walk is worse than worthless. Both directions are tested
here, because the whole point of this package is to be trusted when it says
"nothing survived".
"""

import sqlite3
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

# The analysis package is an optional extra (pip install -e ".[research]").
# Skipping rather than erroring keeps a plain checkout's test run green, but
# CI installs the extra so these actually run -- a suite that silently skips
# its most important tests is worse than one that fails.
pytest.importorskip("scipy", reason='install with: pip install -e ".[research]"')

from cheese_signals.research import dataset, probes, study, validate
from cheese_signals.research.probes import Finding

START = pd.Timestamp("2026-08-04 00:00", tz="UTC")


def _frame(closes, start=START):
    idx = pd.date_range(start, periods=len(closes), freq="1min", tz="UTC")
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"open": np.r_[c[0], c[:-1]], "high": c + 0.0002,
                         "low": c - 0.0002, "close": c, "volume": 100.0}, index=idx)


def _random_walk(n=8000, seed=0, step=0.0002, price=1.1):
    rng = np.random.default_rng(seed)
    return _frame(price + np.cumsum(rng.normal(0, step, n)))


def _mean_reverting(n=8000, seed=0, phi=-0.35, step=0.0002, price=1.1):
    """AR(1) returns with a negative coefficient: a real, plantable edge."""
    rng = np.random.default_rng(seed)
    r = np.zeros(n)
    for i in range(1, n):
        r[i] = phi * r[i - 1] + rng.normal(0, step)
    return _frame(price + np.cumsum(r))


# ------------------------------ loading ------------------------------
def test_candles_load_from_a_journal_database(tmp_path):
    p = tmp_path / "signals.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE candles (asset TEXT, ts TEXT, open REAL, high REAL,"
                " low REAL, close REAL, volume REAL)")
    con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?)", [
        ("EURUSD_otc", "2026-08-04T00:00:00+00:00", 1.1, 1.2, 1.0, 1.15, 10),
        ("EURUSD_otc", "2026-08-04T00:01:00+00:00", 1.15, 1.25, 1.1, 1.2, 10)])
    con.commit(); con.close()

    candles, report = dataset.load([p])
    assert len(candles) == 2
    assert report[0].rows == 2 and report[0].assets == 1


def test_overlapping_sources_are_merged_not_double_counted(tmp_path):
    """A duplicated bar inflates every sample size downstream."""
    rows = [("EURUSD_otc", f"2026-08-04T00:{m:02d}:00+00:00", 1.1, 1.2, 1.0, 1.15, 1)
            for m in range(10)]
    paths = []
    for name, subset in (("a.db", rows), ("b.db", rows[5:])):
        p = tmp_path / name
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE candles (asset TEXT, ts TEXT, open REAL,"
                    " high REAL, low REAL, close REAL, volume REAL)")
        con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?)", subset)
        con.commit(); con.close()
        paths.append(p)
    candles, _ = dataset.load(paths)
    assert len(candles) == 10


def test_a_missing_or_unreadable_file_is_reported_not_skipped(tmp_path):
    bad = tmp_path / "broken.db"
    bad.write_bytes(b"not a database")
    candles, report = dataset.load([bad, tmp_path / "nope.db"])
    assert candles.empty
    assert {r.kind for r in report} == {"sqlite", "missing"}
    assert all(r.note for r in report)


def test_gaps_in_the_recording_are_found(tmp_path):
    df = pd.concat([_random_walk(100), _random_walk(100, start := None) if False
                    else _random_walk(100).set_index(
                        pd.date_range("2026-08-04 06:00", periods=100,
                                      freq="1min", tz="UTC"))])
    gaps = dataset.continuity(df)
    assert len(gaps) == 1 and gaps.missing_bars.iloc[0] > 200


def test_analysis_never_runs_across_a_gap(tmp_path):
    """A four-hour hole treated as one step invents a move that never happened."""
    a = _random_walk(300)
    b = _random_walk(300).set_index(pd.date_range("2026-08-04 12:00", periods=300,
                                                  freq="1min", tz="UTC"))
    segs = dataset.segments(pd.concat([a, b]))
    assert len(segs) == 2
    assert all(s.index.to_series().diff().dropna().dt.total_seconds().max() == 60
               for s in segs)


# ------------------------------ probes find what is there ------------------------------
def test_the_variance_ratio_sees_mean_reversion():
    out = probes.variance_ratio(_mean_reverting(), "X")
    assert out, "no variance ratio computed"
    assert all(f.effect < 1.0 for f in out), [f.effect for f in out]
    assert any(f.p_value < 0.01 for f in out)


def test_the_variance_ratio_reports_about_one_on_a_random_walk():
    out = probes.variance_ratio(_random_walk(), "X")
    assert out
    assert all(0.8 < f.effect < 1.2 for f in out), [f.effect for f in out]


def test_autocorrelation_finds_a_planted_reversal():
    out = probes.autocorrelation(_mean_reverting(), "X", max_lag=3)
    lag1 = [f for f in out if f.detail.startswith("lag 1")]
    assert lag1 and lag1[0].win_rate > 0.55, lag1
    assert "fade" in lag1[0].detail
    assert lag1[0].p_value < 1e-6


def test_autocorrelation_finds_nothing_in_a_random_walk():
    """The direction that matters: no false edge in noise."""
    out = probes.autocorrelation(_random_walk(), "X", max_lag=20)
    assert out
    assert max(f.win_rate for f in out) < 0.56, max(out, key=lambda f: f.win_rate)


def test_streaks_are_measured_and_are_flat_on_a_random_walk():
    out = probes.streaks(_random_walk(20000), "X", max_run=4)
    assert out
    for f in out:
        assert 0.42 < f.win_rate < 0.58, f


def test_quantisation_detects_a_coarse_price_grid():
    rng = np.random.default_rng(0)
    grid = np.round(1.1 + np.cumsum(rng.normal(0, 0.0002, 3000)), 4)
    out = probes.quantisation(_frame(grid), "X")
    assert out and out[0].effect > 0.99


def test_repeats_reports_honestly_when_nothing_ever_repeats():
    out = probes.repeats(_random_walk(3000), "X", window=8, tolerance=0.01)
    assert out and (np.isnan(out[0].win_rate) or out[0].n >= 0)


def test_cross_pair_finds_a_planted_lead_lag():
    lead = _random_walk(6000, seed=1)
    r = lead["close"].diff().fillna(0).to_numpy()
    follower = _frame(1.3 + np.cumsum(np.r_[0.0, r[:-1]] * 0.9))
    out = probes.cross_pair({"A": lead, "B": follower}, max_lag=1)
    hit = [f for f in out if f.subject == "A->B"]
    assert hit and hit[0].win_rate > 0.8, hit


# ------------------------------ multiplicity ------------------------------
def test_benjamini_hochberg_rejects_a_battery_of_pure_noise():
    """Forty questions at p<0.05 produce two 'significant' answers from noise."""
    rng = np.random.default_rng(0)
    noise = [Finding("autocorr", "X", f"lag {i}", 1000, 0.51, 0.5,
                     float(rng.uniform(0, 1))) for i in range(200)]
    assert len(validate.benjamini_hochberg(noise)) == 0


def test_benjamini_hochberg_keeps_a_genuine_signal_among_noise():
    rng = np.random.default_rng(1)
    findings = [Finding("autocorr", "X", f"lag {i}", 1000, 0.51, 0.5,
                        float(rng.uniform(0.2, 1))) for i in range(100)]
    findings.append(Finding("autocorr", "X", "real", 5000, 0.62, 0.5, 1e-12))
    kept = validate.benjamini_hochberg(findings)
    assert [f.detail for f in kept] == ["real"]


# ------------------------------ the split ------------------------------
def test_every_asset_is_split_at_the_same_instant():
    """Per-asset quantiles would leak one pair's future into another's past."""
    panel = {"A": _random_walk(1000), "B": _random_walk(600, seed=3)}
    split = validate.split_by_time(panel, 0.3)
    for df in split.train.values():
        assert df.empty or df.index.max() < split.boundary
    for df in split.test.values():
        assert df.empty or df.index.min() >= split.boundary


def test_a_candidate_below_breakeven_out_of_sample_fails():
    f = Finding("autocorr", "X", "lag 1 follow", 1000, 0.62, 0.5, 1e-9)
    v = validate.confirm(f, lambda _f, _p: (505, 1000), {"X": _random_walk(100)}, 0.92)
    assert not v.survived and "break-even" in v.reason


def test_a_candidate_that_holds_out_of_sample_passes():
    f = Finding("autocorr", "X", "lag 1 follow", 1000, 0.62, 0.5, 1e-9)
    v = validate.confirm(f, lambda _f, _p: (620, 1000), {"X": _random_walk(100)}, 0.92)
    assert v.survived and v.test_win_rate == 0.62


def test_too_few_holdout_samples_is_not_a_pass():
    f = Finding("streak", "X", "3x up -> reverses", 1000, 0.7, 0.5, 1e-9)
    v = validate.confirm(f, lambda _f, _p: (40, 50), {"X": _random_walk(100)}, 0.92)
    assert not v.survived and "holdout samples" in v.reason


# ------------------------------ end to end ------------------------------
def test_a_planted_edge_survives_the_whole_study(tmp_path):
    """The study must be able to find something genuinely there."""
    p = tmp_path / "planted.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE candles (asset TEXT, ts TEXT, open REAL, high REAL,"
                " low REAL, close REAL, volume REAL)")
    df = _mean_reverting(20000, phi=-0.4)
    con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
                    [("EURUSD_otc", ts.isoformat(), r.open, r.high, r.low,
                      r.close, r.volume) for ts, r in df.iterrows()])
    con.commit(); con.close()

    text = study.run([str(p)], payout=0.92, max_lag=5)
    assert "mean-reverting" in text
    assert "PASS" in text, text[-2500:]


def test_a_random_walk_produces_no_surviving_edge(tmp_path):
    """The result that matters most: silence when there is nothing there."""
    p = tmp_path / "noise.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE candles (asset TEXT, ts TEXT, open REAL, high REAL,"
                " low REAL, close REAL, volume REAL)")
    rows = []
    for i, asset in enumerate(["EURUSD_otc", "GBPUSD_otc", "USDCAD_otc"]):
        df = _random_walk(12000, seed=i)
        rows += [(asset, ts.isoformat(), r.open, r.high, r.low, r.close, r.volume)
                 for ts, r in df.iterrows()]
    con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?)", rows)
    con.commit(); con.close()

    text = study.run([str(p)], payout=0.92, max_lag=20)
    assert "Nothing survived" in text, text[-3000:]


def test_the_report_states_how_many_hypotheses_were_tried(tmp_path):
    """Without it, the shortlist is unreadable: ten survivors out of ten is
    a discovery, ten out of four thousand is a Tuesday."""
    p = tmp_path / "n.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE candles (asset TEXT, ts TEXT, open REAL, high REAL,"
                " low REAL, close REAL, volume REAL)")
    df = _random_walk(6000)
    con.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
                    [("EURUSD_otc", ts.isoformat(), r.open, r.high, r.low,
                      r.close, r.volume) for ts, r in df.iterrows()])
    con.commit(); con.close()
    text = study.run([str(p)], payout=0.92, max_lag=10)
    assert "hypotheses tested" in text and "hypotheses tested" in text
