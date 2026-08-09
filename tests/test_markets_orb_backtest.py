"""The simulator, including the one property that can detect a lookahead bug.

A lookahead bug does not raise, does not warn, and looks exactly like an edge.
The only thing that catches it is running the strategy on data that provably
contains no edge and checking that it finds none:
``test_a_driftless_random_walk_yields_about_nothing``.
"""

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from cheese_signals.markets import orb_backtest as bt
from cheese_signals.markets.clock import SESSIONS
from cheese_signals.markets.execution import BUY, SELL
from cheese_signals.markets.orb import OrbConfig

US = SESSIONS["us_cash"]
POINT = 0.1
DAY = date(2026, 3, 2)                      # Monday, US winter: 14:30-21:00 UTC
OPEN = datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc)


def session(rows, day=DAY, spread=10.0, spec=US):
    """``rows`` are (high, low, close) triples from the session open onward."""
    index = pd.date_range(spec.open_utc(day), periods=len(rows), freq="1min", tz="UTC")
    return pd.DataFrame(
        {"open": [r[2] for r in rows], "high": [r[0] for r in rows],
         "low": [r[1] for r in rows], "close": [r[2] for r in rows],
         "spread": [spread] * len(rows)},
        index=index,
    )


def with_range_then(rows, high=44010.0, low=43990.0, **kw):
    """A clean 15-bar range (200 broker points wide), then ``rows``."""
    return session([(high, low, 44000.0)] * 15 + list(rows), **kw)


CFG = OrbConfig(min_range_adr_fraction=0.0, max_range_adr_fraction=10.0)


def one(frame, cfg=CFG, sim=None, day=DAY, adr=1000.0):
    return bt.simulate_session(frame, "US30", US, day, cfg, POINT, adr,
                               sim or bt.SimConfig())


# ------------------------------ a single session ------------------------------
def test_a_breakout_that_reaches_its_target_pays_the_configured_r():
    trades, _ = one(with_range_then(
        [(44020, 44005, 44015)] +                      # close-through long
        [(44070, 44010, 44065)] * 5                    # runs to the target
    ))
    assert len(trades) == 1
    t = trades[0]
    assert t.direction == BUY and t.exit_reason == bt.EXIT_TARGET
    assert t.r == pytest.approx(2.0)


def test_a_breakout_that_fails_loses_exactly_one_r():
    trades, _ = one(with_range_then(
        [(44020, 44005, 44015)] +
        [(44016, 43980, 43985)] * 3                    # straight back through the low
    ))
    assert len(trades) == 1
    assert trades[0].exit_reason == bt.EXIT_STOP
    assert trades[0].r == pytest.approx(-1.0)


def test_the_bar_that_triggers_the_entry_cannot_also_resolve_it():
    """A one-bar lookahead worth a great deal of imaginary profit: this bar's
    range spans the target, but the position does not exist until it closes."""
    trades, _ = one(with_range_then([
        (44120, 44005, 44015),                         # triggers AND spans the target
        (44016, 44014, 44015),
    ]))
    assert len(trades) == 1
    assert trades[0].exit_reason == bt.EXIT_SESSION_CLOSE, \
        "the trigger bar resolved its own trade"


def test_an_ambiguous_bar_is_resolved_as_a_loss_and_flagged():
    """Stop and target both inside one minute: OHLC cannot say which was first."""
    trades, _ = one(with_range_then([
        (44020, 44005, 44015),                         # entry at 44015, stop 43990
        (44070, 43980, 44000),                         # spans target *and* stop
    ]))
    assert len(trades) == 1
    assert trades[0].exit_reason == bt.EXIT_STOP
    assert trades[0].ambiguous is True


def test_the_optimistic_setting_takes_the_target_instead_and_still_flags_it():
    trades, _ = one(with_range_then([
        (44020, 44005, 44015),
        (44070, 43980, 44000),
    ]), sim=bt.SimConfig(pessimistic_fills=False))
    assert trades[0].exit_reason == bt.EXIT_TARGET
    assert trades[0].ambiguous is True


def test_an_unresolved_position_is_flat_before_the_session_closes():
    """A CFD charges swap overnight and gaps over the weekend."""
    n = int((US.close_utc(DAY) - OPEN).total_seconds() // 60)
    trades, _ = one(with_range_then(
        [(44020, 44005, 44015)] + [(44016, 44014, 44015)] * (n - 16)))
    assert len(trades) == 1
    t = trades[0]
    assert t.exit_reason == bt.EXIT_SESSION_CLOSE
    assert t.exit_at <= US.close_utc(DAY) - timedelta(minutes=10)


def test_the_breakeven_stop_turns_a_reversal_into_a_scratch():
    trades, _ = one(with_range_then([
        (44020, 44005, 44015),                         # entry 44016, risk 26, 1R = 44042
        (44050, 44030, 44045),                         # reaches 1R: stop moves to entry
        (44045, 44000, 44005),                         # falls back through the entry
    ]))
    assert len(trades) == 1
    t = trades[0]
    assert t.exit_reason == bt.EXIT_BREAKEVEN
    assert t.r == pytest.approx(0.0, abs=0.05)
    assert t.stop == pytest.approx(43990.0), "the recorded stop is the one placed"
    assert t.final_stop == pytest.approx(t.entry), "which is not where it ended up"


def test_the_breakeven_move_waits_for_the_bar_to_finish():
    """Moving the stop mid-bar and then testing it on the same bar lets the
    stop tighten onto a low that had already happened."""
    trades, _ = one(with_range_then([
        (44020, 44005, 44015),
        (44045, 44012, 44014),      # touches 1R and dips below entry in one bar
    ]))
    assert trades[0].exit_reason == bt.EXIT_SESSION_CLOSE


def test_one_direction_per_session_refuses_the_reversal():
    """The range day that otherwise takes four losses in a row."""
    trades, _ = one(with_range_then(
        [(44020, 44005, 44015)] +
        [(44016, 43980, 43985)] * 2 +                  # long stopped out
        [(43990, 43950, 43960)] * 4                    # then breaks down
    ), cfg=OrbConfig(min_range_adr_fraction=0.0, max_range_adr_fraction=10.0,
                     max_trades_per_session=2))
    assert [t.direction for t in trades] == [BUY]


def test_allowing_both_directions_takes_the_reversal():
    trades, _ = one(with_range_then(
        [(44020, 44005, 44015)] +
        [(44016, 43980, 43985)] * 2 +
        [(43990, 43950, 43960)] * 4
    ), cfg=OrbConfig(min_range_adr_fraction=0.0, max_range_adr_fraction=10.0,
                     max_trades_per_session=2, one_direction_per_session=False))
    assert [t.direction for t in trades] == [BUY, SELL]


def test_the_bar_that_closed_a_trade_cannot_open_the_next_one():
    """Re-entering on the price that just stopped the previous trade out is a
    different strategy, and a favourable one to reconstruct from OHLC."""
    reentry = OrbConfig(min_range_adr_fraction=0.0, max_range_adr_fraction=10.0,
                        max_trades_per_session=3, breakeven_at_r=None)
    trades, _ = one(with_range_then([
        (44020, 44005, 44015),                  # entry 44016, stop 43990
        (44025, 43980, 44020),                  # stops out, and closes back above
        (44008, 43992, 44000),                  # back inside: no new trigger
    ]), cfg=reentry)
    assert len(trades) == 1


def test_a_later_bar_may_re_enter_when_the_cap_allows_it():
    reentry = OrbConfig(min_range_adr_fraction=0.0, max_range_adr_fraction=10.0,
                        max_trades_per_session=3, breakeven_at_r=None)
    trades, _ = one(with_range_then([
        (44020, 44005, 44015),
        (44025, 43980, 44020),                  # stops out
        (44030, 44018, 44025),                  # a fresh close above the range
        (44100, 44030, 44095),
    ]), cfg=reentry)
    assert len(trades) == 2
    assert all(t.direction == BUY for t in trades)


def test_a_range_that_holds_all_session_is_reported_as_a_skip_with_a_reason():
    trades, skips = one(with_range_then([(44008, 43992, 44000)] * 30))
    assert trades == []
    assert len(skips) == 1 and "range held" in skips[0].reason


def test_no_entries_after_the_window_closes():
    trades, _ = one(with_range_then(
        [(44008, 43992, 44000)] * 60 +                 # quiet for an hour
        [(44020, 44005, 44015)] * 5                    # breaks out afterwards
    ), cfg=OrbConfig(min_range_adr_fraction=0.0, max_range_adr_fraction=10.0,
                     entry_window_minutes=30))
    assert trades == []


# ------------------------------ costs ------------------------------
def test_a_long_is_filled_at_the_ask_and_a_short_at_the_bid():
    """Bars are the bid series. Charging the spread as a flat deduction gets
    the money roughly right and the stop distance wrong.

    The range here is 1,000 points wide so a 100-point spread still clears the
    cost filter -- otherwise the day is refused before any fill happens, which
    is correct behaviour but tests a different rule.
    """
    wide = dict(high=44050.0, low=43950.0)
    long_trades, _ = one(with_range_then(
        [(44060, 44005, 44055)] + [(44300, 44050, 44290)] * 5,
        spread=100.0, **wide))
    assert long_trades[0].entry == pytest.approx(44055.0 + 100 * POINT)

    short_trades, _ = one(with_range_then(
        [(43995, 43940, 43945)] + [(43950, 43700, 43710)] * 5,
        spread=100.0, **wide))
    assert short_trades[0].entry == pytest.approx(43945.0)


def test_the_spread_shows_up_as_a_lower_win_rate_not_a_smaller_win():
    """With risk-based sizing a win is still 2R; the spread pushes the target
    further away in price, so fewer trades reach it."""
    # The cost *filter* is switched off here so the two runs differ only in
    # their fills; a 180-point spread on a 200-point range is refused outright
    # by the wall, which is correct but is a different rule from this one.
    fills_only = OrbConfig(min_range_adr_fraction=0.0, max_range_adr_fraction=10.0,
                           min_range_cost_multiple=0.0, min_target_cost_multiple=0.0)
    rows = [(44020, 44005, 44015)] + [(44070, 44050, 44065)] * 5
    cheap, _ = one(with_range_then(rows, spread=10.0), cfg=fills_only)
    dear, _ = one(with_range_then(rows, spread=180.0), cfg=fills_only)
    assert cheap[0].exit_reason == bt.EXIT_TARGET
    assert cheap[0].r == pytest.approx(2.0)
    assert dear[0].exit_reason == bt.EXIT_SESSION_CLOSE, \
        "a wider spread must not still reach the same target"


def test_commission_is_charged_on_top_of_the_spread():
    rows = [(44020, 44005, 44015)] + [(44070, 44010, 44065)] * 5
    free, _ = one(with_range_then(rows))
    charged, _ = one(with_range_then(rows), sim=bt.SimConfig(commission_points=20.0))
    assert charged[0].r < free[0].r
    assert charged[0].cost_points == pytest.approx(free[0].cost_points + 20.0)


def test_zero_cost_mode_removes_both():
    """And removes them from the *filters* too, or the counterfactual is neither
    the strategy nor a comparison to it: free fills judged against a real wall."""
    rows = [(44020, 44005, 44015)] + [(44070, 44050, 44065)] * 5
    trades, _ = one(with_range_then(rows, spread=100.0),
                    sim=bt.SimConfig(commission_points=20.0, zero_cost=True))
    assert len(trades) == 1, "the real-spread cost filter was still applied"
    assert trades[0].cost_points == 0.0
    assert trades[0].entry == pytest.approx(44015.0)


def test_slippage_always_hurts_whichever_way_the_trade_goes():
    rows = [(44020, 44005, 44015)] + [(44070, 44010, 44065)] * 5
    trades, _ = one(with_range_then(rows), sim=bt.SimConfig(slippage_points=50.0))
    assert trades[0].entry > 44015.0 + 10 * POINT


# ------------------------------ the whole run ------------------------------
def _walk(day, base, seed, drift=0.0, noise=3.0, spec=US, spread=20.0):
    rng = np.random.default_rng(seed)
    start, end = spec.open_utc(day), spec.close_utc(day)
    n = int((end - start).total_seconds() // 60)
    index = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    close = base + np.cumsum(rng.normal(drift, noise, n))
    open_ = np.concatenate([[base], close[:-1]])
    return pd.DataFrame({
        "open": open_, "close": close,
        "high": np.maximum(open_, close) + rng.uniform(0, 1.5, n),
        "low": np.minimum(open_, close) - rng.uniform(0, 1.5, n),
        "spread": np.full(n, spread),
    }, index=index)


def history(seed, sessions=120, drift=0.0, spec=US):
    day, days = date(2025, 1, 6), []
    while len(days) < sessions:
        if spec.is_open_weekday(day):
            days.append(day)
        day += timedelta(days=1)
    frames, base = [], 44000.0
    for i, d in enumerate(days):
        frame = _walk(d, base, seed * 977 + i, drift=drift, spec=spec)
        base = float(frame["close"].iloc[-1])
        frames.append(frame)
    out = pd.concat(frames).sort_index()
    out.index.name = "ts"
    return out


def test_a_driftless_random_walk_yields_about_nothing():
    """The lookahead detector.

    A random walk contains no edge, so a correct simulator must find none. A
    peeking one reports something emphatic -- half an R per trade or more --
    and reports it with a straight face. The tolerance here is loose on
    purpose: it is sized to catch a bug, not to make a claim about noise.
    """
    expectancies = []
    for seed in range(3):
        report = bt.run({"US30": history(seed)}, points={"US30": POINT},
                        sim=bt.SimConfig(zero_cost=True))
        assert report.count > 60, "too few trades to conclude anything"
        expectancies.append(report.expectancy_r)
    assert abs(float(np.mean(expectancies))) < 0.25, \
        f"a driftless walk produced {np.mean(expectancies):+.3f}R per trade"


def test_a_drifting_market_is_captured_and_its_mirror_loses():
    """The baseline doing the job it exists for."""
    frames = {"US30": history(7, drift=0.35)}
    results = bt.compare(frames, points={"US30": POINT})
    assert results["strategy"].expectancy_r > 0.2
    assert results["inverted"].expectancy_r < 0.0


def test_the_inverted_baseline_is_a_real_trade_not_a_free_one():
    """An earlier mirror put the stop on the wrong side of the entry and
    reported a 100% win rate for it."""
    results = bt.compare({"US30": history(3)}, points={"US30": POINT})
    inverted = results["inverted"]
    assert inverted.count > 0
    assert inverted.win_rate < 0.9
    for t in inverted.trades:
        if t.direction == BUY:
            assert t.stop < t.entry < t.target
        else:
            assert t.target < t.entry < t.stop


def test_costs_are_visible_rather_than_assumed_small():
    frames = {"US30": history(1)}
    results = bt.compare(frames, points={"US30": POINT})
    assert results["zero_cost"].expectancy_r > results["strategy"].expectancy_r


def test_every_session_is_accounted_for_as_a_trade_or_a_reason():
    report = bt.run({"US30": history(2, sessions=40)}, points={"US30": POINT})
    days_seen = {t.session_date for t in report.trades} | {
        s.session_date for s in report.skips}
    assert len(days_seen) == report.sessions
    assert report.sessions_by_symbol == {"US30": report.sessions}


def test_a_contradictory_config_is_refused_before_any_bars_are_read():
    with pytest.raises(ValueError, match="inverted"):
        bt.run({"US30": history(0, sessions=5)},
               cfg=OrbConfig(min_range_adr_fraction=0.9,
                             max_range_adr_fraction=0.1))


def test_the_first_sessions_are_skipped_for_want_of_a_yardstick():
    report = bt.run({"US30": history(0, sessions=20)}, points={"US30": POINT})
    assert any("average-daily-range" in s.reason for s in report.skips)


def test_fx_gets_a_thirty_minute_range_without_being_told():
    frames = {"EURUSD": history(4, sessions=30, spec=SESSIONS["london"])}
    report = bt.run(frames, points={"EURUSD": 0.00001},
                    sessions={"EURUSD": "london"})
    for t in report.trades:
        assert t.entry_at >= SESSIONS["london"].open_utc(t.session_date) + \
            timedelta(minutes=30)


def test_reports_summarise_without_pretending_to_precision():
    report = bt.run({"US30": history(5, sessions=60)}, points={"US30": POINT})
    text = bt.format_report(report)
    assert "OPENING RANGE BREAKOUT" in text
    assert "trades/session" in text
    assert report.money(10_000, 0.005) == pytest.approx(report.net_r * 50.0)


def test_an_empty_result_says_so_rather_than_dividing_by_zero():
    empty = bt.OrbReport()
    assert empty.win_rate == 0.0 and empty.expectancy_r == 0.0
    assert empty.trade_rate == 0.0 and empty.max_drawdown_r == 0.0
    assert "no trades" in empty.summary()
    assert "No instrument" not in bt.format_report(empty)


def test_the_comparison_calls_out_a_strategy_beaten_by_its_own_mirror():
    good = bt.OrbReport(trades=[], sessions=1)
    text = bt.format_comparison({"strategy": good, "inverted": good})
    assert "Nothing traded" in text
