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

from ..paths import data_dir
from . import orb

SETTINGS_FILE = "orb-signals.json"

DEFAULT_SYMBOLS = ["XAUUSD", "XAGUSD", "US30", "SPX500", "NAS100",
                   "EURUSD", "GBPUSD", "USDJPY"]


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

    # --- connection
    terminal_path: Optional[str] = None
    login: Optional[int] = None
    server: str = ""

    # A bot token is stored, unlike a trading password. It is not equivalent:
    # the worst a leaked bot token allows is sending messages as the bot, and
    # the alternative -- retyping a 46-character token every session -- means
    # nobody ever turns alerts on. The trading password is still never saved.

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
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Optional[Path] = None) -> Path:
        path = path or settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return path


def settings_path() -> Path:
    return data_dir() / SETTINGS_FILE
