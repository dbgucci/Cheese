"""The survey that runs before any strategy is written.

Driven against a fake broker here; the same code path runs against the live
MetaTrader terminal, which is the only place the real spreads exist.
"""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from cheese_signals.markets import survey
from cheese_signals.markets.mt5_bridge import ReplayFeed, SymbolSpec


def _bars(n, step, spread, seed=0, start="2026-01-05 00:00", price=10_000.0):
    rng = np.random.default_rng(seed)
    close = price + np.cumsum(rng.normal(0, step, n))
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {"open": close, "high": close + step, "low": close - step,
         "close": close, "spread": float(spread), "tick_volume": 100.0},
        index=idx,
    )


def _spec(name, point=1.0):
    return SymbolSpec(name=name, point=point, digits=2, contract_size=1.0,
                      tick_value=1.0, volume_min=0.01, volume_step=0.01,
                      volume_max=100.0, spread_current=1.0)


NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)


def _feed(**overrides):
    n = 60 * 24 * 20
    frames = {
        "NAS100": _bars(n, 3.0, 2.0, seed=1, start="2026-02-09"),
        "XAUUSD": _bars(n, 0.30, 25.0, seed=2, start="2026-02-09", price=2400.0),
        "EURGBP": _bars(n, 0.00004, 1.6, seed=3, start="2026-02-09", price=0.85),
    }
    frames.update(overrides)
    specs = {"NAS100": _spec("NAS100", 1.0), "XAUUSD": _spec("XAUUSD", 0.01),
             "EURGBP": _spec("EURGBP", 0.00001)}
    return ReplayFeed(frames, specs)


# ------------------------------ symbol resolution ------------------------------
def test_broker_specific_suffixes_are_matched():
    """CFD naming is not standardised; exact-match-only looks like an outage."""
    feed = ReplayFeed({"XAUUSD.r": _bars(10, 1, 1), "US30cash": _bars(10, 1, 1)})
    found, missing = survey.resolve(feed, ["XAUUSD", "US30"])
    assert found == {"XAUUSD": "XAUUSD.r", "US30": "US30cash"}
    assert missing == []


def test_the_shortest_match_wins_over_a_longer_one():
    feed = ReplayFeed({"NAS100": _bars(10, 1, 1), "NAS100_mini": _bars(10, 1, 1)})
    found, _ = survey.resolve(feed, ["NAS100"])
    assert found["NAS100"] == "NAS100"


def test_an_instrument_the_broker_does_not_offer_is_named():
    feed = ReplayFeed({"NAS100": _bars(10, 1, 1)})
    found, missing = survey.resolve(feed, ["NAS100", "SPX500"])
    assert missing == ["SPX500"] and "SPX500" not in found


# ------------------------------ the survey itself ------------------------------
def test_the_survey_produces_a_row_per_instrument_and_horizon():
    r = survey.run(_feed(), symbols=["NAS100", "XAUUSD", "EURGBP"],
                   days=20, horizons=(1, 15, 60), now=NOW)
    assert len(r["table"]) == 9
    assert set(r["table"].symbol) == {"NAS100", "XAUUSD", "EURGBP"}


def test_scalping_horizons_fail_where_longer_holds_pass():
    """The finding the whole module exists to surface, on a wide-spread pair."""
    r = survey.run(_feed(), symbols=["XAUUSD"], days=20,
                   horizons=(1, 5, 60, 240), now=NOW)
    t = r["table"].set_index("horizon_min")
    assert t.loc[1, "verdict"] == "unviable"
    assert t.loc[240, "ratio"] > t.loc[1, "ratio"]


def test_the_break_even_win_rate_is_reported_for_comparison():
    r = survey.run(_feed(), symbols=["NAS100"], days=20, horizons=(240,), now=NOW)
    be = r["table"].breakeven_wr.iloc[0]
    assert 0.0 < be < 1.0


def test_a_horizon_whose_move_equals_the_cost_needs_a_hundred_percent():
    """Not a bug in the formula -- the honest answer.

    This fake NAS100 moves a median 2 points per minute against a 2-point
    round trip, so a winning trade nets exactly zero and a losing one nets
    -4. No win rate makes that profitable. It is the same wall Mesfin (2026)
    measured on real Nasdaq micro futures, where fourteen signal families
    produced 0.07-1.50 points of gross edge against a 2-point friction cost
    and every one of them failed.
    """
    r = survey.run(_feed(), symbols=["NAS100"], days=20, horizons=(1,), now=NOW)
    row = r["table"].iloc[0]
    assert row.move_pts <= row.cost_pts
    assert row.breakeven_wr == 1.0
    assert row.verdict == "unviable"


def test_missing_instruments_reach_the_report_rather_than_vanishing():
    r = survey.run(_feed(), symbols=["NAS100", "US30"], days=20,
                   horizons=(15,), now=NOW)
    assert any("US30" in p for p in r["problems"])
    assert "US30" not in set(r["table"].symbol)


def test_index_measurements_use_cash_session_hours_only():
    """Spreads outside the US session describe a market nobody is trading."""
    n = 60 * 24 * 20
    wide = _bars(n, 3.0, 2.0, seed=1, start="2026-02-09")
    wide.loc[(wide.index.hour < 13) | (wide.index.hour >= 20), "spread"] = 60.0
    r = survey.run(_feed(NAS100=wide), symbols=["NAS100"], days=20,
                   horizons=(15,), now=NOW)
    assert r["table"].spread_p90.iloc[0] == 2.0, "the overnight spread leaked in"


def test_the_point_size_from_the_broker_is_applied():
    """A 25-point gold spread is 25 points, not 0.25 -- and sizing depends on it."""
    r = survey.run(_feed(), symbols=["XAUUSD"], days=20, horizons=(60,), now=NOW)
    assert r["table"].cost_pts.iloc[0] == pytest.approx(25.0)


# ------------------------------ the written report ------------------------------
def test_the_report_states_how_to_read_it_and_names_the_old_benchmark():
    text = survey.format_report(
        survey.run(_feed(), symbols=["NAS100"], days=20, horizons=(1, 60), now=NOW))
    assert "COST WALL SURVEY" in text
    assert "54.05%" in text, "the report should be comparable to the last project"
    assert "VERDICT" in text


def test_the_report_lists_the_most_expensive_hours():
    n = 60 * 24 * 20
    wide = _bars(n, 3.0, 2.0, seed=1, start="2026-02-09")
    wide.loc[wide.index.hour == 22, "spread"] = 90.0
    text = survey.format_report(
        survey.run(_feed(NAS100=wide), symbols=["NAS100"], days=20,
                   horizons=(60,), now=NOW))
    assert "MOST EXPENSIVE HOURS" in text
    assert "22:00 = 90.0" in text


def test_the_report_names_what_it_could_not_measure():
    text = survey.format_report(
        survey.run(_feed(), symbols=["NAS100", "SPX500"], days=20,
                   horizons=(60,), now=NOW))
    assert "COULD NOT MEASURE" in text and "SPX500" in text


def test_a_broker_offering_nothing_produces_a_report_not_a_crash():
    r = survey.run(ReplayFeed({}), symbols=["NAS100"], days=20,
                   horizons=(15,), now=NOW)
    text = survey.format_report(r)
    assert "No instrument returned usable data." in text
    assert r["problems"]
