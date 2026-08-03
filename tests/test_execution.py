"""Autotrading: the safety gate, paper fills, and live order handling.

The gate is the only thing standing between a strategy bug and a drained
account, so it is tested for what it *refuses*, not just what it allows.
"""

from datetime import datetime, timedelta, timezone

import pytest

from cheese_signals import execution
from cheese_signals.execution import (
    MODE_LIVE, MODE_OFF, MODE_PAPER, LiveExecutor, PaperExecutor,
    SafetyConfig, SafetyGate,
)


def _gate(**kw):
    return SafetyGate(SafetyConfig(**kw))


# -------------------------------- defaults --------------------------------
def test_execution_is_off_by_default():
    from cheese_signals.settings import Settings
    s = Settings()
    assert s.trade_mode == MODE_OFF
    assert s.live_confirmed is False


def test_gate_refuses_when_off():
    ok, why = _gate().check(MODE_OFF, stake=10, balance=1000)
    assert not ok and "off" in why


def test_live_requires_explicit_confirmation():
    ok, why = _gate(live_confirmed=False).check(MODE_LIVE, stake=10, balance=1000)
    assert not ok and "not been confirmed" in why

    ok, _ = _gate(live_confirmed=True).check(MODE_LIVE, stake=10, balance=1000)
    assert ok


def test_paper_does_not_need_live_confirmation():
    ok, _ = _gate(live_confirmed=False).check(MODE_PAPER, stake=10, balance=1000)
    assert ok


def test_unknown_mode_is_refused():
    ok, why = _gate().check("yolo", stake=10, balance=1000)
    assert not ok and "unknown" in why


# ------------------------------- hard caps --------------------------------
def test_stake_cap():
    ok, why = _gate(max_stake=25).check(MODE_PAPER, stake=26, balance=1000)
    assert not ok and "cap" in why


def test_balance_floor():
    ok, why = _gate(min_balance=100).check(MODE_PAPER, stake=10, balance=99)
    assert not ok and "floor" in why


def test_stake_cannot_exceed_balance():
    # Raise the per-trade cap so this isolates the balance rule rather than
    # tripping the stake cap first.
    ok, why = _gate(min_balance=0, max_stake=1000).check(MODE_PAPER, stake=500, balance=100)
    assert not ok and "exceeds the available balance" in why


def test_non_positive_stake_refused():
    for bad in (0, -5):
        ok, why = _gate().check(MODE_PAPER, stake=bad, balance=1000)
        assert not ok and "positive" in why


def test_concurrent_trade_cap():
    g = _gate(max_concurrent=2)
    for _ in range(2):
        assert g.check(MODE_PAPER, 10, 1000)[0]
        g.register_open()
    ok, why = g.check(MODE_PAPER, 10, 1000)
    assert not ok and "already open" in why

    g.register_close(5.0)
    assert g.check(MODE_PAPER, 10, 1000)[0], "closing a trade should free a slot"


def test_hourly_cap_and_rollover():
    g = _gate(max_trades_per_hour=2, max_concurrent=99)
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    for _ in range(2):
        assert g.check(MODE_PAPER, 10, 1000, now)[0]
        g.register_open()
        g.register_close(1.0)
    assert not g.check(MODE_PAPER, 10, 1000, now)[0]

    later = now + timedelta(hours=1, seconds=1)
    assert g.check(MODE_PAPER, 10, 1000, later)[0], "the hourly window should roll over"


def test_daily_loss_limit_halts_trading():
    g = _gate(max_daily_loss=30)
    for _ in range(3):
        g.register_open()
        g.register_close(-10.0)
    assert g.state.halted
    ok, why = g.check(MODE_PAPER, 10, 1000)
    assert not ok and "halted" in why


def test_daily_pnl_resets_on_a_new_day():
    g = _gate(max_daily_loss=30)
    day1 = datetime(2026, 8, 3, 23, 0, tzinfo=timezone.utc)
    g.check(MODE_PAPER, 10, 1000, day1)
    g.register_open(); g.register_close(-25.0)
    assert g.check(MODE_PAPER, 10, 1000, day1)[0]

    day2 = day1 + timedelta(hours=2)
    ok, _ = g.check(MODE_PAPER, 10, 1000, day2)
    assert ok and g.state.realised_today == 0.0


def test_kill_switch_and_resume():
    g = _gate()
    g.halt("manual")
    ok, why = g.check(MODE_PAPER, 10, 1000)
    assert not ok and "manual" in why
    g.resume()
    assert g.check(MODE_PAPER, 10, 1000)[0]


# ----------------------------- paper executor -----------------------------
def test_paper_balance_tracks_wins_and_losses():
    ex = PaperExecutor(starting_balance=100.0)
    order = ex.place("EURUSD_otc", 1, 10.0, 60)
    assert ex.balance() == pytest.approx(90.0), "stake is deducted at entry"

    fill = ex.settle(order, exit_price=1.1, won_locally=True, payout=0.85)
    assert fill.won and fill.profit == pytest.approx(8.5)
    assert ex.balance() == pytest.approx(108.5)

    order2 = ex.place("EURUSD_otc", -1, 10.0, 60)
    fill2 = ex.settle(order2, exit_price=1.1, won_locally=False, payout=0.85)
    assert not fill2.won and fill2.profit == pytest.approx(-10.0)
    assert ex.balance() == pytest.approx(98.5)


def test_paper_orders_are_marked_as_such():
    order = PaperExecutor().place("EURUSD_otc", 1, 10.0, 60)
    assert order.mode == MODE_PAPER
    assert order.broker_id is None


# ------------------------------ live executor -----------------------------
class _Client:
    def __init__(self, result=None, raise_on_buy=False):
        self.calls = []
        self._result = result
        self._raise = raise_on_buy

    def buy(self, asset, amount, time, check_win):
        if self._raise:
            raise RuntimeError("insufficient funds")
        self.calls.append(("buy", asset, amount, time))
        return "tid-1", {"openPrice": 1.2345}

    def sell(self, asset, amount, time, check_win):
        self.calls.append(("sell", asset, amount, time))
        return "tid-2", {}

    def check_win(self, tid):
        if self._result is None:
            raise RuntimeError("unavailable")
        return self._result

    def balance(self):
        return 777.0


def test_live_buy_and_sell_route_correctly():
    c = _Client()
    ex = LiveExecutor(c)
    up = ex.place("EURUSD_otc", 1, 10.0, 60)
    dn = ex.place("EURUSD_otc", -1, 10.0, 60)
    assert c.calls[0][0] == "buy" and c.calls[1][0] == "sell"
    assert up.broker_id == "tid-1" and up.entry_price == pytest.approx(1.2345)
    assert dn.broker_id == "tid-2"
    assert ex.balance() == 777.0


def test_broker_result_overrides_the_local_one():
    """The broker knows the real fill; our candle-derived guess does not."""
    ex = LiveExecutor(_Client(result={"profit": 9.2}))
    order = ex.place("EURUSD_otc", 1, 10.0, 60)
    fill = ex.settle(order, exit_price=1.1, won_locally=False, payout=0.85)
    assert fill.won is True and fill.profit == pytest.approx(9.2)
    assert fill.source == "broker"


def test_falls_back_to_local_result_when_broker_is_silent():
    ex = LiveExecutor(_Client(result=None))
    order = ex.place("EURUSD_otc", 1, 10.0, 60)
    fill = ex.settle(order, exit_price=1.1, won_locally=True, payout=0.85)
    assert fill.won is True and fill.source == "local"
    assert fill.profit == pytest.approx(8.5)


def test_order_failure_propagates():
    ex = LiveExecutor(_Client(raise_on_buy=True))
    with pytest.raises(RuntimeError, match="insufficient funds"):
        ex.place("EURUSD_otc", 1, 10.0, 60)


def test_build_executor_requires_a_client_for_live():
    from cheese_signals.settings import Settings
    s = Settings()
    assert execution.build_executor(MODE_OFF, s) is None
    assert isinstance(execution.build_executor(MODE_PAPER, s), PaperExecutor)
    with pytest.raises(ValueError, match="needs a connected"):
        execution.build_executor(MODE_LIVE, s, client=None)
