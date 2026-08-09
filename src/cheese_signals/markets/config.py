"""Settings for the autotrader, as a file rather than command-line flags.

A packaged exe is double-clicked, so every knob has to live somewhere the
user can edit without a terminal. The file is written on first run with the
defaults and a note beside each risky field, in the same Desktop folder as
everything else this app produces.

``live`` defaults to false and stays false until it is changed by hand. There
is no flag, prompt or first-run wizard that can turn trading on -- switching
it on has to be a deliberate edit to a file, because the alternative is
someone clicking through a dialog they did not read.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

from .. import paths

FOLDER_NAME = "KPS Markets"


def data_dir() -> Path:
    """``<Desktop>/KPS Markets``. Separate from the Pocket Option folder.

    Deliberately not shared: these are different products with different
    journals, and mixing binary-option results into CFD statistics would
    make both meaningless.
    """
    import os

    override = os.environ.get("KPS_MARKETS_HOME")
    root = Path(override) if override else paths._desktop_dir() / FOLDER_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def config_path() -> Path:
    return data_dir() / "trader.json"


def log_path() -> Path:
    return data_dir() / "trader.log"


@dataclass
class TraderConfig:
    # --- the switch that matters ---
    live: bool = False

    # --- what to trade ---
    symbols: list[str] = field(default_factory=lambda: ["US30", "NAS100",
                                                        "SPX500", "XAUUSD"])
    strategy: str = "intraday_momentum"      # or opening_range_breakout
    evaluate_every: int = 30                 # minutes between breakout checks

    # --- risk ---
    risk: float = 0.005                      # fraction of equity per trade
    max_daily_loss: float = 0.03
    max_positions: int = 2
    max_trades: int = 6
    min_equity: float = 0.0

    # --- the cost gate ---
    # Refuse a trade when the live spread exceeds this, per symbol, in points.
    # Empty means no gate. Fill it from the survey's p90 column: without it
    # the bot will trade at any spread, including the ones the strategy was
    # never tested at.
    max_spread_points: dict[str, float] = field(default_factory=dict)

    # --- news ---
    blackout_minutes: int = 15

    # --- connection ---
    login: Optional[int] = None
    password: str = ""
    server: str = ""
    terminal_path: str = ""

    # --- alerts ---
    telegram_token: str = ""
    telegram_chat: str = ""

    interval_seconds: float = 60.0

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "TraderConfig":
        path = path or config_path()
        if not path.exists():
            cfg = cls()
            cfg.save(path)
            return cfg
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt file must not silently become defaults with live=False
            # looking like a deliberate choice -- but it also must not stop the
            # app. Defaults are safe, so use them and say so at start-up.
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Optional[Path] = None) -> None:
        path = path or config_path()
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    def problems(self) -> list[str]:
        """Settings that would trade in a way the user probably did not mean."""
        out = []
        if not self.symbols:
            out.append("No symbols configured; nothing will be traded.")
        if self.risk <= 0:
            out.append("Risk per trade is zero, so every position sizes to nothing.")
        if self.risk > 0.02:
            out.append(
                f"Risk per trade is {self.risk:.1%}. At that size a run of five "
                f"losses -- ordinary for any strategy -- costs "
                f"{1 - (1 - self.risk) ** 5:.0%} of the account.")
        if self.max_daily_loss <= 0:
            out.append("The daily loss limit is off. This is the one control "
                       "that turns a bad day into a bounded day.")
        if self.risk >= self.max_daily_loss:
            out.append(
                f"A single trade risks {self.risk:.2%} but the daily limit is "
                f"{self.max_daily_loss:.2%}, so one loss stops the day. Either "
                f"lower the risk or raise the limit.")
        if self.live and not self.max_spread_points:
            out.append(
                "Live with no spread limits set. The strategy will take trades "
                "at any spread, including the wide ones it was never tested "
                "at. Run the cost survey and fill max_spread_points.")
        if self.evaluate_every < 1:
            out.append("evaluate_every must be at least 1 minute.")
        return out

    def describe(self) -> str:
        mode = "LIVE - real orders" if self.live else "dry run - nothing is sent"
        return (
            f"mode      {mode}\n"
            f"symbols   {', '.join(self.symbols) or '(none)'}\n"
            f"strategy  {self.strategy}, checked every {self.evaluate_every} min\n"
            f"risk      {self.risk:.2%} per trade, day stops at "
            f"-{self.max_daily_loss:.2%}\n"
            f"limits    {self.max_positions} positions, {self.max_trades} trades/day\n"
            f"spread    {self.max_spread_points or 'no gate set'}"
        )


HEADER_NOTE = """\
This file controls the autotrader. It was created with safe defaults.

  "live": false     nothing is sent to the broker. Change to true only after
                    you have watched it in dry run and run the cost survey.
  "risk"            fraction of equity risked per trade. 0.005 is 0.5%.
  "max_daily_loss"  the day stops here. Do not set it to 0.
  "max_spread_points"
                    per symbol, from the survey's spread_p90 column. Without
                    it the bot trades at any spread.

Close the app before editing, then start it again.
"""


def write_readme() -> Path:
    p = data_dir() / "README.txt"
    if not p.exists():
        p.write_text(HEADER_NOTE, encoding="utf-8")
    return p
