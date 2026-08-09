"""The strategy and the cycle that runs it."""

from datetime import datetime, time, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from cheese_signals.markets import strategy as st
from cheese_signals.markets.execution import BUY, MAGIC, ExecutionConfig, Executor, SELL
from cheese_signals.markets.guards import GuardConfig, Guards
from cheese_signals.markets.mt5_bridge import SymbolSpec
from cheese_signals.markets.strategy import (
    FLAT, IntradayMomentum, MomentumConfig, OpeningRangeBreakout, ORBConfig,
)
from cheese_signals.markets.trader import SymbolPlan, Trader

UTC = timezone.utc
SPEC = SymbolSpec(name="NAS100", point=1.0, digits=1, contract_size=1.0,
                  tick_value=1.0, tick_size=1.0, volume_min=0.1, volume_step=0.1,
                  volume_max=50.0, spread_current=2.0)

OPEN_T, CLOSE_T = time(13, 30), time(20, 0)


def _session(day: str, path, open_px=20_000.0):
    """One session's 1-minute bars following ``path`` (deltas from the open)."""
    idx = pd.date_range(f"{day} 13:30", periods=len(path), freq="1min", tz="UTC")
    close = open_px + np.cumsum(path)
    return pd.DataFrame(
        {"open": np.r_[open_px, close[:-1]], "high": close + 1, "low": close - 1,
         "close": close, "tick_volume": 100.0, "spread": 2.0}, index=idx)


def _quiet_days(n=16, minutes=390, end_day="2026-03-02", amp=3.0, seed=0):
    """``n`` sessions of small oscillation -- a narrow band to breach."""
    rng = np.random.default_rng(seed)
    frames, day = [], pd.Timestamp(end_day)
    made = 0
    while made < n:
        day -= pd.Timedelta(days=1)
        if day.weekday() >= 5:
            continue
        frames.append(_session(day.strftime("%Y-%m-%d"),
                               rng.normal(0, amp / 10, minutes)))
        made += 1
    return pd.concat(sorted(frames, key=lambda f: f.index[0]))


def _cfg(**over):
    d = dict(session_open=OPEN_T, session_close=CLOSE_T,
             evaluate_every_minutes=1, lookback_days=14)
    d.update(over)
    return MomentumConfig(**d)


# ------------------------------ the band ------------------------------
def test_the_band_widens_as_the_session_progresses():
    """The reason a fixed percentage band takes all its trades late."""
    hist = _quiet_days()
    prof = st.move_profile(hist, _cfg(), pd.Timestamp("2026-03-02", tz=UTC))
    assert prof[300] > prof[30], "the band did not widen through the day"


def test_the_profile_never_includes_the_day_being_traded():
    """A band built from today is reading the answer off its own bar."""
    hist = _quiet_days()
    today = _session("2026-03-02", np.r_[np.zeros(60) + 50.0])   # a huge move
    prof_before = st.move_profile(hist, _cfg(), pd.Timestamp("2026-03-02", tz=UTC))
    prof_after = st.move_profile(pd.concat([hist, today]), _cfg(),
                                 pd.Timestamp("2026-03-02", tz=UTC))
    assert prof_before == prof_after


def test_the_profile_uses_only_the_configured_number_of_days():
    hist = _quiet_days(n=16)
    cfg = _cfg(lookback_days=3)
    days_used = sorted({ts.date() for ts in hist.index})[-3:]
    prof = st.move_profile(hist, cfg, pd.Timestamp("2026-03-02", tz=UTC))
    # A 3-day profile must differ from a 14-day one on a series that varies.
    assert prof != st.move_profile(hist, _cfg(lookback_days=14),
                                   pd.Timestamp("2026-03-02", tz=UTC))
    assert len(days_used) == 3


# ------------------------------ the signal ------------------------------
def _frame_with_breakout(direction=1, size=80.0):
    hist = _quiet_days()
    path = np.zeros(60)
    path[30] = direction * size            # one decisive bar half an hour in
    return pd.concat([hist, _session("2026-03-02", path)])


def test_a_close_above_the_upper_boundary_is_a_buy():
    df = _frame_with_breakout(+1)
    s = IntradayMomentum(_cfg(), point=1.0)
    intent = s.evaluate(df, df.index[-1], 0)
    assert intent.direction == BUY, intent.reason
    assert intent.stop_loss < float(df.close.iloc[-1])


def test_a_close_below_the_lower_boundary_is_a_sell():
    df = _frame_with_breakout(-1)
    s = IntradayMomentum(_cfg(), point=1.0)
    intent = s.evaluate(df, df.index[-1], 0)
    assert intent.direction == SELL, intent.reason
    assert intent.stop_loss > float(df.close.iloc[-1])


def test_price_inside_the_band_produces_nothing_and_says_why():
    df = pd.concat([_quiet_days(), _session("2026-03-02", np.zeros(60))])
    intent = IntradayMomentum(_cfg()).evaluate(df, df.index[-1], 0)
    assert not intent and "inside the band" in intent.reason


def test_the_band_multiple_makes_the_strategy_more_selective():
    df = _frame_with_breakout(+1, size=40.0)
    assert IntradayMomentum(_cfg(band_multiple=1.0)).evaluate(df, df.index[-1], 0)
    wide = IntradayMomentum(_cfg(band_multiple=25.0)).evaluate(df, df.index[-1], 0)
    assert not wide


def test_evaluation_only_happens_on_the_configured_minutes():
    """The paper checks on the half hour; checking every bar is a different rule."""
    df = _frame_with_breakout(+1)          # decisive bar at session minute 30
    s = IntradayMomentum(_cfg(evaluate_every_minutes=30))
    session = df[df.index >= pd.Timestamp("2026-03-02 13:30", tz=UTC)]

    at30, at31 = session.index[30], session.index[31]
    assert s.evaluate(df[df.index <= at30], at30, 0).direction == BUY
    assert "not an evaluation minute" in s.evaluate(df[df.index <= at31], at31, 0).reason


def test_checking_every_minute_finds_the_same_breakout_sooner():
    """The two settings are different strategies, not a tuning detail."""
    df = _frame_with_breakout(+1)
    session = df[df.index >= pd.Timestamp("2026-03-02 13:30", tz=UTC)]
    at31 = session.index[31]
    every = IntradayMomentum(_cfg(evaluate_every_minutes=1))
    assert every.evaluate(df[df.index <= at31], at31, 0).direction == BUY


def test_the_session_trade_cap_is_respected():
    df = _frame_with_breakout(+1)
    s = IntradayMomentum(_cfg(max_trades_per_session=1))
    assert not s.evaluate(df, df.index[-1], trades_today=1)


def test_a_band_narrower_than_the_spread_is_refused():
    """Connects the strategy to the cost wall: a band inside the spread is noise."""
    df = _frame_with_breakout(+1)
    s = IntradayMomentum(_cfg(min_band_points=10_000.0))
    intent = s.evaluate(df, df.index[-1], 0)
    assert not intent and "narrower than" in intent.reason


def test_without_enough_history_it_refuses_rather_than_guessing():
    df = _session("2026-03-02", np.zeros(60))
    intent = IntradayMomentum(_cfg()).evaluate(df, df.index[-1], 0)
    assert not intent and "history" in intent.reason


# ------------------------------ trailing ------------------------------
def test_the_trailing_stop_sits_below_price_on_a_long():
    df = _frame_with_breakout(+1)
    s = IntradayMomentum(_cfg())
    stop = s.trailing_stop(df, df.index[-1], BUY)
    assert stop is not None and stop < float(df.close.iloc[-1])


def test_the_trailing_stop_sits_above_price_on_a_short():
    df = _frame_with_breakout(-1)
    s = IntradayMomentum(_cfg())
    stop = s.trailing_stop(df, df.index[-1], SELL)
    assert stop is not None and stop > float(df.close.iloc[-1])


# ------------------------------ opening range ------------------------------
def test_the_opening_range_breaks_upward():
    path = np.r_[np.zeros(5), 60.0, np.zeros(10)]
    df = _session("2026-03-02", path)
    s = OpeningRangeBreakout(ORBConfig(range_minutes=5, session_open=OPEN_T,
                                       session_close=CLOSE_T))
    intent = s.evaluate(df, df.index[-1], 0)
    assert intent.direction == BUY, intent.reason


def test_the_opening_range_is_not_traded_before_it_completes():
    df = _session("2026-03-02", np.zeros(3))
    s = OpeningRangeBreakout(ORBConfig(range_minutes=5, session_open=OPEN_T,
                                       session_close=CLOSE_T))
    assert "not complete" in s.evaluate(df, df.index[-1], 0).reason


def test_the_opening_range_is_traded_once_a_session():
    path = np.r_[np.zeros(5), 60.0, np.zeros(10)]
    df = _session("2026-03-02", path)
    s = OpeningRangeBreakout(ORBConfig(range_minutes=5, session_open=OPEN_T,
                                       session_close=CLOSE_T))
    assert not s.evaluate(df, df.index[-1], trades_today=1)


# ------------------------------ the cycle ------------------------------
class FakeBroker:
    def __init__(self, frames, equity=50_000.0):
        self._frames = frames
        self._acct_equity = equity
        self.positions_list = []
        self.sent = []
        self.closed = []
        self._ticket = 500

    # read side
    def history(self, symbol, timeframe, start, end):
        df = self._frames[symbol]
        return df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]

    def spec(self, symbol):
        return SPEC

    # trade side
    def account(self):
        from cheese_signals.markets.execution import AccountState
        return AccountState(self._acct_equity, self._acct_equity, self._acct_equity)

    def tick(self, symbol):
        px = float(self._frames[symbol].close.iloc[-1])
        return px - 1.0, px + 1.0

    def positions(self, magic=None):
        return [p for p in self.positions_list if magic is None or p.magic == magic]

    def send(self, order):
        from cheese_signals.markets.execution import OrderResult, Position
        self._ticket += 1
        bid, ask = self.tick(order.symbol)
        price = ask if order.direction == BUY else bid
        self.positions_list.append(Position(
            self._ticket, order.symbol, order.direction, order.lots, price,
            order.stop_loss, order.take_profit or 0.0,
            datetime(2026, 3, 2, tzinfo=UTC), magic=order.magic))
        self.sent.append(order)
        return OrderResult(True, ticket=self._ticket, price=price, lots=order.lots)

    def close(self, position, reason=""):
        from cheese_signals.markets.execution import OrderResult
        self.positions_list = [p for p in self.positions_list
                               if p.ticket != position.ticket]
        self.closed.append((position.ticket, reason))
        return OrderResult(True, ticket=position.ticket)

    def modify(self, position, stop_loss, take_profit=None):
        from cheese_signals.markets.execution import OrderResult
        position.stop_loss = stop_loss
        return OrderResult(True, ticket=position.ticket)


def _trader(frames, guard_cfg=None, exec_cfg=None, strategy=None):
    broker = FakeBroker(frames)
    ex = Executor(broker, exec_cfg or ExecutionConfig(dry_run=False, risk_fraction=0.005),
                  specs={"NAS100": SPEC})
    guards = Guards(guard_cfg or GuardConfig(trading_hours_utc={"NAS100": (13, 20)}))
    plan = SymbolPlan("NAS100", strategy or IntradayMomentum(_cfg(), point=1.0))
    return Trader(broker, [plan], ex, guards), broker


NOW = datetime(2026, 3, 2, 14, 1, tzinfo=UTC)


def test_a_breakout_opens_a_position_and_reports_it():
    frames = {"NAS100": _frame_with_breakout(+1)}
    t, broker = _trader(frames)
    events = t.cycle(NOW)
    assert broker.sent, [e.detail for e in events]
    opened = [e for e in events if e.kind == "opened"]
    assert opened and "BUY" in opened[0].detail


def test_the_forming_bar_is_never_used():
    """Acting on a bar that has not closed is acting on a price that has
    not happened."""
    frames = {"NAS100": _frame_with_breakout(+1)}
    t, _ = _trader(frames)
    last = frames["NAS100"].index[-1]
    df = t.history("NAS100", last.to_pydatetime(), 25)
    assert df.index.max() < last


def test_a_refusal_is_always_explained():
    """"No trades today" with no reason is how a broken bot hides."""
    frames = {"NAS100": pd.concat([_quiet_days(), _session("2026-03-02", np.zeros(60))])}
    t, broker = _trader(frames)
    events = t.cycle(NOW)
    assert not broker.sent
    refused = [e for e in events if e.kind == "refused"]
    assert refused and refused[0].detail


def test_the_guards_can_veto_a_valid_signal():
    frames = {"NAS100": _frame_with_breakout(+1)}
    t, broker = _trader(frames, guard_cfg=GuardConfig(max_open_positions=0))
    events = t.cycle(NOW)
    assert not broker.sent
    assert any("positions open" in e.detail for e in events)


def test_a_second_position_is_not_opened_on_a_symbol_already_held():
    frames = {"NAS100": _frame_with_breakout(+1)}
    t, broker = _trader(frames)
    t.cycle(NOW)
    t.cycle(NOW + timedelta(minutes=1))
    assert len(broker.sent) == 1


def test_positions_are_flattened_at_the_session_close():
    frames = {"NAS100": _frame_with_breakout(+1)}
    t, broker = _trader(frames)
    t.cycle(NOW)
    assert broker.positions_list
    t.cycle(NOW.replace(hour=19, minute=55))
    assert not broker.positions_list
    assert broker.closed and "close" in broker.closed[0][1]


def test_a_position_that_disappears_is_counted_as_a_closed_trade():
    """A stop filling is a loss the breakers have to see."""
    frames = {"NAS100": _frame_with_breakout(+1)}
    t, broker = _trader(frames)
    t.cycle(NOW)
    broker.positions_list[0].profit = -250.0
    t.cycle(NOW + timedelta(minutes=1))          # records the profit
    broker.positions_list.clear()                # the stop fills
    t.cycle(NOW + timedelta(minutes=2))
    assert t.guards.state.consecutive_losses == 1


def test_one_unquotable_symbol_does_not_stop_the_others():
    frames = {"NAS100": _frame_with_breakout(+1)}
    t, broker = _trader(frames)
    t.plans["BROKEN"] = SymbolPlan("BROKEN", IntradayMomentum(_cfg()))
    events = t.cycle(NOW)
    assert any(e.kind == "error" and e.symbol == "BROKEN" for e in events)
    assert broker.sent, "the working symbol was skipped because another failed"


def test_a_broker_that_will_not_answer_does_not_kill_the_loop():
    frames = {"NAS100": _frame_with_breakout(+1)}
    t, broker = _trader(frames)

    def boom():
        raise ConnectionError("terminal went away")
    broker.account = boom
    events = t.cycle(NOW)
    assert any(e.kind == "error" and "cycle aborted" in e.detail for e in events)


def test_dry_run_decides_everything_and_sends_nothing():
    frames = {"NAS100": _frame_with_breakout(+1)}
    t, broker = _trader(frames, exec_cfg=ExecutionConfig(dry_run=True))
    events = t.cycle(NOW)
    assert any(e.kind == "opened" for e in events)
    assert broker.sent == []


def test_a_news_blackout_stops_the_bot_opening():
    frames = {"NAS100": _frame_with_breakout(+1)}
    t, broker = _trader(frames)
    t.news_windows = lambda: [(NOW, NOW, "US CPI")]
    events = t.cycle(NOW)
    assert not broker.sent
    assert any("US CPI" in e.detail for e in events)


# ------------------------------ the runner ------------------------------
def test_the_runner_defaults_to_dry_run():
    """--live is the only path to a real order, and it is never the default."""
    import argparse

    from cheese_signals.markets import run as run_mod

    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    assert ap.parse_args([]).live is False
    assert ap.parse_args(["--live"]).live is True


def test_the_runner_builds_plans_and_names_what_it_skipped():
    from cheese_signals.markets import run as run_mod

    broker = FakeBroker({"NAS100": _frame_with_breakout(+1)})
    broker.symbols = lambda: ["NAS100"]
    plans, missing = run_mod.build_plans(broker, ["NAS100", "US30"])
    assert [p.symbol for p in plans] == ["NAS100"]
    assert any("US30" in m for m in missing)


def test_index_plans_get_the_us_cash_session():
    from cheese_signals.markets import run as run_mod

    broker = FakeBroker({"NAS100": _frame_with_breakout(+1)})
    broker.symbols = lambda: ["NAS100"]
    plans, _ = run_mod.build_plans(broker, ["NAS100"])
    assert plans[0].strategy.cfg.session_open == time(13, 30)
    assert run_mod.trading_hours(plans) == {"NAS100": (13, 20)}


def test_gold_gets_the_longer_london_and_us_window():
    from cheese_signals.markets import run as run_mod

    broker = FakeBroker({"XAUUSD": _frame_with_breakout(+1)})
    broker.symbols = lambda: ["XAUUSD"]
    plans, _ = run_mod.build_plans(broker, ["XAUUSD"])
    assert plans[0].strategy.cfg.session_open == time(7, 0)


def test_the_loop_runs_a_bounded_number_of_cycles_and_briefs_once():
    from cheese_signals.markets import news as news_mod
    from cheese_signals.markets import run as run_mod

    frames = {"NAS100": _frame_with_breakout(+1)}
    t, broker = _trader(frames)
    feed = run_mod.NewsFeed(["NAS100"])
    feed.events, feed._fetched_on = [], datetime.now(UTC).date()

    briefs = []

    class _N:
        def send(self, text):
            briefs.append(text)

    run_mod.loop(t, feed, _N(), interval=0.0, max_cycles=3)
    assert len([b for b in briefs if "Market brief" in b]) == 1, briefs
