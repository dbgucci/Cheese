"""Order placement against a broker that behaves like a real one.

The fake here rejects the things a live broker rejects -- off-grid volumes,
stops inside the minimum distance, positions it does not recognise -- because
a fake that accepts everything tests nothing that matters.
"""

from datetime import datetime, timezone

import pytest

from cheese_signals.markets import execution as ex
from cheese_signals.markets.execution import BUY, MAGIC, SELL
from cheese_signals.markets.mt5_bridge import SymbolSpec

NOW = datetime(2026, 3, 2, 14, 0, tzinfo=timezone.utc)

# US30-like: point 0.1, a tick is 10 points, $1 per tick per lot.
US30 = SymbolSpec(name="US30", point=0.1, digits=1, contract_size=1.0,
                  tick_value=1.0, tick_size=1.0, volume_min=0.1, volume_step=0.1,
                  volume_max=50.0, spread_current=40.0, stops_level=50)
# EURUSD-like: point 0.00001, tick == point, $1 per point per lot.
EURUSD = SymbolSpec(name="EURUSD", point=0.00001, digits=5, contract_size=100_000,
                    tick_value=1.0, tick_size=0.00001, volume_min=0.01,
                    volume_step=0.01, volume_max=100.0, spread_current=8.0)


class FakeTrader:
    def __init__(self, equity=10_000.0, quotes=None, specs=None):
        self._acct = ex.AccountState(balance=equity, equity=equity, margin_free=equity)
        self._quotes = quotes or {"US30": (44000.0, 44004.0),
                                  "EURUSD": (1.09000, 1.09008)}
        self._specs = specs or {"US30": US30, "EURUSD": EURUSD}
        self._positions: list[ex.Position] = []
        self.sent: list[ex.Order] = []
        self._ticket = 1000

    def account(self):
        return self._acct

    def spec(self, symbol):
        return self._specs[symbol]

    def tick(self, symbol):
        return self._quotes[symbol]

    def positions(self, magic=None):
        return [p for p in self._positions if magic is None or p.magic == magic]

    def send(self, order):
        spec = self._specs[order.symbol]
        # A real broker refuses an off-grid volume.
        steps = order.lots / spec.volume_step
        if abs(steps - round(steps)) > 1e-6:
            return ex.OrderResult(False, "Invalid volume", retcode=10014)
        bid, ask = self._quotes[order.symbol]
        price = ask if order.direction == BUY else bid
        # ...and a stop inside the minimum distance.
        if spec.stops_level and abs(price - order.stop_loss) < spec.stops_level * spec.point - 1e-9:
            return ex.OrderResult(False, "Invalid stops", retcode=10016)
        self._ticket += 1
        self._positions.append(ex.Position(
            ticket=self._ticket, symbol=order.symbol, direction=order.direction,
            lots=order.lots, open_price=price, stop_loss=order.stop_loss,
            take_profit=order.take_profit or 0.0, opened_at=NOW, magic=order.magic))
        self.sent.append(order)
        return ex.OrderResult(True, ticket=self._ticket, price=price, lots=order.lots)

    def close(self, position, reason=""):
        self._positions = [p for p in self._positions if p.ticket != position.ticket]
        return ex.OrderResult(True, reason, ticket=position.ticket)

    def modify(self, position, stop_loss, take_profit=None):
        position.stop_loss = stop_loss
        return ex.OrderResult(True, ticket=position.ticket)


def _exec(trader=None, **cfg):
    trader = trader or FakeTrader()
    config = ex.ExecutionConfig(dry_run=False, **cfg)
    return ex.Executor(trader, config, specs=dict(trader._specs)), trader


# ------------------------------ volume normalisation ------------------------------
def test_volume_rounds_down_never_up():
    """Rounding up would place more risk than the policy allows."""
    assert ex.normalise_volume(0.1799, EURUSD) == 0.17
    assert ex.normalise_volume(0.999, US30) == 0.9


def test_below_the_minimum_is_zero_not_the_minimum():
    """Trading the minimum anyway is how a 1% risk becomes an 8% risk."""
    assert ex.normalise_volume(0.004, EURUSD) == 0.0
    assert ex.normalise_volume(0.05, US30) == 0.0


def test_volume_is_capped_at_the_broker_maximum():
    assert ex.normalise_volume(500.0, US30) == 50.0


def test_the_step_grid_is_respected_exactly():
    """0.30000000000000004 is rejected by a real broker as an invalid volume."""
    v = ex.normalise_volume(0.3, EURUSD)
    assert v == 0.3
    steps = v / EURUSD.volume_step
    assert abs(steps - round(steps)) < 1e-9


# ------------------------------ risk-based sizing ------------------------------
def test_a_wider_stop_gets_a_smaller_position():
    near = ex.size_for_risk(10_000, 0.01, 1.09000, 1.08900, EURUSD)   # 100 pts
    far = ex.size_for_risk(10_000, 0.01, 1.09000, 1.08800, EURUSD)    # 200 pts
    assert far == pytest.approx(near / 2, rel=0.02)


def test_the_money_at_risk_matches_the_budget():
    equity, risk = 10_000.0, 0.01
    entry, stop = 1.09000, 1.08900
    lots = ex.size_for_risk(equity, risk, entry, stop, EURUSD)
    points = abs(entry - stop) / EURUSD.point
    loss = points * EURUSD.money_per_point(lots)
    assert loss == pytest.approx(equity * risk, rel=0.02)


def test_a_tick_worth_ten_points_is_not_sized_ten_times_too_large():
    """The error a live account does not survive twice."""
    assert US30.money_per_point(1.0) == pytest.approx(0.1)
    assert EURUSD.money_per_point(1.0) == pytest.approx(1.0)
    lots = ex.size_for_risk(10_000, 0.01, 44000.0, 43900.0, US30)   # 1000 points
    loss = 1000 * US30.money_per_point(lots)
    assert loss == pytest.approx(100.0, rel=0.05)


def test_a_zero_distance_stop_sizes_nothing_rather_than_infinity():
    assert ex.size_for_risk(10_000, 0.01, 1.09, 1.09, EURUSD) == 0.0


# ------------------------------ stop distance ------------------------------
def test_a_stop_too_close_is_pushed_out_not_rejected():
    """Rejecting leaves the position open with no protection at all."""
    stop = ex.enforce_stop_distance(BUY, 44000.0, 43999.0, US30)   # 10pt, min 50
    assert stop == pytest.approx(44000.0 - 50 * US30.point)


def test_a_stop_already_far_enough_is_untouched():
    assert ex.enforce_stop_distance(BUY, 44000.0, 43900.0, US30) == 43900.0


def test_the_sell_side_pushes_the_other_way():
    stop = ex.enforce_stop_distance(SELL, 44000.0, 44001.0, US30)
    assert stop == pytest.approx(44000.0 + 50 * US30.point)


def test_the_broker_accepts_the_stop_the_executor_produced():
    """End to end against a fake that rejects what a real broker rejects."""
    e, trader = _exec()
    r = e.open("US30", BUY, stop_loss=43999.9)      # far too close
    assert r.ok, r.reason
    assert trader.sent[0].stop_loss <= 44004.0 - 50 * US30.point


# ------------------------------ the spread gate ------------------------------
def test_a_spread_wider_than_the_strategy_was_tested_at_is_refused():
    """The gate that connects the cost wall to live trading."""
    trader = FakeTrader(quotes={"US30": (44000.0, 44020.0)})   # 200 points
    e, _ = _exec(trader, max_spread_points={"US30": 60.0})
    r = e.open("US30", BUY, stop_loss=43900.0)
    assert not r.ok and "exceeds" in r.reason
    assert trader.sent == []


def test_a_normal_spread_passes_the_gate():
    e, trader = _exec(max_spread_points={"US30": 60.0})
    assert e.open("US30", BUY, stop_loss=43900.0).ok
    assert len(trader.sent) == 1


def test_no_limit_configured_means_no_gate():
    trader = FakeTrader(quotes={"US30": (44000.0, 44100.0)})
    e, _ = _exec(trader)
    assert e.open("US30", BUY, stop_loss=43900.0).ok


# ------------------------------ duplicate protection ------------------------------
def test_a_second_position_on_the_same_symbol_is_refused():
    """The restart bug: the bot forgets, and doubles its exposure."""
    e, trader = _exec()
    assert e.open("US30", BUY, stop_loss=43900.0).ok
    second = e.open("US30", BUY, stop_loss=43900.0)
    assert not second.ok and "already holding" in second.reason
    assert len(trader.sent) == 1


def test_a_position_opened_by_hand_is_neither_counted_nor_closed():
    """Closing a position the user opened themselves is unforgivable."""
    e, trader = _exec()
    trader._positions.append(ex.Position(
        ticket=1, symbol="US30", direction=BUY, lots=1.0, open_price=44000.0,
        stop_loss=0.0, take_profit=0.0, opened_at=NOW, magic=0))
    assert e.open("US30", BUY, stop_loss=43900.0).ok, "the manual trade blocked the bot"
    e.close_all("end of day")
    assert any(p.ticket == 1 for p in trader._positions), "it closed the user's trade"


def test_adopt_reports_only_this_bots_positions():
    e, trader = _exec()
    e.open("US30", BUY, stop_loss=43900.0)
    trader._positions.append(ex.Position(
        ticket=7, symbol="EURUSD", direction=BUY, lots=1.0, open_price=1.09,
        stop_loss=0.0, take_profit=0.0, opened_at=NOW, magic=0))
    assert [p.magic for p in e.adopt()] == [MAGIC]


# ------------------------------ sanity refusals ------------------------------
def test_a_stop_on_the_wrong_side_is_refused():
    e, trader = _exec()
    r = e.open("US30", BUY, stop_loss=44100.0)
    assert not r.ok and "wrong side" in r.reason
    assert trader.sent == []


def test_an_account_too_small_for_the_minimum_lot_says_so():
    e, trader = _exec(FakeTrader(equity=50.0))
    r = e.open("US30", BUY, stop_loss=43900.0)
    assert not r.ok and "minimum" in r.reason


def test_an_untradeable_symbol_is_refused():
    spec = SymbolSpec(**{**US30.__dict__, "trade_allowed": False})
    trader = FakeTrader(specs={"US30": spec})
    e, _ = _exec(trader)
    assert not e.open("US30", BUY, stop_loss=43900.0).ok


def test_dry_run_never_reaches_the_broker():
    trader = FakeTrader()
    e = ex.Executor(trader, ex.ExecutionConfig(dry_run=True), specs=dict(trader._specs))
    r = e.open("US30", BUY, stop_loss=43900.0)
    assert r.ok and "dry run" in r.reason
    assert trader.sent == [] and trader.positions() == []


def test_dry_run_is_the_default():
    assert ex.ExecutionConfig().dry_run is True


# ------------------------------ trailing stops ------------------------------
def test_a_trailing_stop_moves_in_the_protective_direction():
    e, trader = _exec()
    e.open("US30", BUY, stop_loss=43900.0)
    pos = e.adopt()[0]
    assert e.trail(pos, 43950.0).ok
    assert trader.positions()[0].stop_loss == 43950.0


def test_a_trailing_stop_refuses_to_loosen():
    """A bug here turns a bounded loss into an unbounded one."""
    e, trader = _exec()
    e.open("US30", BUY, stop_loss=43900.0)
    pos = e.adopt()[0]
    r = e.trail(pos, 43800.0)
    assert not r.ok and "may not move down" in r.reason
    assert trader.positions()[0].stop_loss == 43900.0


def test_a_short_trailing_stop_refuses_to_move_up():
    e, trader = _exec()
    e.open("US30", SELL, stop_loss=44100.0)
    pos = e.adopt()[0]
    assert not e.trail(pos, 44200.0).ok
    assert e.trail(pos, 44050.0).ok


# ------------------------------ closing ------------------------------
def test_close_all_closes_only_this_bots_positions_and_reports_each():
    e, trader = _exec()
    e.open("US30", BUY, stop_loss=43900.0)
    e.open("EURUSD", BUY, stop_loss=1.08900)
    results = e.close_all("session close")
    assert len(results) == 2 and all(r.ok for r in results)
    assert trader.positions(MAGIC) == []


def test_close_all_can_be_limited_to_one_symbol():
    e, trader = _exec()
    e.open("US30", BUY, stop_loss=43900.0)
    e.open("EURUSD", BUY, stop_loss=1.08900)
    e.close_all("news", symbol="US30")
    assert [p.symbol for p in trader.positions(MAGIC)] == ["EURUSD"]
