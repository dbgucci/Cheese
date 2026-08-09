"""The autotrader entry point.

    python -m cheese_signals.markets.run                 # dry run, decides nothing real
    python -m cheese_signals.markets.run --live          # places orders

Dry run is the default and stays the default. ``--live`` is the only way
orders reach the broker, and it prints what it is about to do first.

The loop runs one cycle a minute. Nothing here is time-critical -- positions
are held for hours -- so a cycle that takes two seconds is irrelevant, and a
missed minute costs a check rather than a trade.
"""

from __future__ import annotations

import argparse
import time as _time
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from . import news as news_mod
from .execution import ExecutionConfig, Executor
from .guards import GuardConfig, Guards
from .strategy import IntradayMomentum, MomentumConfig, OpeningRangeBreakout, ORBConfig
from .trader import CycleEvent, SymbolPlan, Trader

# Sessions in UTC. Index CFDs track the US cash session; gold is traded
# through the London and US hours where the spread is tightest.
US_CASH = (time(13, 30), time(20, 0))
GOLD = (time(7, 0), time(20, 0))

# Defaults chosen from the research, not from taste: intraday momentum on the
# indices and gold, which is where the published evidence survives costs, at
# a holding period long enough to clear a retail CFD spread.
#
# max_spread_points is the live gate. Leave it None until the cost survey has
# run against the account, then set each one from the measured p90.
DEFAULT_PLAN = {
    "US30":   dict(session=US_CASH, max_spread_points=None),
    "NAS100": dict(session=US_CASH, max_spread_points=None),
    "SPX500": dict(session=US_CASH, max_spread_points=None),
    "XAUUSD": dict(session=GOLD,    max_spread_points=None),
}


def build_plans(broker, symbols: Optional[list[str]] = None,
                strategy_name: str = "intraday_momentum",
                evaluate_every: int = 30) -> tuple[list[SymbolPlan], list[str]]:
    """Resolve symbols against the account and attach a strategy to each."""
    from .survey import resolve

    wanted = symbols or list(DEFAULT_PLAN)
    found, missing = resolve(broker, wanted)
    plans = []
    for want, actual in found.items():
        cfg = DEFAULT_PLAN.get(want, dict(session=US_CASH, max_spread_points=None))
        open_t, close_t = cfg["session"]
        try:
            point = broker.spec(actual).point
        except (KeyError, RuntimeError):
            missing.append(f"{want}: no contract spec")
            continue
        if strategy_name == "opening_range_breakout":
            strat = OpeningRangeBreakout(
                ORBConfig(session_open=open_t, session_close=close_t), point=point)
        else:
            strat = IntradayMomentum(
                MomentumConfig(session_open=open_t, session_close=close_t,
                               evaluate_every_minutes=evaluate_every), point=point)
        plans.append(SymbolPlan(actual, strat, cfg.get("max_spread_points")))
    return plans, missing


def trading_hours(plans: list[SymbolPlan]) -> dict[str, tuple[int, int]]:
    out = {}
    for p in plans:
        cfg = getattr(p.strategy, "cfg", None)
        if cfg is None:
            continue
        out[p.symbol] = (cfg.session_open.hour, cfg.session_close.hour)
    return out


class NewsFeed:
    """The calendar, fetched once a day and cached.

    A fetch failure keeps yesterday's windows rather than reporting none: an
    empty calendar means "trade through the release", and a network blip is
    not a reason to do that.
    """

    def __init__(self, universe: list[str], blackout_minutes: int = 15):
        self.universe = universe
        self.blackout_minutes = blackout_minutes
        self.events: list[news_mod.Event] = []
        self._fetched_on = None
        self.last_error = ""

    def refresh(self, now: datetime) -> bool:
        if self._fetched_on == now.date():
            return True
        try:
            self.events = news_mod.fetch()
            self._fetched_on = now.date()
            self.last_error = ""
            return True
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    def windows(self, now: Optional[datetime] = None):
        now = now or datetime.now(timezone.utc)
        return news_mod.blackout_windows(self.events, self.universe, now)

    def brief(self, now: datetime) -> str:
        text = news_mod.daily_brief(self.events, self.universe, now,
                                    self.blackout_minutes)
        if self.last_error:
            text += (f"\n\n_Calendar could not be refreshed today "
                     f"({self.last_error}); the windows above may be stale._")
        return text


def make_trader(broker, args, notifier=None) -> tuple[Trader, NewsFeed, list[str]]:
    plans, missing = build_plans(broker, args.symbols, args.strategy,
                                 args.evaluate_every)
    if not plans:
        raise SystemExit("None of the requested instruments are available. "
                         + "; ".join(missing))

    guards = Guards(GuardConfig(
        max_daily_loss_fraction=args.max_daily_loss,
        max_open_positions=args.max_positions,
        max_trades_per_day=args.max_trades,
        news_blackout_minutes=args.blackout_minutes,
        trading_hours_utc=trading_hours(plans),
    ))
    executor = Executor(broker, ExecutionConfig(
        risk_fraction=args.risk,
        max_spread_points={p.symbol: p.max_spread_points
                           for p in plans if p.max_spread_points},
        dry_run=not args.live,
    ))
    feed = NewsFeed([p.symbol for p in plans], args.blackout_minutes)

    def on_event(e: CycleEvent) -> None:
        stamp = f"{e.at:%H:%M:%S}"
        line = f"[{stamp}] {e.symbol or '-':10s} {e.kind:8s} {e.detail}"
        if e.kind in ("opened", "closed", "error"):
            print(line, flush=True)
            if notifier is not None:
                notifier.send(f"*{e.kind.upper()}* {e.symbol}\n{e.detail}")
        elif e.kind == "trailed":
            print(line, flush=True)

    trader = Trader(broker, plans, executor, guards,
                    on_event=on_event, news_windows=feed.windows)
    return trader, feed, missing


def loop(trader: Trader, feed: NewsFeed, notifier=None,
         interval: float = 60.0, max_cycles: Optional[int] = None) -> None:
    briefed_on = None
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        now = datetime.now(timezone.utc)
        feed.refresh(now)
        if briefed_on != now.date():
            briefed_on = now.date()
            brief = feed.brief(now)
            print("\n" + brief + "\n", flush=True)
            if notifier is not None:
                notifier.send(brief)
        trader.cycle(now)
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        _time.sleep(interval)


def main(argv: Optional[list[str]] = None) -> int:      # pragma: no cover
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true",
                    help="actually place orders; without this nothing is sent")
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--strategy", default="intraday_momentum",
                    choices=["intraday_momentum", "opening_range_breakout"])
    ap.add_argument("--risk", type=float, default=0.005,
                    help="fraction of equity risked per trade (default 0.5%%)")
    ap.add_argument("--max-daily-loss", type=float, default=0.03,
                    dest="max_daily_loss")
    ap.add_argument("--max-positions", type=int, default=2, dest="max_positions")
    ap.add_argument("--max-trades", type=int, default=6, dest="max_trades")
    ap.add_argument("--blackout-minutes", type=int, default=15,
                    dest="blackout_minutes")
    ap.add_argument("--evaluate-every", type=int, default=30, dest="evaluate_every",
                    help="check for a breakout every N minutes of the session")
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--telegram-token", default=None)
    ap.add_argument("--telegram-chat", default=None)
    ap.add_argument("--login", type=int, default=None)
    ap.add_argument("--password", default="")
    ap.add_argument("--server", default="")
    ap.add_argument("--terminal", default=None)
    args = ap.parse_args(argv)

    from .mt5_bridge import MT5Trader

    notifier = None
    if args.telegram_token and args.telegram_chat:
        from ..notifiers.telegram import TelegramNotifier
        notifier = TelegramNotifier(args.telegram_token, args.telegram_chat)

    broker = MT5Trader(login=args.login, password=args.password,
                       server=args.server, terminal_path=args.terminal)
    diag = broker.diagnostics()
    trader, feed, missing = make_trader(broker, args, notifier)

    print(f"broker   {diag['company']} / {diag['server']}")
    print(f"account  {diag['login']}  equity {diag['equity']:.2f} {diag['currency']}")
    print(f"mode     {'LIVE - orders will be placed' if args.live else 'dry run'}")
    print(f"risk     {args.risk:.2%} per trade, "
          f"stop for the day at -{args.max_daily_loss:.2%}")
    print(f"symbols  {', '.join(p.symbol for p in trader.plans.values())}")
    if missing:
        print(f"skipped  {'; '.join(missing)}")
    if args.live and not diag["algo_allowed"]:
        print("\nAlgo Trading is switched off in the terminal toolbar. Orders "
              "will be rejected until it is enabled.")
    print()

    try:
        loop(trader, feed, notifier, interval=args.interval)
    except KeyboardInterrupt:
        print("\nstopping; open positions are left as they are")
    finally:
        broker.close()
    return 0


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(main())
