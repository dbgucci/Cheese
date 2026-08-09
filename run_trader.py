"""Entry point for the packaged autotrader (KPSTrader.exe).

Frozen apps get double-clicked, so this reads its settings from a file and
never assumes a terminal is watching. It also refuses to exit silently: a
trading app that closes its own window on an error looks identical to one
that finished normally.
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))


class _Tee:
    """Everything printed also goes to a log file beside the config."""

    def __init__(self, stream, path):
        self.stream = stream
        self.fh = open(path, "a", encoding="utf-8", buffering=1)

    def write(self, text):
        self.stream.write(text)
        self.fh.write(text)
        return len(text)

    def flush(self):
        self.stream.flush()
        self.fh.flush()


def main() -> int:
    from cheese_signals.markets.config import TraderConfig, config_path, log_path, write_readme
    from cheese_signals.markets.run import loop, make_trader

    cfg = TraderConfig.load()
    write_readme()
    sys.stdout = _Tee(sys.stdout, log_path())
    sys.stderr = sys.stdout

    print("=" * 62)
    print(f"  KPS Markets autotrader   {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    print("=" * 62)
    print(f"settings  {config_path()}")
    print(cfg.describe())

    problems = cfg.problems()
    if problems:
        print("\nCHECK THESE:")
        for p in problems:
            print(f"  - {p}")
    print()

    try:
        from cheese_signals.markets.mt5_bridge import MT5Trader
    except Exception as exc:
        print(f"Could not load the MetaTrader 5 bridge: {exc}")
        return _hold(1)

    notifier = None
    if cfg.telegram_token and cfg.telegram_chat:
        from cheese_signals.notifiers.telegram import TelegramNotifier
        notifier = TelegramNotifier(cfg.telegram_token, cfg.telegram_chat)

    try:
        broker = MT5Trader(login=cfg.login, password=cfg.password,
                           server=cfg.server,
                           terminal_path=cfg.terminal_path or None)
    except Exception as exc:
        print(f"\nMetaTrader 5 did not connect: {exc}\n")
        print("Check, in order:")
        print("  1. MetaTrader 5 is open and logged in.")
        print("  2. Algo Trading is enabled in the toolbar.")
        print("  3. This app and the terminal are both running as "
              "administrator, or neither is.")
        return _hold(1)

    try:
        diag = broker.diagnostics()
        print(f"broker    {diag['company']} / {diag['server']}")
        print(f"account   {diag['login']}   equity {diag['equity']:.2f} "
              f"{diag['currency']}")
        if cfg.live and not diag["algo_allowed"]:
            print("\nAlgo Trading is OFF in the terminal toolbar. Orders will "
                  "be rejected until you enable it.")
        print()

        trader, feed, missing = make_trader(broker, _Args(cfg), notifier)
        if missing:
            print(f"skipped   {'; '.join(missing)}")
        print(f"trading   {', '.join(trader.plans)}")
        print("\nRunning. Close this window to stop; open positions are left "
              "as they are.\n")
        loop(trader, feed, notifier, interval=cfg.interval_seconds)
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception:
        print("\nThe trader stopped with an error:\n")
        traceback.print_exc()
        return _hold(1)
    finally:
        try:
            broker.close()
        except Exception:
            pass
    return 0


class _Args:
    """Adapts the config file to what ``make_trader`` expects from argparse."""

    def __init__(self, cfg):
        self.live = cfg.live
        self.symbols = cfg.symbols
        self.strategy = cfg.strategy
        self.risk = cfg.risk
        self.max_daily_loss = cfg.max_daily_loss
        self.max_positions = cfg.max_positions
        self.max_trades = cfg.max_trades
        self.blackout_minutes = cfg.blackout_minutes
        self.evaluate_every = cfg.evaluate_every
        self.max_spread_points = cfg.max_spread_points


def _hold(code: int) -> int:
    """Keep the window open so the message can actually be read."""
    try:
        input("\nPress Enter to close...")
    except EOFError:
        pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
