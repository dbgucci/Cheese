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
