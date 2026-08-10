"""The double-clickable front end: ORB-Autobot.exe.

Written for the case where there is no terminal, no Python, no command line
and no prior setup -- someone double-clicks a file on their Desktop and
expects to be told what is happening. That constrains the design in three
ways that the CLI in ``autobot.py`` does not have to care about:

* **It must never exit silently.** A console exe launched from Explorer closes
  its window the instant the process ends, so a crash or a one-line error is
  invisible. Every path through this module ends at a "press Enter" pause,
  including the failures.
* **It must find the broker itself.** Asking a first-time user for a terminal
  path is asking them for something they do not know. The MetaTrader package
  attaches to whichever terminal is already running, so the working default is
  "start MT5 first, then this" -- and when that fails, the installs found on
  disk are listed by name so the right one can be picked from a menu rather
  than typed.
* **The dangerous option must be the hard one.** Live trading is the last item
  on the menu, it states the account and balance it is about to trade, and it
  requires a typed phrase. Everything else is a dry run.

Settings live next to the app's other data in ``<Desktop>/KPS/`` so this file
and the Pocket Option side agree about where things go.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ..paths import data_dir
from . import orb
from .autobot import DEFAULT_SYMBOLS, Autobot, AutobotConfig
from .clock import session_for
from .execution import ExecutionConfig
from .guards import GuardConfig

SETTINGS_FILE = "autobot.json"
BANNER = r"""
  ___  ___  ___     _   _   _ _____ ___  ___  ___ _____
 / _ \| _ \| _ )   /_\ | | | |_   _/ _ \| _ )/ _ \_   _|
| (_) |   /| _ \  / _ \| |_| | | || (_) | _ \ (_) || |
 \___/|_|_\|___/ /_/ \_\\___/  |_| \___/|___/\___/ |_|

  Opening range breakout -- FX, metals, and index CFDs
"""

# Where MetaTrader installs itself. Brokers rename the folder, so the search is
# for the executable anywhere one level down rather than for known names.
TERMINAL_SEARCH_ROOTS = [
    r"C:\Program Files",
    r"C:\Program Files (x86)",
]


@dataclass
class LauncherSettings:
    """What the exe remembers between runs. Editable by hand; it is plain JSON."""

    terminal_path: Optional[str] = None
    login: Optional[int] = None
    server: str = ""
    symbols: list[str] = field(default_factory=lambda: list(DEFAULT_SYMBOLS))
    risk_fraction: float = 0.005
    range_minutes: int = 15
    target_r: float = 2.0
    breakeven_at_r: Optional[float] = 1.0
    max_trades_per_session: int = 1
    max_daily_loss_fraction: float = 0.03
    max_open_positions: int = 3
    poll_seconds: int = 20
    backtest_days: int = 180
    commission_points: float = 0.0
    # Deliberately absent: the trading password. A password sitting in a JSON
    # file on the Desktop is a worse risk than typing it once per session, and
    # MetaTrader does not need it at all when the terminal is already logged in.

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "LauncherSettings":
        path = path or settings_path()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  ! {path.name} could not be read ({exc}); using defaults")
            return cls()
        known = {f for f in cls().__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Optional[Path] = None) -> Path:
        path = path or settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return path


def settings_path() -> Path:
    return data_dir() / SETTINGS_FILE


# --------------------------------------------------------------------------
# finding a terminal
# --------------------------------------------------------------------------
def find_terminals(roots: Optional[list[str]] = None) -> list[Path]:
    """Every ``terminal64.exe`` on the machine, so the user can pick one.

    Brokers ship MetaTrader under their own name -- "Liquid Brokers MT5",
    "IC Markets MetaTrader 5" -- so there is no fixed path to look for. The
    executable name is the only constant.

    This matters more than it sounds: the Python package attaches to a
    *running* terminal, so with two brokers installed it can silently attach to
    the wrong one. Listing them by folder name is what makes that choice
    visible instead of accidental.
    """
    found: list[Path] = []
    for root in (roots if roots is not None else TERMINAL_SEARCH_ROOTS):
        base = Path(root)
        if not base.is_dir():
            continue
        try:
            for child in sorted(base.iterdir()):
                if not child.is_dir():
                    continue
                exe = child / "terminal64.exe"
                if exe.is_file():
                    found.append(exe)
        except (OSError, PermissionError):
            continue
    return found


def describe_terminal(exe: Path) -> str:
    """The install folder's name, which is what the broker branded it."""
    return exe.parent.name


# --------------------------------------------------------------------------
# connecting
# --------------------------------------------------------------------------
def connect(settings: LauncherSettings, password: str = ""):
    """Attach to a terminal, or explain what to do about it.

    Returns ``(broker, error)``: exactly one is set. The error is written for
    somebody who has not read any of this code.
    """
    try:
        from .mt5_bridge import MT5Trader
    except ImportError as exc:        # pragma: no cover - import is unconditional
        return None, str(exc)

    try:
        broker = MT5Trader(
            login=settings.login, password=password, server=settings.server,
            terminal_path=settings.terminal_path)
        return broker, None
    except RuntimeError as exc:
        return None, str(exc)


def connection_help(error: str, found: list[Path]) -> list[str]:
    """What to try next, in the order most likely to be the actual problem."""
    lines = [
        "COULD NOT CONNECT TO METATRADER 5",
        "",
        f"  {error}",
        "",
        "The usual causes, in order:",
        "",
        "  1. MetaTrader 5 is not running. Start it, log in, and leave the",
        "     window open -- this app attaches to a running terminal, it does",
        "     not launch one.",
        "  2. It is running but not logged in. The title bar will say",
        '     "No connection" or "Invalid account".',
        "  3. Algo trading is switched off. In MetaTrader: Tools > Options >",
        '     Expert Advisors > tick "Allow algorithmic trading", and click the',
        '     "Algo Trading" button in the toolbar so it turns green.',
        "  4. Your broker does not offer MetaTrader 5 at all. If your account",
        "     area shows no MT5 login number and no server name, that is the",
        "     answer, and nothing here can work around it -- the bot needs an",
        "     MT5 account at some broker.",
    ]
    if found:
        lines += ["", "MetaTrader installs found on this machine:"]
        lines += [f"    {describe_terminal(exe)}\n      {exe}" for exe in found]
    else:
        lines += ["", "No MetaTrader 5 install was found under Program Files.",
                  "Download it from your broker's own website rather than from",
                  "metatrader5.com: the broker's installer already knows its",
                  "server address, which is what makes the server appear in the",
                  "login list."]
    return lines


# --------------------------------------------------------------------------
# the screens
# --------------------------------------------------------------------------
def format_account(diag: dict) -> list[str]:
    algo = "yes" if diag["algo_allowed"] else 'NO -- tick "Algo Trading" in the toolbar'
    experts = "yes" if diag["trade_expert"] else (
        "NO -- the broker has disabled automated trading on this account")
    return [
        "CONNECTED",
        "",
        f"  broker       {diag['company']}",
        f"  server       {diag['server']}",
        f"  account      {diag['login']}  ({diag['currency']})",
        f"  balance      {diag['balance']:.2f}      equity {diag['equity']:.2f}",
        f"  terminal     {diag['terminal_path']}",
        f"  algo allowed {algo}",
        f"  EAs on acct  {experts}",
    ]


def format_plan(symbols: list[str], settings: LauncherSettings) -> list[str]:
    """What the bot will do today, per instrument, in the user's own clock terms."""
    today = datetime.now(timezone.utc).date()
    lines = ["TODAY'S SESSIONS (times in UTC)", ""]
    for symbol in sorted(symbols):
        try:
            spec = session_for(symbol)
        except KeyError:
            lines.append(f"  {symbol:12s} no session mapped -- will not be traded")
            continue
        minutes = orb.suggest_range_minutes(symbol)
        open_at = spec.open_utc(today)
        lines.append(
            f"  {symbol:12s} {spec.label:20s} range {open_at:%H:%M}-"
            f"{open_at + timedelta(minutes=minutes):%H:%M}, "
            f"flat by {spec.close_utc(today):%H:%M}")
    return lines


def build_config(settings: LauncherSettings, symbols: list[str],
                 live: bool) -> AutobotConfig:
    """One place that turns the saved settings into a bot configuration.

    Shared by the console launcher and the desktop window, so the two front
    ends cannot drift into configuring different strategies from the same
    settings file.
    """
    return AutobotConfig(
        symbols=symbols,
        orb=orb.OrbConfig(range_minutes=settings.range_minutes,
                          target_r=settings.target_r,
                          breakeven_at_r=settings.breakeven_at_r,
                          max_trades_per_session=settings.max_trades_per_session),
        execution=ExecutionConfig(risk_fraction=settings.risk_fraction,
                                  dry_run=not live),
        guards=GuardConfig(
            max_daily_loss_fraction=settings.max_daily_loss_fraction,
            max_open_positions=settings.max_open_positions),
        poll_seconds=settings.poll_seconds,
    )


MENU = """
WHAT WOULD YOU LIKE TO DO?

  1. Measure the cost wall          is there room for any strategy here?
  2. Backtest the strategy          walk it forward over this broker's history
  3. Watch it, place nothing        the full bot, orders printed not sent
  4. Trade it for real             sends orders; asks you to confirm

  5. Show my settings file
  0. Quit
"""


def pause(prompt: str = "\nPress Enter to close...") -> None:   # pragma: no cover
    try:
        input(prompt)
    except (EOFError, KeyboardInterrupt):
        pass


def main(argv: Optional[list[str]] = None) -> int:              # pragma: no cover
    # Any argument at all means somebody is driving this from a command line,
    # so hand straight over to the real CLI rather than showing a menu.
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        from .autobot import main as cli_main

        return cli_main(argv)

    print(BANNER)
    settings = LauncherSettings.load()
    path = settings.save()          # writes the file on first run, so it exists
    print(f"  settings: {path}")

    found = find_terminals()
    if settings.terminal_path is None and len(found) > 1:
        print("\nMore than one MetaTrader 5 install found. Which broker?\n")
        for i, exe in enumerate(found, 1):
            print(f"  {i}. {describe_terminal(exe)}")
        print("  0. Whichever is already running")
        choice = input("\nNumber: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(found):
            settings.terminal_path = str(found[int(choice) - 1])
            settings.save()

    print("\nConnecting to MetaTrader 5...")
    broker, error = connect(settings)
    if broker is None:
        print()
        print("\n".join(connection_help(error, found)))
        pause()
        return 1

    try:
        diag = broker.diagnostics()
        print()
        print("\n".join(format_account(diag)))

        from .survey import resolve

        available, missing = resolve(broker, settings.symbols)
        symbols = sorted(available.values())
        if missing:
            print("\nNOT OFFERED ON THIS ACCOUNT")
            for m in missing:
                print(f"  - {m}")
        if not symbols:
            print("\nNone of the configured instruments exist on this account.")
            print(f"Edit 'symbols' in {settings_path()} to match your broker's")
            print("names, then run this again.")
            pause()
            return 1

        print()
        print("\n".join(format_plan(symbols, settings)))

        while True:
            print(MENU)
            choice = input("Number: ").strip()
            if choice in ("0", "q", ""):
                return 0
            if choice == "5":
                print(f"\n{settings_path()}\n")
                print(json.dumps(asdict(settings), indent=2))
                continue
            if choice == "1":
                _run_survey(broker, symbols, settings)
                continue
            if choice == "2":
                _run_backtest(broker, symbols, settings)
                continue
            if choice in ("3", "4"):
                _run_bot(broker, symbols, settings, live=(choice == "4"), diag=diag)
                continue
            print("  Not one of the options.")
    finally:
        try:
            broker.close()
        except Exception:
            pass


def _run_survey(broker, symbols, settings) -> None:             # pragma: no cover
    from .survey import format_report, run

    print("\nPulling 90 days of history and measuring spreads. A few minutes.\n")
    print(format_report(run(broker, symbols=symbols, days=90)))
    pause("\nPress Enter to go back...")


def _run_backtest(broker, symbols, settings) -> None:           # pragma: no cover
    from .clock import BarClock, measure_server_offset
    from .mt5_bridge import M1, fetch_all
    from . import orb_backtest as bt

    days = settings.backtest_days
    print(f"\nPulling {days} days of 1-minute history. This takes a while.\n")
    clock = BarClock(measure_server_offset(broker.server_time(symbols[0])))
    print(f"  {clock.describe()}")

    end = datetime.now(timezone.utc)
    frames, problems = fetch_all(broker, symbols, clock.to_server(end - timedelta(days=days)),
                                 clock.to_server(end), M1)
    for p in problems:
        print(f"  ! {p}")
    if not frames:
        print("\nNo history came back, so there is nothing to test.")
        pause("\nPress Enter to go back...")
        return

    frames = {s: clock.frame_to_utc(df) for s, df in frames.items()}
    points = {s: broker.spec(s).point for s in frames}
    cfg = orb.OrbConfig(range_minutes=settings.range_minutes,
                        target_r=settings.target_r)
    sim = bt.SimConfig(commission_points=settings.commission_points)
    results = bt.compare(frames, cfg, sim, points=points)

    equity = broker.account().equity
    print()
    print(bt.format_report(results["strategy"], equity=equity,
                           risk_fraction=settings.risk_fraction))
    print(bt.format_comparison(results))
    pause("\nPress Enter to go back...")


def _run_bot(broker, symbols, settings, live: bool, diag: dict) -> None:  # pragma: no cover
    if live:
        print()
        print("=" * 68)
        print(f"  LIVE TRADING on {diag['company']} account {diag['login']}")
        print(f"  {diag['currency']} {diag['equity']:.2f}, risking "
              f"{settings.risk_fraction:.2%} per trade")
        print(f"  Stops for the day at -{settings.max_daily_loss_fraction:.0%}")
        print("=" * 68)
        print("\nReal orders will be sent to this account.")
        if input('Type "trade live" to confirm: ').strip() != "trade live":
            print("\nNot confirmed. Nothing was sent.")
            pause("\nPress Enter to go back...")
            return

    config = build_config(settings, symbols, live)
    bot = Autobot(broker, config)
    for warning in config.warnings():
        print(f"  ! {warning}")
    print(f"\n{bot.calibrate().describe()}")
    print(f"Watching {', '.join(symbols)} "
          f"({'LIVE' if live else 'dry run -- nothing is sent'}).")
    print("Press Ctrl+C to stop and go back.\n")

    log = data_dir() / "logs" / "autobot.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log.open("a", encoding="utf-8") as fh:
            def record(event):
                line = event.line()
                print(line, flush=True)
                fh.write(line + "\n")
                fh.flush()

            bot.run(on_event=record)
    except KeyboardInterrupt:
        print("\nStopped.")
        held = bot.executor.adopt()
        if held and live:
            print(f"\n! {len(held)} position(s) are still open and this bot is no")
            print("  longer managing them. They keep their stop and take-profit,")
            print("  but nothing will flatten them at the session close now:")
            for p in held:
                print(f"    {p.symbol} {p.lots} lots, ticket {p.ticket}")
        print(f"\nLog: {log}")
    pause("\nPress Enter to go back...")


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(main())
