"""What the signals app remembers between runs.

Separate from ``SignalConfig`` on purpose. ``SignalConfig`` is what the bot
needs to run; this is what a person edited in a window, including things the bot
knows nothing about, like where to send the alerts. Keeping them apart means the
bot stays testable without a settings file and the window stays free to add
fields the strategy does not care about.

Stored as plain JSON in the same folder as the rest of the app's data, so it can
be read, edited or deleted without the app running.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from ..paths import data_dir
from . import orb

SETTINGS_FILE = "orb-signals.json"
# CSV rather than JSON: the point of the results file is that it opens in Excel
# so the record can be sorted and totalled without this app.
RESULTS_FILE = "orb-results.csv"
# Charts are kept beside the record so a subscriber who missed a signal can
# be sent the picture of it afterwards.
CHARTS_DIR = "orb-charts"

# What the app watches out of the box: the ten most heavily traded US large
# caps, the three US index CFDs, and gold and silver.
#
# **No FX.** It was there and it is gone, on the evidence of using it and on a
# structural reason that agrees with the evidence: an opening range is a bet
# that a market's *opening auction* carries information -- a bell, a crossing
# trade, a real imbalance to clear. Stocks and index futures have one. Spot FX
# does not. "London open" is a gradual handover of liquidity from Asia, so no
# single minute is special, the first fifteen produce a range too narrow to
# mean much, and what comes out is mostly spread. The FX session mappings are
# still there for anyone who wants to put a pair back in the list.
#
# The stock list is a starting point, not a ranking to defend: turnover
# leadership rotates, and these were chosen for consistently heavy dollar
# volume rather than a snapshot of one week. Edit the list in Settings.
#
# Two things to know about single stocks that do not apply to the indices.
# They trade only during the cash session, so there is one setup per name per
# day and no overnight follow-through to catch. And they gap on earnings --
# the range filters will skip a morning whose range is already 60% of the
# average day, which is what an earnings gap looks like, but the day itself is
# worth avoiding rather than trusting a filter to catch.
DEFAULT_SYMBOLS = [
    # US large caps, by dollar volume
    "NVDA", "TSLA", "AAPL", "AMZN", "META",
    "MSFT", "AMD", "GOOGL", "NFLX", "AVGO",
    # US indices
    "US30", "SPX500", "NAS100",
    # Metals
    "XAUUSD", "XAGUSD",
]

# Every watchlist this app has ever shipped as its default.
#
# A settings file overrides the default -- that is what a settings file is for,
# and it is also why changing the default did nothing whatsoever for anyone who
# had already opened the app once. "Default to the top ten stocks" reasonably
# means "watch them", not "watch them on a machine that has never run this
# before", and the fix is not to ask someone to retype fifteen tickers.
#
# So a stored list that matches one of these is taken to be nobody's decision --
# it is whatever the default happened to be the day the app first ran -- and a
# new default replaces it. A list matching none of them was edited by hand;
# that is somebody's decision and is left alone, with the window saying the
# default moved and offering a button.
PREVIOUS_DEFAULT_SYMBOLS: list[list[str]] = [
    ["XAUUSD", "XAGUSD", "US30", "SPX500", "NAS100",
     "EURUSD", "GBPUSD", "USDJPY"],
]


@dataclass
class SignalSettings:
    """Editable in the window, and by hand in the JSON file."""

    symbols: list[str] = field(default_factory=lambda: list(DEFAULT_SYMBOLS))
    range_minutes: int = 15
    target_r: float = 2.0
    retest_tolerance_fraction: float = 0.10
    entry_window_minutes: int = 120
    apply_filters: bool = True
    poll_seconds: int = 20

    # --- alerts
    telegram_token: str = ""
    telegram_chat_id: str = ""
    telegram_enabled: bool = True
    alert_on_break: bool = True
    alert_on_retest: bool = True
    alert_on_result: bool = True

    # --- the paper record
    track_outcomes: bool = True
    log_results: bool = True

    # --- making an alert findable later
    # A chart image sent with each alert. The single most useful thing for
    # someone who missed a signal and wants to see what the setup was, because
    # it does not depend on their platform, their zone or their memory.
    attach_chart: bool = True
    keep_charts: bool = True
    # An extra time zone printed in every alert, for whoever the alerts are
    # addressed to. Empty means the three that are always there: UTC, the
    # market's own clock and the broker's chart clock.
    reader_timezone: str = ""

    # --- connection
    terminal_path: Optional[str] = None
    login: Optional[int] = None
    server: str = ""

    # A bot token is stored, unlike a trading password. It is not equivalent:
    # the worst a leaked bot token allows is sending messages as the bot, and
    # the alternative -- retyping a 46-character token every session -- means
    # nobody ever turns alerts on. The trading password is still never saved.

    # Deliberately not a field: it describes what happened during this load, not
    # anything worth writing to the file. Being a plain class attribute keeps it
    # out of asdict() and out of __eq__.
    watchlist_adopted = False
    watchlist_is_custom = False

    # ---------------------------------------------------------- migration
    def adopt_default_watchlist(self) -> bool:
        """Take a changed default list -- but only over a list nobody chose.

        Returns True when the list was replaced. Sets ``watchlist_is_custom``
        when it was not, so the window can say the default moved rather than
        silently disagreeing with what was asked for.
        """
        if self.symbols == DEFAULT_SYMBOLS:
            return False
        for shipped in PREVIOUS_DEFAULT_SYMBOLS:
            if sorted(self.symbols) == sorted(shipped):
                self.symbols = list(DEFAULT_SYMBOLS)
                self.watchlist_adopted = True
                return True
        self.watchlist_is_custom = True
        return False

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_enabled and self.telegram_token
                    and self.telegram_chat_id)

    def wants(self, kind: str) -> bool:
        """Whether this stage should be alerted at all."""
        from .signals import BREAK

        return self.alert_on_break if kind == BREAK else self.alert_on_retest

    def orb_config(self) -> orb.OrbConfig:
        return orb.OrbConfig(
            range_minutes=self.range_minutes,
            target_r=self.target_r,
            entry_window_minutes=self.entry_window_minutes,
        )

    def signal_config(self, symbols: Optional[list[str]] = None):
        """The bot's own configuration, derived in one place.

        One conversion point, shared by the window and the console CLI, so the
        two front ends cannot end up watching for different things from the same
        settings file.
        """
        from .signals import SignalConfig

        return SignalConfig(
            symbols=list(symbols if symbols is not None else self.symbols),
            orb=self.orb_config(),
            poll_seconds=self.poll_seconds,
            retest_tolerance_fraction=self.retest_tolerance_fraction,
            apply_filters=self.apply_filters,
            per_symbol_range_minutes=False,
            track_outcomes=self.track_outcomes,
            reader_timezone=self.reader_timezone,
        )

    def problems(self) -> list[str]:
        """Settings that would stop it working, in words rather than a traceback."""
        out = list(self.orb_config().validate())
        if not self.symbols:
            out.append("no instruments listed, so there is nothing to watch")
        if self.poll_seconds <= 0:
            out.append("the check interval must be at least one second")
        if not (0.0 <= self.retest_tolerance_fraction <= 1.0):
            out.append("the retest tolerance must be between 0% and 100% of the range")
        if self.telegram_enabled and self.telegram_token and not self.telegram_chat_id:
            out.append("a Telegram token is set but no chat ID, so alerts have "
                       "nowhere to go -- use Find my chat ID")
        if not self.alert_on_break and not self.alert_on_retest:
            out.append("both alert types are switched off, so nothing will ever "
                       "be sent")
        if self.reader_timezone:
            try:
                ZoneInfo(self.reader_timezone)
            except Exception:
                out.append(f"'{self.reader_timezone}' is not a time zone name -- "
                           f"use the Region/City form, e.g. America/New_York")
        if self.alert_on_result and not self.track_outcomes:
            out.append("results are set to be alerted but outcome tracking is "
                       "off, so no result will ever be worked out")
        return out

    # ------------------------------------------------------------ storage
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "SignalSettings":
        path = path or settings_path()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A half-written file must not stop the app starting: that is an
            # unrecoverable state for someone with no console to read.
            return cls()
        known = set(cls().__dataclass_fields__)
        settings = cls(**{k: v for k, v in raw.items() if k in known})
        settings.adopt_default_watchlist()
        return settings

    def save(self, path: Optional[Path] = None) -> Path:
        path = path or settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return path


def settings_path() -> Path:
    return data_dir() / SETTINGS_FILE


def results_path() -> Path:
    return data_dir() / RESULTS_FILE


def charts_dir() -> Path:
    return data_dir() / CHARTS_DIR


def chart_path(ref: str, at) -> Path:
    """Where one alert's picture lives.

    Named by the alert's own reference and stamped with the date, so the file a
    subscriber asks about by name is the file on disk.
    """
    safe = "".join(ch for ch in ref if ch.isalnum() or ch in "-_") or "signal"
    return charts_dir() / f"{at:%Y-%m-%d}-{safe}.png"


RESULT_COLUMNS = ["closed_at", "symbol", "side", "result", "entry", "stop",
                  "target", "exit", "risk_points", "points", "cost_points",
                  "r_gross", "r_net", "minutes_held", "ambiguous", "session",
                  "opened_at", "reason"]


def append_result(outcome, path: Optional[Path] = None) -> Path:
    """Add one finished paper trade to the results file.

    Appended per result rather than written at shutdown: an app that is closed
    by the taskbar, or that crashes, never gets a clean shutdown, and a record
    that only survives a graceful exit is not a record.
    """
    import csv

    path = path or results_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new:
            writer.writerow(RESULT_COLUMNS)
        writer.writerow([
            f"{outcome.closed_at:%Y-%m-%d %H:%M}", outcome.symbol, outcome.side,
            outcome.result, f"{outcome.entry:.{outcome.digits}f}",
            f"{outcome.stop:.{outcome.digits}f}",
            f"{outcome.target:.{outcome.digits}f}",
            f"{outcome.exit_price:.{outcome.digits}f}",
            f"{outcome.risk_points:.0f}", f"{outcome.points:.0f}",
            f"{outcome.cost_points:.0f}", f"{outcome.r_gross:.3f}",
            f"{outcome.r_net:.3f}", f"{outcome.minutes_held:.0f}",
            int(outcome.ambiguous), outcome.session_label,
            f"{outcome.opened_at:%Y-%m-%d %H:%M}", outcome.reason,
        ])
    return path


def load_tally(path: Optional[Path] = None):
    """Rebuild the running record from the results file.

    So a restart does not reset the hit rate to zero, which would make the
    number meaningless on any day the app is reopened. Rows that cannot be
    parsed are skipped rather than raising: a truncated last line from a kill
    signal must not stop the app starting.
    """
    import csv

    from .signals import FLAT, LOSS, Tally, WIN

    tally = Tally()
    path = path or results_path()
    if not path.exists():
        return tally
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                result = (row.get("result") or "").strip().lower()
                if result not in (WIN, LOSS, FLAT):
                    continue
                try:
                    r_net = float(row["r_net"])
                    r_gross = float(row["r_gross"])
                except (KeyError, TypeError, ValueError):
                    continue
                if result == WIN:
                    tally.wins += 1
                elif result == LOSS:
                    tally.losses += 1
                else:
                    tally.flats += 1
                tally.r_net += r_net
                tally.r_gross += r_gross
                tally.ambiguous += int((row.get("ambiguous") or "0").strip() == "1")
    except OSError:
        return Tally()
    return tally
