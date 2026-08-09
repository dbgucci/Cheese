"""The live cycle: what it opens, what it refuses, and what it closes.

The broker here is a fake, but it is the same shape as ``MT5Trader`` and it
enforces what a real terminal enforces, because the point of these tests is the
wiring between the strategy, the guards and the executor -- and wiring is
exactly what a permissive fake cannot test.
"""

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from cheese_signals.markets import orb
from cheese_signals.markets.autobot import Autobot, AutobotConfig
from cheese_signals.markets.clock import SESSIONS, BarClock
from cheese_signals.markets.execution import (BUY, MAGIC, AccountState,
                                              ExecutionConfig, OrderResult, Position)
from cheese_signals.markets.guards import GuardConfig
from cheese_signals.markets.mt5_bridge import D1, SymbolSpec
from cheese_signals.markets.orb import OrbConfig

US = SESSIONS["us_cash"]
DAY = date(2026, 3, 2)                       # Monday, US winter: open 14:30 UTC
OPEN = datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc)
POINT = 0.1

US30 = SymbolSpec(name="US30", point=POINT, digits=1, contract_size=1.0,
                  tick_value=1.0, tick_size=1.0, volume_min=0.1, volume_step=0.1,
                  volume_max=50.0, spread_current=20.0, stops_level=50)

LOOSE = OrbConfig(min_range_adr_fraction=0.0, max_range_adr_fraction=10.0)


def make_bars(rows, start=OPEN, spread=20.0):
    index = pd.date_range(start, periods=len(rows), freq="1min", tz="UTC")
    return pd.DataFrame(
        {"open": [r[2] for r in rows], "high": [r[0] for r in rows],
         "low": [r[1] for r in rows], "close": [r[2] for r in rows],
         "spread": [spread] * len(rows)},
        index=index,
    )


def range_then(rows, high=44010.0, low=43990.0, **kw):
    """A 200-point opening range, then whatever happens next."""
    return make_bars([(high, low, 44000.0)] * 15 + list(rows), **kw)


class FakeBroker:
    """A terminal that rejects what a terminal rejects."""

    def __init__(self, bars, equity=50_000.0, quote=(44014.0, 44016.0),
                 daily_range=100.0, server_offset_minutes=0):
        self._bars = bars
        self._acct = AccountState(balance=equity, equity=equity, margin_free=equity)
        self._quote = quote
        self._daily_range = daily_range
        self._offset = timedelta(minutes=server_offset_minutes)
        self.positions_list: list[Position] = []
        self.sent: list = []
        self.closed: list = []
        self.modified: list = []
        self._ticket = 500

    # --- read side
    def symbols(self):
        return ["US30"]

    def spec(self, symbol):
        return US30

    def account(self):
        return self._acct

    def tick(self, symbol):
        return self._quote

    def server_time(self, symbol):
        return self._now + self._offset

    def history(self, symbol, timeframe, start, end):
        if timeframe == D1:
            # Twenty prior days, each with a fixed range, in broker time.
            days = pd.date_range(DAY - timedelta(days=30), periods=25, freq="1D",
                                 tz="UTC") + self._offset
            return pd.DataFrame({
                "open": 44000.0, "close": 44000.0,
                "high": 44000.0 + self._daily_range / 2,
                "low": 44000.0 - self._daily_range / 2,
            }, index=days)
        shifted = self._bars.copy()
        shifted.index = shifted.index + self._offset
        return shifted[(shifted.index >= pd.Timestamp(start))
                       & (shifted.index <= pd.Timestamp(end))]

    # --- write side
    def positions(self, magic=None):
        return [p for p in self.positions_list
                if magic is None or p.magic == magic]

    def send(self, order):
        spec = US30
        if spec.stops_level and abs(self._quote[1] - order.stop_loss) < \
                spec.stops_level * spec.point - 1e-9:
            return OrderResult(False, "Invalid stops", retcode=10016)
        self._ticket += 1
        self.positions_list.append(Position(
            ticket=self._ticket, symbol=order.symbol, direction=order.direction,
            lots=order.lots, open_price=self._quote[1] if order.direction == BUY
            else self._quote[0], stop_loss=order.stop_loss,
            take_profit=order.take_profit or 0.0, opened_at=self._now,
            magic=order.magic))
        self.sent.append(order)
        return OrderResult(True, ticket=self._ticket, price=self._quote[1],
                           lots=order.lots)

    def close(self, position, reason=""):
        self.positions_list = [p for p in self.positions_list
                               if p.ticket != position.ticket]
        self.closed.append((position.ticket, reason))
        return OrderResult(True, reason, ticket=position.ticket)

    def modify(self, position, stop_loss, take_profit=None):
        position.stop_loss = stop_loss
        self.modified.append((position.ticket, stop_loss))
        return OrderResult(True, ticket=position.ticket)


def build(bars, now, orb_cfg=LOOSE, live=True, guards=None, broker=None, **kw):
    broker = broker or FakeBroker(bars, **kw)
    broker._now = now
    config = AutobotConfig(
        symbols=["US30"], orb=orb_cfg,
        execution=ExecutionConfig(dry_run=not live, risk_fraction=0.005),
        guards=guards or GuardConfig(),
    )
    bot = Autobot(broker, config)
    return bot, broker


def kinds(events):
    return [(e.kind, e.detail) for e in events]


# ------------------------------ opening a trade ------------------------------
def test_a_breakout_after_the_range_is_traded():
    bars = range_then([(44020, 44005, 44015)])
    now = OPEN + timedelta(minutes=16, seconds=5)
    bot, broker = build(bars, now)
    events = bot.cycle(now)
    assert [e.kind for e in events] == ["opened"]
    assert len(broker.sent) == 1
    order = broker.sent[0]
    assert order.direction == BUY
    assert order.stop_loss == pytest.approx(43990.0)
    assert order.magic == MAGIC


def test_nothing_is_traded_while_the_range_is_still_forming():
    bars = range_then([])
    now = OPEN + timedelta(minutes=8)
    bot, broker = build(bars, now)
    assert bot.cycle(now) == []
    assert broker.sent == []


def test_the_bar_still_in_progress_is_not_treated_as_closed():
    """Its close does not exist yet, and using its running price trades signals
    that never appeared in any backtest."""
    bars = range_then([(44020, 44005, 44015)])
    now = OPEN + timedelta(minutes=15, seconds=30)     # the 14:45 bar is live
    bot, broker = build(bars, now)
    assert bot.cycle(now) == []
    assert broker.sent == []


def test_a_range_that_fails_its_filters_is_reported_once_and_dropped():
    bars = range_then([(44020, 44005, 44015)], high=44000.5, low=44000.0)
    now = OPEN + timedelta(minutes=16, seconds=5)
    bot, broker = build(bars, now, orb_cfg=OrbConfig())
    events = bot.cycle(now)
    assert [e.kind for e in events] == ["skipped"]
    assert "round-trip cost" in events[0].detail
    assert broker.sent == []
    # And the day is finished: a standing condition must not re-log every cycle.
    broker._now = now + timedelta(minutes=1)
    assert bot.cycle(now + timedelta(minutes=1)) == []


def test_only_one_position_per_symbol_per_session():
    bars = range_then([(44020, 44005, 44015)] * 4)
    now = OPEN + timedelta(minutes=16, seconds=5)
    bot, broker = build(bars, now)
    bot.cycle(now)
    later = now + timedelta(minutes=2)
    broker._now = later
    bot.cycle(later)
    assert len(broker.sent) == 1


def test_the_session_is_over_for_the_day_after_its_one_trade():
    """The trade closes, and the bot does not immediately take another."""
    bars = range_then([(44020, 44005, 44015)] * 6)
    now = OPEN + timedelta(minutes=16, seconds=5)
    bot, broker = build(bars, now)
    bot.cycle(now)
    broker.positions_list.clear()          # the stop filled
    later = now + timedelta(minutes=3)
    broker._now = later
    bot.cycle(later)
    assert len(broker.sent) == 1


def test_a_reversal_is_refused_once_the_session_has_broken_one_way():
    bars = range_then([(44020, 44005, 44015)] * 2 + [(43995, 43950, 43960)] * 3)
    now = OPEN + timedelta(minutes=16, seconds=5)
    bot, broker = build(bars, now, orb_cfg=OrbConfig(
        min_range_adr_fraction=0.0, max_range_adr_fraction=10.0,
        max_trades_per_session=2))
    bot.cycle(now)
    broker.positions_list.clear()
    later = OPEN + timedelta(minutes=20, seconds=5)
    broker._now = later
    bot.cycle(later)
    assert [o.direction for o in broker.sent] == [BUY]


# ------------------------------ the broker's clock ------------------------------
def test_a_server_three_hours_ahead_still_finds_the_right_opening_range():
    """The EET broker case. Uncorrected, the bot looks for the New York open in
    the middle of the London afternoon and finds nothing all year."""
    bars = range_then([(44020, 44005, 44015)])
    now = OPEN + timedelta(minutes=16, seconds=5)
    broker = FakeBroker(bars, server_offset_minutes=180)
    broker._now = now
    bot, _ = build(bars, now, broker=broker)
    bot.calibrate(real_now=now)
    assert bot.clock.server_offset_minutes == 180
    events = bot.cycle()
    assert [e.kind for e in events] == ["opened"]


def test_the_bot_takes_its_time_from_the_broker_not_the_machine():
    bars = range_then([(44020, 44005, 44015)])
    now = OPEN + timedelta(minutes=16, seconds=5)
    broker = FakeBroker(bars, server_offset_minutes=120)
    broker._now = now
    bot, _ = build(bars, now, broker=broker)
    bot.clock = BarClock(120)
    assert bot.now() == now


# ------------------------------ managing a position ------------------------------
def _with_position(direction=BUY, stop=43990.0, open_price=44016.0, **kw):
    bars = range_then([(44020, 44005, 44015)])
    now = OPEN + timedelta(minutes=30)
    bot, broker = build(bars, now, **kw)
    broker.positions_list.append(Position(
        ticket=901, symbol="US30", direction=direction, lots=1.0,
        open_price=open_price, stop_loss=stop, take_profit=0.0, opened_at=now,
        magic=MAGIC))
    bot._ranges[("US30", DAY)] = orb.build_range(bars, "US30", US, DAY, LOOSE, POINT)
    return bot, broker, now


def test_a_position_is_flat_before_the_session_closes():
    bars = range_then([(44020, 44005, 44015)])
    flat_at = US.close_utc(DAY) - timedelta(minutes=5)
    bot, broker = build(bars, flat_at)
    broker.positions_list.append(Position(
        ticket=901, symbol="US30", direction=BUY, lots=1.0, open_price=44016.0,
        stop_loss=43990.0, take_profit=0.0, opened_at=OPEN, magic=MAGIC))
    events = bot.cycle(flat_at)
    assert broker.closed and broker.closed[0][0] == 901
    assert any("close" in e.detail for e in events)


def test_the_stop_moves_to_entry_once_the_trade_is_one_r_ahead():
    bot, broker, now = _with_position()
    broker._quote = (44042.0, 44044.0)          # 1R past a 26-point risk
    events = bot.cycle(now)
    assert broker.modified == [(901, pytest.approx(44016.0))]
    assert any(e.kind == "trailed" for e in events)


def test_the_stop_does_not_move_before_one_r():
    bot, broker, now = _with_position()
    broker._quote = (44020.0, 44022.0)
    bot.cycle(now)
    assert broker.modified == []


def test_the_stop_is_only_moved_once():
    bot, broker, now = _with_position()
    broker._quote = (44042.0, 44044.0)
    bot.cycle(now)
    broker._now = now + timedelta(minutes=1)
    bot.cycle(now + timedelta(minutes=1))
    assert len(broker.modified) == 1


def test_a_position_opened_by_hand_is_left_alone():
    """Closing a trade the user placed themselves is unforgivable."""
    bars = range_then([(44020, 44005, 44015)])
    flat_at = US.close_utc(DAY) - timedelta(minutes=5)
    bot, broker = build(bars, flat_at)
    broker.positions_list.append(Position(
        ticket=7, symbol="US30", direction=BUY, lots=1.0, open_price=44000.0,
        stop_loss=0.0, take_profit=0.0, opened_at=OPEN, magic=0))
    bot.cycle(flat_at)
    assert broker.closed == []
    assert any(p.ticket == 7 for p in broker.positions_list)


# ------------------------------ the guards ------------------------------
def test_a_closed_position_is_fed_back_to_the_loss_breakers():
    """Without this the consecutive-loss halt sits at zero forever -- safety
    rails configured and not connected."""
    bot, broker, now = _with_position()
    bot.cycle(now)                              # sees the position
    broker.positions_list.clear()               # the stop filled
    broker.positions_list = []
    events = bot.cycle(now + timedelta(minutes=1))
    assert any(e.kind == "settled" for e in events)
    assert bot.guards.state.consecutive_losses == 0   # profit defaulted to 0.0


def test_a_losing_close_increments_the_consecutive_loss_count():
    bot, broker, now = _with_position()
    broker.positions_list[0].profit = -120.0
    bot.cycle(now)
    broker.positions_list = []
    bot.cycle(now + timedelta(minutes=1))
    assert bot.guards.state.consecutive_losses == 1


def test_the_brokers_realised_profit_is_preferred_when_it_can_answer():
    bot, broker, now = _with_position()
    broker.positions_list[0].profit = -5.0      # last floating value
    broker.deal_profit = lambda ticket: -250.0  # what actually settled
    bot.cycle(now)
    broker.positions_list = []
    bot.cycle(now + timedelta(minutes=1))
    assert bot.guards.state.consecutive_losses == 1
    settled = [e for e in bot.events if e.kind == "settled"]
    assert "-250.00" in settled[-1].detail


def test_a_halted_day_blocks_entries_and_says_so_once():
    bars = range_then([(44020, 44005, 44015)] * 5)
    now = OPEN + timedelta(minutes=16, seconds=5)
    bot, broker = build(bars, now)
    bot.guards.start_day(now, 50_000.0)
    bot.guards.halt("testing")
    events = bot.cycle(now)
    assert [e.kind for e in events] == ["blocked"]
    broker._now = now + timedelta(minutes=1)
    assert bot.cycle(now + timedelta(minutes=1)) == []
    assert broker.sent == []


def test_the_weekend_is_not_traded():
    saturday = datetime(2026, 3, 7, 15, 0, tzinfo=timezone.utc)
    bars = range_then([(44020, 44005, 44015)])
    bot, broker = build(bars, saturday)
    bot.cycle(saturday)
    assert broker.sent == []


# ------------------------------ safety ------------------------------
def test_dry_run_is_the_default_and_sends_nothing():
    bars = range_then([(44020, 44005, 44015)])
    now = OPEN + timedelta(minutes=16, seconds=5)
    bot, broker = build(bars, now, live=False)
    events = bot.cycle(now)
    assert [e.kind for e in events] == ["opened"]
    assert broker.sent == [], "a dry run reached the broker"
    assert "dry run" in events[0].detail


def test_a_contradictory_config_will_not_start():
    with pytest.raises(ValueError, match="inverted"):
        Autobot(FakeBroker(range_then([])), AutobotConfig(
            symbols=["US30"],
            orb=OrbConfig(min_range_adr_fraction=0.9, max_range_adr_fraction=0.1)))


def test_no_symbols_will_not_start():
    with pytest.raises(ValueError, match="nothing to trade"):
        Autobot(FakeBroker(range_then([])), AutobotConfig())


def test_a_surprising_but_legal_risk_setting_warns_instead_of_refusing():
    config = AutobotConfig(
        symbols=["US30"],
        execution=ExecutionConfig(dry_run=False, risk_fraction=0.05))
    assert config.validate() == []
    assert any("risking 5.0%" in w for w in config.warnings())
    bot = Autobot(FakeBroker(range_then([])), config)
    assert any(e.kind == "warning" for e in bot.events)


def test_the_missing_spread_gate_is_warned_about():
    config = AutobotConfig(symbols=["US30"],
                           execution=ExecutionConfig(dry_run=False))
    assert any("max_spread_points" in w for w in config.warnings())


def test_hardcoded_guard_hours_are_warned_about_as_dst_unsafe():
    config = AutobotConfig(
        symbols=["US30"],
        guards=GuardConfig(trading_hours_utc={"US30": (13, 20)}))
    assert any("daylight-saving" in w for w in config.warnings())


def test_an_unreadable_account_stops_the_cycle_rather_than_trading_blind():
    bars = range_then([(44020, 44005, 44015)])
    now = OPEN + timedelta(minutes=16, seconds=5)
    bot, broker = build(bars, now)

    def boom():
        raise RuntimeError("not logged in")

    broker.account = boom
    events = bot.cycle(now)
    assert [e.kind for e in events] == ["error"]
    assert broker.sent == []


def test_a_broker_error_on_one_symbol_does_not_stop_the_others():
    bars = range_then([(44020, 44005, 44015)])
    now = OPEN + timedelta(minutes=16, seconds=5)
    bot, broker = build(bars, now)
    bot.config.symbols = ["US30", "US30"]

    calls = {"n": 0}
    real = broker.history

    def flaky(symbol, timeframe, start, end):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("timeout")
        return real(symbol, timeframe, start, end)

    broker.history = flaky
    events = bot.cycle(now)
    assert [e.kind for e in events] == ["error", "opened"]


def test_the_event_trace_is_bounded():
    bars = range_then([])
    bot, _ = build(bars, OPEN)
    for i in range(2500):
        bot._record(OPEN, "US30", "skipped", f"reason {i}")
    assert len(bot.events) <= 2000
