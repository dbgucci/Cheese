"""Break, then retest, then nothing more.

The state machine is the part that can be silently wrong: a bot that alerts the
break twice, or alerts a retest on a failed break, is still a bot that sends
alerts, and nothing about its output says it is broken.
"""

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from cheese_signals.markets import orb, signals
from cheese_signals.markets.clock import SESSIONS
from cheese_signals.markets.execution import BUY, SELL
from cheese_signals.markets.mt5_bridge import SymbolSpec
from cheese_signals.markets.signals import BREAK, RETEST, SignalBot, SignalConfig

US = SESSIONS["us_cash"]
DAY = date(2026, 3, 2)                 # Monday, US winter: open 14:30 UTC
OPEN = datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc)
POINT = 0.1

US30 = SymbolSpec(name="US30", point=POINT, digits=1, contract_size=1.0,
                  tick_value=1.0, tick_size=1.0, volume_min=0.1, volume_step=0.1,
                  volume_max=50.0, spread_current=20.0)

# The filters are switched off in most tests so each one measures the state
# machine rather than the day filters, which have their own suite.
LOOSE = orb.OrbConfig(min_range_adr_fraction=0.0, max_range_adr_fraction=10.0)


class FakeSource:
    """A bar source that serves a fixed frame, like a broker terminal would."""

    def __init__(self, bars, spec=US30):
        self.bars = bars
        self._spec = spec
        self.calls = 0

    def spec(self, symbol):
        return self._spec

    def history(self, symbol, timeframe, start, end):
        self.calls += 1
        df = self.bars
        return df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]


def frame(rows, start=OPEN, spread=10.0):
    """``rows`` are (high, low, close) triples, one per minute."""
    index = pd.date_range(start, periods=len(rows), freq="1min", tz="UTC")
    return pd.DataFrame(
        {"open": [r[2] for r in rows], "high": [r[0] for r in rows],
         "low": [r[1] for r in rows], "close": [r[2] for r in rows],
         "spread": [spread] * len(rows)},
        index=index,
    )


def range_then(rows, high=44010.0, low=43990.0, **kw):
    """A 200-point opening range over 15 bars, then whatever follows."""
    return frame([(high, low, 44000.0)] * 15 + list(rows), **kw)


def make(bars, cfg=LOOSE, **kw):
    source = FakeSource(bars)
    config = SignalConfig(symbols=["US30"], orb=cfg, **kw)
    return SignalBot(source, config, specs={"US30": US30}), source


def at(minutes, seconds=5):
    return OPEN + timedelta(minutes=minutes, seconds=seconds)


# ------------------------------ the range ------------------------------
def test_nothing_happens_before_the_range_completes():
    bot, _ = make(range_then([]))
    assert bot.cycle(at(8)) == []


def test_the_range_is_the_first_fifteen_minutes_for_every_instrument():
    """The rule asked for is 15 minutes, not 15 for indices and 30 for gold."""
    assert SignalConfig().per_symbol_range_minutes is False
    bot, _ = make(range_then([(44020, 44005, 44015)]))
    out = bot.cycle(at(16))
    assert len(out) == 1
    assert out[0].range_high == 44010.0 and out[0].range_low == 43990.0


def test_the_range_is_measured_once_and_then_remembered():
    """Re-deriving it every poll is wasted work, and lets a late bar move a
    level the break was already judged against."""
    bot, source = make(range_then([(44020, 44005, 44015)]))
    bot.cycle(at(16))
    watch = bot.watches[("US30", DAY)]
    first = watch.range_
    bot.cycle(at(17))
    assert bot.watches[("US30", DAY)].range_ is first


# ------------------------------ the break ------------------------------
def test_a_close_above_the_range_sends_a_break_signal():
    bot, _ = make(range_then([(44020, 44005, 44015)]))
    out = bot.cycle(at(16))
    assert [s.kind for s in out] == [BREAK]
    signal = out[0]
    assert signal.direction == BUY
    assert signal.entry == 44015.0
    assert signal.stop == 43990.0
    assert signal.target == pytest.approx(44015.0 + 2 * 25.0)


def test_a_close_below_the_range_sends_a_sell_break():
    bot, _ = make(range_then([(43995, 43980, 43985)]))
    out = bot.cycle(at(16))
    assert out[0].direction == SELL
    assert out[0].stop == 44010.0


def test_a_wick_through_the_level_is_not_a_break():
    """It closed back inside, so nothing has happened yet."""
    bot, _ = make(range_then([(44030, 44000, 44005)]))
    assert bot.cycle(at(16)) == []
    assert bot.watches[("US30", DAY)].state == signals.ARMED


def test_the_break_is_only_sent_once():
    """Price stays above the range all morning; a naive poll would resend it
    every twenty seconds.

    The follow-up bars deliberately hold well clear of the 44010 level: a bar
    that dips back to it and closes above is a retest, which is a different
    alert and has its own tests.
    """
    bot, _ = make(range_then(
        [(44020, 44005, 44015)] + [(44035, 44025, 44030)] * 5))
    first = bot.cycle(at(16))
    assert len(first) == 1
    for minute in (17, 18, 19, 20):
        assert bot.cycle(at(minute)) == []
    assert bot.watches[("US30", DAY)].state == signals.BROKEN


# ------------------------------ the retest ------------------------------
def test_a_pullback_to_the_level_that_holds_sends_a_retest():
    bot, _ = make(range_then([
        (44020, 44005, 44015),        # break: closes above 44010
        (44025, 44018, 44022),        # runs away
        (44022, 44009, 44014),        # back to the level, closes above it
    ]))
    assert [s.kind for s in bot.cycle(at(16))] == [BREAK]
    assert bot.cycle(at(17)) == []
    out = bot.cycle(at(18))
    assert [s.kind for s in out] == [RETEST]
    signal = out[0]
    assert signal.entry == 44010.0, "the retest entry is the broken level"
    assert signal.stop == 43990.0
    assert "held it" in signal.reason


def test_the_retest_is_only_sent_once_and_ends_the_day():
    bot, _ = make(range_then([
        (44020, 44005, 44015),
        (44022, 44009, 44014),        # retest
        (44022, 44009, 44014),        # would retest again
        (44030, 44012, 44028),
    ]))
    bot.cycle(at(16))
    assert [s.kind for s in bot.cycle(at(17))] == [RETEST]
    for minute in (18, 19, 20):
        assert bot.cycle(at(minute)) == []
    assert bot.watches[("US30", DAY)].state == signals.RETESTED


def test_a_pullback_that_closes_back_inside_is_a_failure_not_a_retest():
    """The distinction that matters: alerting this as a retest puts the entry at
    the start of a reversal."""
    bot, _ = make(range_then([
        (44020, 44005, 44015),        # break
        (44016, 43995, 44000),        # closes back inside the range
        (44022, 44009, 44014),        # a "retest" that must not fire
    ]))
    bot.cycle(at(16))
    assert bot.cycle(at(17)) == []
    assert bot.watches[("US30", DAY)].state == signals.FAILED
    assert bot.cycle(at(18)) == []


def test_a_shallow_pullback_does_not_count_as_a_retest():
    bot, _ = make(range_then([
        (44020, 44005, 44015),
        (44030, 44025, 44028),        # never comes near 44010
    ]))
    bot.cycle(at(16))
    assert bot.cycle(at(17)) == []
    assert bot.watches[("US30", DAY)].state == signals.BROKEN


def test_the_retest_tolerance_is_a_fraction_of_the_range():
    """A pullback stopping a few points short is still a retest; demanding an
    exact touch means most of them are never seen."""
    bars = range_then([
        (44020, 44005, 44015),
        (44024, 44011.5, 44020),      # 15 points above the 44010 level
    ])
    tight, _ = make(bars, retest_tolerance_fraction=0.0)
    tight.cycle(at(16))
    assert tight.cycle(at(17)) == []

    loose, _ = make(bars, retest_tolerance_fraction=0.10)   # 20pt of slack
    loose.cycle(at(16))
    assert [s.kind for s in loose.cycle(at(17))] == [RETEST]


def test_the_sell_side_retest_mirrors():
    bot, _ = make(range_then([
        (43995, 43980, 43985),        # break down through 43990
        (43991, 43975, 43980),        # back up to the level, closes below it
    ]))
    assert [s.kind for s in bot.cycle(at(16))] == [BREAK]
    out = bot.cycle(at(17))
    assert [s.kind for s in out] == [RETEST]
    assert out[0].entry == 43990.0 and out[0].stop == 44010.0


def test_both_stages_can_arrive_in_one_poll():
    """If the bot starts late, or a poll is missed, the break and its retest are
    both already in the bars. Dropping one would strand the state machine."""
    bot, _ = make(range_then([
        (44020, 44005, 44015),
        (44022, 44009, 44014),
    ]))
    out = bot.cycle(at(18))
    assert [s.kind for s in out] == [BREAK, RETEST]


# ------------------------------ timing ------------------------------
def test_the_bar_in_progress_is_never_used():
    """Its close does not exist yet; using it produces alerts that vanish."""
    bot, _ = make(range_then([(44020, 44005, 44015)]))
    assert bot.cycle(OPEN + timedelta(minutes=15, seconds=30)) == []
    assert bot.cycle(at(16)) != []


def test_no_break_by_the_deadline_closes_the_day():
    bot, _ = make(range_then([(44008, 43992, 44000)] * 200),
                  cfg=orb.OrbConfig(min_range_adr_fraction=0.0,
                                    max_range_adr_fraction=10.0,
                                    entry_window_minutes=30))
    assert bot.cycle(at(50)) == []
    assert bot.watches[("US30", DAY)].state == signals.SKIPPED
    assert "no break" in bot.watches[("US30", DAY)].reason


def test_a_break_already_made_still_gets_its_retest_after_the_deadline():
    """The window governs new breaks. A setup already armed is still live."""
    bot, _ = make(range_then(
        [(44020, 44005, 44015)] + [(44030, 44020, 44025)] * 40 + [(44025, 44009, 44014)]),
        cfg=orb.OrbConfig(min_range_adr_fraction=0.0, max_range_adr_fraction=10.0,
                          entry_window_minutes=20))
    assert [s.kind for s in bot.cycle(at(16))] == [BREAK]
    out = bot.cycle(at(60))
    assert [s.kind for s in out] == [RETEST]


def test_weekends_are_not_watched():
    saturday = datetime(2026, 3, 7, 15, 0, tzinfo=timezone.utc)
    bot, _ = make(range_then([(44020, 44005, 44015)]))
    assert bot.cycle(saturday) == []


# ------------------------------ the filters ------------------------------
def test_a_range_narrower_than_the_spread_is_skipped_with_a_reason():
    bot, _ = make(range_then([(44002, 44000, 44001)], high=44000.5, low=44000.0),
                  cfg=orb.OrbConfig())
    assert bot.cycle(at(16)) == []
    watch = bot.watches[("US30", DAY)]
    assert watch.state == signals.SKIPPED
    assert "round-trip cost" in watch.reason


def test_the_filters_can_be_turned_off_for_a_signals_only_bot():
    bot, _ = make(range_then([(44002, 44000, 44001)], high=44000.5, low=44000.0),
                  cfg=orb.OrbConfig(), apply_filters=False)
    assert len(bot.cycle(at(16))) == 1


def test_an_unmapped_symbol_is_reported_rather_than_guessed():
    source = FakeSource(range_then([(44020, 44005, 44015)]))
    bot = SignalBot(source, SignalConfig(symbols=["BTCUSD"], orb=LOOSE))
    assert bot.cycle(at(16)) == []
    assert any("no session mapped" in note for note in bot.notes)


def test_a_source_error_is_reported_and_does_not_stop_the_cycle():
    source = FakeSource(range_then([(44020, 44005, 44015)]))

    def boom(*a, **k):
        raise RuntimeError("connection lost")

    source.history = boom
    bot = SignalBot(source, SignalConfig(symbols=["US30"], orb=LOOSE),
                    specs={"US30": US30})
    assert bot.cycle(at(16)) == []
    assert any("connection lost" in note for note in bot.notes)


def test_a_standing_reason_is_not_repeated_every_poll():
    bot, _ = make(range_then([(44002, 44000, 44001)], high=44000.5, low=44000.0),
                  cfg=orb.OrbConfig())
    bot.cycle(at(16))
    before = len(bot.notes)
    bot.cycle(at(17))
    bot.cycle(at(18))
    assert len(bot.notes) == before


def test_a_contradictory_config_will_not_start():
    with pytest.raises(ValueError, match="inverted"):
        SignalBot(FakeSource(range_then([])), SignalConfig(
            symbols=["US30"],
            orb=orb.OrbConfig(min_range_adr_fraction=0.9,
                              max_range_adr_fraction=0.1)))


def test_no_symbols_will_not_start():
    with pytest.raises(ValueError, match="nothing to watch"):
        SignalBot(FakeSource(range_then([])), SignalConfig())


# ------------------------------ the alert itself ------------------------------
def test_the_alert_carries_everything_needed_to_act_on_it():
    bot, _ = make(range_then([(44020, 44005, 44015)]))
    text = bot.cycle(at(16))[0].format()
    for expected in ("BREAK", "US30", "BUY", "Entry", "Stop", "Target",
                     "Range", "Spread", "UTC", "flat by"):
        assert expected in text, expected


def test_prices_are_printed_at_the_instruments_own_precision():
    """US30 quotes to one decimal; printing it to five looks like a bug."""
    bot, _ = make(range_then([(44020, 44005, 44015)]))
    text = bot.cycle(at(16))[0].format()
    assert "44015.0" in text and "44015.00000" not in text


def test_the_retest_alert_says_it_is_a_retest():
    bot, _ = make(range_then([
        (44020, 44005, 44015),
        (44022, 44009, 44014),
    ]))
    bot.cycle(at(16))
    signal = bot.cycle(at(17))[0]
    assert signal.headline.startswith("RETEST")
    assert "RETEST" in signal.one_line()


def test_the_signal_log_accumulates_both_stages():
    bot, _ = make(range_then([
        (44020, 44005, 44015),
        (44022, 44009, 44014),
    ]))
    bot.cycle(at(16))
    bot.cycle(at(17))
    assert [s.kind for s in bot.signals] == [BREAK, RETEST]


# ------------------------------ the clock ------------------------------
def test_a_source_without_a_clock_is_assumed_to_be_utc_out_loud():
    bot, _ = make(range_then([]))
    clock = bot.calibrate()
    assert clock.server_offset_minutes == 0
    assert any("assuming" in note for note in bot.notes)


def test_a_broker_clock_offset_is_measured_and_corrected():
    """The EET case: bars stamped three hours ahead. Uncorrected, the bot looks
    for the New York open in the middle of the London afternoon."""
    shifted = range_then([(44020, 44005, 44015)])
    shifted.index = shifted.index + timedelta(minutes=180)
    source = FakeSource(shifted)
    source.server_time = lambda symbol: at(16) + timedelta(minutes=180)
    bot = SignalBot(source, SignalConfig(symbols=["US30"], orb=LOOSE),
                    specs={"US30": US30})
    bot.calibrate(real_now=at(16))
    assert bot.clock.server_offset_minutes == 180
    assert [s.kind for s in bot.cycle(at(16))] == [BREAK]


# ------------------------------ the result ------------------------------
#
# The retest is the entry, so from there the alert can be scored. These numbers
# are all derived from one setup: a 200-point range (44010/43990), a retest entry
# at the 44010 level, a stop at 43990 (200 points of risk) and a 2R target at
# 44050. The frame's spread is 10 points, so a 2R win nets 1.95R and a stop nets
# -1.05R -- the gap between gross and net is the whole reason both are reported.
BREAK_BAR = (44020, 44005, 44015)
RETEST_BAR = (44022, 44009, 44014)


def run_to(bot, last_minute):
    """Poll every minute up to and including ``last_minute``, as the app does."""
    for minute in range(16, last_minute + 1):
        bot.cycle(at(minute))


def test_a_target_hit_after_the_retest_is_recorded_as_a_win():
    bot, _ = make(range_then([
        BREAK_BAR,
        RETEST_BAR,
        (44055, 44020, 44050),          # trades through the 44050 target
    ]))
    run_to(bot, 18)
    assert len(bot.outcomes) == 1
    outcome = bot.outcomes[0]
    assert outcome.result == signals.WIN
    assert outcome.exit_price == 44050.0
    assert outcome.r_gross == pytest.approx(2.0)
    assert outcome.r_net == pytest.approx(1.95)
    assert outcome.points == pytest.approx(400.0)


def test_a_stop_hit_after_the_retest_is_recorded_as_a_loss():
    bot, _ = make(range_then([
        BREAK_BAR,
        RETEST_BAR,
        (44012, 43985, 43990),          # trades through the 43990 stop
    ]))
    run_to(bot, 18)
    outcome = bot.outcomes[0]
    assert outcome.result == signals.LOSS
    assert outcome.exit_price == 43990.0
    assert outcome.r_gross == pytest.approx(-1.0)
    assert outcome.r_net == pytest.approx(-1.05), "the spread is charged on a loss too"


def test_a_bar_holding_both_levels_is_scored_as_a_loss():
    """Which came first is not in the OHLC. Guessing favourably is the easiest
    way to invent a win rate, so the loss is assumed and the trade is flagged."""
    bot, _ = make(range_then([
        BREAK_BAR,
        RETEST_BAR,
        (44060, 43980, 44020),          # spans the stop and the target
    ]))
    run_to(bot, 18)
    outcome = bot.outcomes[0]
    assert outcome.result == signals.LOSS
    assert outcome.ambiguous is True
    assert "both" in outcome.format()


def test_the_entry_bar_cannot_resolve_the_trade():
    """The fill is at the level somewhere inside that minute and the OHLC does
    not say where, so letting it also hit the target is a bar of lookahead."""
    bot, _ = make(range_then([
        BREAK_BAR,
        (44060, 44009, 44014),          # the retest bar itself spans the target
    ]))
    run_to(bot, 17)
    assert [s.kind for s in bot.signals] == [BREAK, RETEST]
    assert bot.outcomes == []
    assert bot.watches[("US30", DAY)].following is True


def test_a_trade_that_touches_neither_level_is_flattened_at_the_cut_off():
    """Not a loss: it did not lose, and counting it as one makes a quiet morning
    look like a bad strategy. The exit is the price at the flatten moment."""
    from dataclasses import replace

    # 372 minutes before the 21:00 UTC close puts the flat-by at 14:48, which is
    # inside this frame instead of six hours past the end of it.
    cfg = replace(LOOSE, flat_before_close_minutes=372)
    bot, _ = make(range_then([
        BREAK_BAR,
        RETEST_BAR,
        (44020, 44008, 44015),          # neither level
        (44030, 44005, 44025),          # 14:48: the cut-off, opens at 44025
    ]), cfg=cfg)
    run_to(bot, 19)
    outcome = bot.outcomes[0]
    assert outcome.result == signals.FLAT
    assert outcome.exit_price == 44025.0
    assert outcome.r_gross == pytest.approx(0.75)
    assert "flattened" in outcome.reason


def test_the_sell_side_result_mirrors():
    bot, _ = make(range_then([
        (43995, 43980, 43985),          # break down through 43990
        (43991, 43975, 43980),          # retest of the 43990 level
        (43980, 43945, 43950),          # trades through the 43950 target
    ]))
    run_to(bot, 18)
    outcome = bot.outcomes[0]
    assert outcome.direction == SELL
    assert outcome.result == signals.WIN
    assert outcome.exit_price == 43950.0
    assert outcome.r_gross == pytest.approx(2.0)


def test_a_result_is_recorded_once_no_matter_how_often_it_is_polled():
    bot, _ = make(range_then([
        BREAK_BAR,
        RETEST_BAR,
        (44055, 44020, 44050),
        (44060, 44040, 44055),
        (44030, 43980, 43990),          # would hit the stop, after the exit
    ]))
    run_to(bot, 25)
    assert len(bot.outcomes) == 1
    assert bot.watches[("US30", DAY)].done is True


def test_the_entry_window_closing_does_not_kill_a_running_trade():
    """The deadline stops new entries. Applied to a watch that is already in a
    trade it marked the day skipped and the result was never recorded."""
    bot, _ = make(range_then([BREAK_BAR, RETEST_BAR]))
    run_to(bot, 18)
    bot.cycle(at(200))                  # long past the 120-minute entry window
    watch = bot.watches[("US30", DAY)]
    assert watch.state == signals.RETESTED
    assert watch.following is True, "still waiting for a level to be touched"


def test_tracking_can_be_switched_off_and_then_the_day_ends_at_the_retest():
    bot, _ = make(range_then([
        BREAK_BAR,
        RETEST_BAR,
        (44055, 44020, 44050),
    ]), track_outcomes=False)
    run_to(bot, 18)
    watch = bot.watches[("US30", DAY)]
    assert watch.trade is None
    assert watch.done is True
    assert bot.outcomes == []


# ------------------------------ the tally ------------------------------
def outcome(result, r_net=1.95, r_gross=2.0):
    return signals.Outcome(
        symbol="US30", direction=BUY, result=result, entry=1.0, stop=0.9,
        target=1.2, exit_price=1.2, opened_at=OPEN, closed_at=OPEN,
        risk_points=100.0, points=200.0, cost_points=10.0, r_gross=r_gross,
        r_net=r_net, session_label="US cash", reason="")


def test_the_tally_counts_wins_losses_and_flats():
    tally = signals.Tally()
    for result in (signals.WIN, signals.WIN, signals.LOSS):
        tally.add(outcome(result))
    assert (tally.wins, tally.losses, tally.flats) == (2, 1, 0)
    assert tally.resolved == 3


def test_a_flat_is_left_out_of_the_win_rate():
    """It neither won nor lost. Counting it as a loss understates the edge."""
    tally = signals.Tally()
    tally.add(outcome(signals.WIN))
    tally.add(outcome(signals.LOSS))
    tally.add(outcome(signals.FLAT, r_net=0.0, r_gross=0.0))
    assert tally.win_rate == pytest.approx(0.5)
    assert "flat" in tally.summary()


def test_the_tally_carries_total_r_as_well_as_the_win_rate():
    """A 30% hit rate at 2R makes money and a 60% one at 0.5R does not, so the
    win rate alone cannot say whether the alerts are worth taking."""
    tally = signals.Tally()
    tally.add(outcome(signals.WIN))
    tally.add(outcome(signals.LOSS, r_net=-1.05, r_gross=-1.0))
    assert tally.r_net == pytest.approx(0.90)
    assert tally.r_gross == pytest.approx(1.0)
    assert "+0.90R net" in tally.summary()


def test_an_empty_tally_says_so_rather_than_showing_a_zero_win_rate():
    assert signals.Tally().summary() == "no results yet"
    assert signals.Tally().win_rate == 0.0


def test_outcomes_are_drained_so_a_poll_only_reports_what_is_new():
    bot, _ = make(range_then([
        BREAK_BAR,
        RETEST_BAR,
        (44055, 44020, 44050),
    ]))
    run_to(bot, 18)
    assert len(bot.take_outcomes()) == 1
    assert bot.take_outcomes() == []


def test_the_result_message_carries_the_numbers_needed_to_check_it():
    bot, _ = make(range_then([
        BREAK_BAR,
        RETEST_BAR,
        (44055, 44020, 44050),
    ]))
    run_to(bot, 18)
    text = bot.outcomes[0].format()
    assert "WIN" in text and "US30" in text
    assert "+1.95R net" in text
    assert "+2.00R" in text, "the gross figure is shown beside the net one"
    assert "44050" in text


def test_a_result_is_written_to_the_activity_notes_with_the_running_record():
    bot, _ = make(range_then([
        BREAK_BAR,
        RETEST_BAR,
        (44055, 44020, 44050),
    ]))
    run_to(bot, 18)
    assert any("WIN" in note and "1W / 0L" in note for note in bot.notes)


def test_a_trade_is_closed_out_when_the_bars_simply_stop():
    """A feed gap, a holiday or a Friday close means the cut-off bar never
    arrives. Without this the trade follows price into next week.

    The two-minute grace is what keeps this a fallback: at the moment the clock
    first passes the cut-off, the bar stamped at it has not closed yet, and
    settling then exits a bar early at the wrong price.
    """
    from dataclasses import replace

    cfg = replace(LOOSE, flat_before_close_minutes=372)     # cut-off 14:48
    bot, _ = make(range_then([
        BREAK_BAR,
        RETEST_BAR,
        (44020, 44008, 44015),          # 14:47, then the feed stops
    ]), cfg=cfg)
    run_to(bot, 18)
    assert bot.outcomes == [], "the cut-off bar might still be coming"
    bot.cycle(at(51))
    outcome = bot.outcomes[0]
    assert outcome.result == signals.FLAT
    assert outcome.exit_price == 44015.0, "the last price there was"
    assert "the session ended" in outcome.reason


def test_a_trade_left_open_by_a_dead_session_is_not_left_open_forever():
    """The watch is keyed by session date, so once the date rolls over nothing
    looks at it again -- and a result that is never recorded is a hole in the
    record rather than a neutral omission.

    The sweep runs an hour after the cut-off so the settlement that can see bars
    always gets there first.
    """
    from dataclasses import replace

    cfg = replace(LOOSE, flat_before_close_minutes=372)     # cut-off 14:48
    bot, _ = make(range_then([BREAK_BAR, RETEST_BAR]), cfg=cfg)
    run_to(bot, 17)
    watch = bot.watches[("US30", DAY)]
    assert watch.following is True
    bot.cycle(at(30))                   # 15:00, inside the hour of grace
    assert watch.following is True
    bot.cycle(at(140))                  # 16:50, past it
    outcome = bot.outcomes[0]
    assert outcome.result == signals.FLAT
    assert outcome.exit_price == outcome.entry, "no idea what it did, so 0R gross"
    assert outcome.r_gross == 0.0
    assert "the feed stopped" in outcome.reason
    assert watch.done is True


# ------------------------------ saying when ------------------------------
#
# The complaint that produced these: "a break came in at 12:41am, the message
# said 07:40 UTC, London open 07:00, flat by 15:20 -- that sounds confusing to
# me, much less the average person". It was, and one line of it was close to
# wrong: London opens at 08:00 London time.
def test_the_alert_gives_the_moment_in_every_clock_the_reader_might_hold():
    # Bars stamped three hours ahead, as an EET broker delivers them -- the
    # usual case, and the one that made the original message unfindable.
    shifted = range_then([BREAK_BAR])
    shifted.index = shifted.index + timedelta(minutes=180)
    source = FakeSource(shifted)
    bot = SignalBot(source, SignalConfig(symbols=["US30"], orb=LOOSE,
                                         reader_timezone="America/New_York"),
                    clock=signals.BarClock(180), specs={"US30": US30})
    text = bot.cycle(at(16))[0].format()
    assert "UTC" in text
    assert "New York — the market's own clock" in text
    assert "on your MT5 chart" in text, "the number that finds the candle"
    assert "your time" in text
    assert "2026" in text, "the date, or a review the next day cannot find it"


def test_the_session_is_described_in_its_own_clock_not_in_utc():
    """"London open 07:00" reads as a mistake to anyone who knows the market."""
    bot, _ = make(range_then([BREAK_BAR]))
    text = bot.cycle(at(16))[0].session_summary()
    assert "09:30-09:45" in text, "the US cash open is 09:30 in New York"
    assert "New York time" in text


def test_every_alert_carries_a_reference_that_can_be_quoted_back():
    bot, _ = make(range_then([BREAK_BAR, RETEST_BAR]))
    first = bot.cycle(at(16))[0]
    second = bot.cycle(at(17))[0]
    assert first.ref == "US30-0302-BREAK"
    assert second.ref == "US30-0302-RETEST"
    assert first.ref in first.format()


def test_the_result_quotes_the_entry_it_answers():
    """So a subscriber reading "WIN +1.95R" knows which alert won."""
    bot, _ = make(range_then([BREAK_BAR, RETEST_BAR, (44055, 44020, 44050)]))
    run_to(bot, 18)
    outcome = bot.outcomes[0]
    assert outcome.ref == "US30-0302-RETEST"
    assert outcome.ref in outcome.format()
    assert "UTC" in outcome.format()


def test_the_chart_time_is_absent_when_the_broker_runs_on_utc():
    """Nothing to correct, so nothing to say. An extra line reading the same as
    the one above it is noise that trains people to skip the block."""
    bot, _ = make(range_then([BREAK_BAR]))
    assert bot.clock.server_offset_minutes == 0
    assert "MT5 chart" not in bot.cycle(at(16))[0].format()


def test_the_bars_behind_an_alert_are_kept_so_it_can_be_drawn():
    """A chart cannot be drawn from a signal alone, and refetching history to
    draw one would ask the broker for the same bars twice."""
    bot, _ = make(range_then([BREAK_BAR]))
    bot.cycle(at(16))
    kept = bot.last_bars["US30"]
    assert not kept.empty
    assert {"open", "high", "low", "close"} <= set(kept.columns)


# ------------------------------ the cost of polling ------------------------------
def test_the_long_history_is_fetched_once_a_day_not_once_a_poll():
    """Three weeks of minutes is for the average-daily-range filter, which is
    measured once per instrument per day. Asking for it again every twenty
    seconds is thirty thousand rows per symbol per cycle, and the default
    watchlist is fifteen symbols.
    """
    class Recording(FakeSource):
        def __init__(self, bars):
            super().__init__(bars)
            self.spans = []

        def history(self, symbol, timeframe, start, end):
            self.spans.append((pd.Timestamp(end) - pd.Timestamp(start)).days)
            return super().history(symbol, timeframe, start, end)

    source = Recording(range_then([BREAK_BAR, RETEST_BAR]))
    bot = SignalBot(source, SignalConfig(symbols=["US30"], orb=LOOSE),
                    specs={"US30": US30})
    bot.cycle(at(16))
    bot.cycle(at(17))
    bot.cycle(at(18))
    assert source.spans[0] >= 20, "the first look needs the ADR history"
    assert all(span <= 2 for span in source.spans[1:]), \
        f"later polls still asked for {source.spans[1:]} days"


def test_a_new_day_fetches_the_long_history_again():
    """The filter has to be measured against the days behind *this* session."""
    class Recording(FakeSource):
        def __init__(self, bars):
            super().__init__(bars)
            self.spans = []

        def history(self, symbol, timeframe, start, end):
            self.spans.append((pd.Timestamp(end) - pd.Timestamp(start)).days)
            return super().history(symbol, timeframe, start, end)

    source = Recording(range_then([BREAK_BAR]))
    bot = SignalBot(source, SignalConfig(symbols=["US30"], orb=LOOSE),
                    specs={"US30": US30})
    bot.cycle(at(16))
    bot.cycle(at(17))
    # A fresh watch, as the next session gets.
    bot.watches.clear()
    source.spans.clear()
    bot.cycle(at(18))
    assert source.spans[0] >= 20
