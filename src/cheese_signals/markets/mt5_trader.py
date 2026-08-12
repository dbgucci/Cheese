"""Placing real orders through MetaTrader 5.

Separate from ``mt5_bridge`` on purpose, and the separation is load-bearing
rather than tidiness. The signals app needs ``MT5Feed`` to read prices and must
not be able to trade; while both classes shared a module, every build that
imported the feed also shipped the order code, and "this app cannot place a
trade" was a promise rather than a property. Now the trading build imports this
module and the signals build does not, and CI can verify the difference by
grepping the finished binary.

Read and write still meet on one object at runtime -- ``MT5Trader`` subclasses
``MT5Feed`` -- because they must agree about which terminal they are talking to.
A reader attached to one terminal and a writer to another is a failure with no
error message, where the strategy prices one broker and trades another's account.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .mt5_bridge import MT5Feed


class MT5Trader(MT5Feed):                          # pragma: no cover
    """``MT5Feed`` plus the ability to place, modify and close orders.

    Read and write live on the same object because they must agree about which
    terminal they are talking to. Two connections, or a reader pointed at one
    terminal and a writer at another, is a failure mode with no error message:
    the strategy evaluates one broker's prices and trades another's account.

    Everything here returns an ``OrderResult`` carrying the broker's own
    retcode rather than raising. A rejection is information the bot has to act
    on -- and MetaTrader rejects for a dozen mundane reasons that are all
    survivable if they are read, and none of which are survivable if they are
    swallowed.
    """

    # Filling modes, tried in the order the broker is most likely to accept.
    # A wrong mode is rejected with "Unsupported filling mode", which reads
    # like a problem with the order rather than with the enum.
    def _filling_mode(self, symbol: str) -> int:
        mt5 = self._mt5
        info = mt5.symbol_info(symbol)
        allowed = int(getattr(info, "filling_mode", 0) or 0)
        # filling_mode is a bit mask of SYMBOL_FILLING_* flags; the order
        # constants are a different enum, hence the explicit pairing.
        if allowed & getattr(mt5, "SYMBOL_FILLING_FOK", 1):
            return mt5.ORDER_FILLING_FOK
        if allowed & getattr(mt5, "SYMBOL_FILLING_IOC", 2):
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def account(self):
        from .execution import AccountState

        a = self._mt5.account_info()
        if a is None:
            raise RuntimeError("no account_info: the terminal is not logged in")
        return AccountState(balance=float(a.balance), equity=float(a.equity),
                            margin_free=float(a.margin_free), currency=str(a.currency))

    def tick(self, symbol: str) -> tuple[float, float]:
        return self.quote(symbol)

    def positions(self, magic: Optional[int] = None):
        from .execution import BUY, SELL, Position

        raw = self._mt5.positions_get()
        if raw is None:
            return []
        out = []
        for p in raw:
            if magic is not None and int(p.magic) != magic:
                continue
            out.append(Position(
                ticket=int(p.ticket), symbol=str(p.symbol),
                direction=BUY if p.type == self._mt5.POSITION_TYPE_BUY else SELL,
                lots=float(p.volume), open_price=float(p.price_open),
                stop_loss=float(p.sl), take_profit=float(p.tp),
                opened_at=datetime.fromtimestamp(int(p.time), tz=timezone.utc),
                profit=float(p.profit), magic=int(p.magic), comment=str(p.comment),
            ))
        return out

    def send(self, order):
        from .execution import BUY, OrderResult

        mt5 = self._mt5
        bid, ask = self.quote(order.symbol)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": order.symbol,
            "volume": float(order.lots),
            "type": mt5.ORDER_TYPE_BUY if order.direction == BUY else mt5.ORDER_TYPE_SELL,
            "price": ask if order.direction == BUY else bid,
            "sl": float(order.stop_loss),
            "deviation": 20,
            "magic": int(order.magic),
            "comment": order.comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(order.symbol),
        }
        if order.take_profit is not None:
            request["tp"] = float(order.take_profit)
        result = mt5.order_send(request)
        if result is None:
            code, msg = mt5.last_error()
            return OrderResult(False, f"order_send returned nothing: {code} {msg}")
        ok = result.retcode == mt5.TRADE_RETCODE_DONE
        return OrderResult(
            ok=ok,
            reason="" if ok else f"{result.retcode} {result.comment}",
            ticket=int(result.order) or None,
            price=float(result.price) or None,
            lots=float(result.volume) or None,
            retcode=int(result.retcode),
        )

    def close(self, position, reason: str = ""):
        from .execution import BUY, OrderResult

        mt5 = self._mt5
        bid, ask = self.quote(position.symbol)
        # Closing is an opposite-side deal against this specific ticket.
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": float(position.lots),
            "type": mt5.ORDER_TYPE_SELL if position.direction == BUY else mt5.ORDER_TYPE_BUY,
            "position": int(position.ticket),
            "price": bid if position.direction == BUY else ask,
            "deviation": 20,
            "magic": int(position.magic),
            "comment": reason[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(position.symbol),
        }
        result = mt5.order_send(request)
        if result is None:
            code, msg = mt5.last_error()
            return OrderResult(False, f"close returned nothing: {code} {msg}")
        ok = result.retcode == mt5.TRADE_RETCODE_DONE
        return OrderResult(ok, "" if ok else f"{result.retcode} {result.comment}",
                           ticket=int(position.ticket), retcode=int(result.retcode))

    def deal_profit(self, ticket: int) -> Optional[float]:
        """Realised profit for a position that has already closed.

        Summed over the deals belonging to the position, because a partial
        close leaves more than one, and swap and commission are separate
        entries -- reading only the closing deal's ``profit`` reports a
        winning trade that actually lost money to financing.
        """
        deals = self._mt5.history_deals_get(position=int(ticket))
        if not deals:
            return None
        return float(sum(float(d.profit) + float(getattr(d, "swap", 0.0))
                         + float(getattr(d, "commission", 0.0)) for d in deals))

    def modify(self, position, stop_loss: float, take_profit: Optional[float] = None):
        from .execution import OrderResult

        mt5 = self._mt5
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": position.symbol,
            "position": int(position.ticket),
            "sl": float(stop_loss),
            "tp": float(take_profit if take_profit is not None else position.take_profit),
        }
        result = mt5.order_send(request)
        if result is None:
            code, msg = mt5.last_error()
            return OrderResult(False, f"modify returned nothing: {code} {msg}")
        ok = result.retcode == mt5.TRADE_RETCODE_DONE
        return OrderResult(ok, "" if ok else f"{result.retcode} {result.comment}",
                           ticket=int(position.ticket), retcode=int(result.retcode))
