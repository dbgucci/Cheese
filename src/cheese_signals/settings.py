"""User-adjustable settings, persisted as JSON in the Desktop data folder."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from . import paths

# Pocket Option OTC pairs. OTC instruments are broker-priced and trade 24/7,
# which is why they're the default set here.
DEFAULT_ASSETS = [
    "EURUSD_otc",
    "GBPUSD_otc",
    "USDJPY_otc",
    "AUDUSD_otc",
    "USDCAD_otc",
    "EURJPY_otc",
]


@dataclass
class Settings:
    # --- what to trade ---
    assets: list[str] = field(default_factory=lambda: list(DEFAULT_ASSETS))
    timeframe_seconds: int = 60
    expiry_minutes: int = 1

    # --- advance warning ---
    # How many minutes ahead of the entry candle a signal is announced. The
    # setup is re-validated every candle in between and cancelled if it
    # breaks down, so a longer lead means more warning but more cancellations.
    lead_minutes: int = 2

    # --- strategy ---
    # trend_continuation trades with the trend (Heikin Ashi + Keltner + EMA200
    # + fractal); liquidity_sweep trades reversals. They are opposite postures,
    # so only one runs at a time.
    strategy: str = "trend_continuation"
    adaptive_expiry: bool = True
    # How recently a fractal must have been confirmed to count as a trigger.
    # In a strong trend new swing points form rarely, so a 1-2 bar window
    # silences the strategy for long stretches; too wide and it fires on a
    # pullback that is long finished. Watch the Diagnostics tab to tune it.
    fractal_max_age: int = 5
    expiry_min_minutes: int = 1
    expiry_max_minutes: int = 5

    # --- signal quality ---
    min_score: float = 0.60
    require_liquidity_sweep: bool = False
    use_higher_timeframe_bias: bool = True
    bias_multiple: int = 5
    cooldown_minutes: int = 3

    # --- session handling (OTC: measured, not assumed -- see sessions.py) ---
    restrict_to_sessions: bool = False
    allowed_sessions: list[str] = field(default_factory=list)

    # --- risk ---
    account_balance: float = 500.0
    risk_per_trade: float = 0.02
    max_daily_loss_fraction: float = 0.06
    max_trades_per_hour: int = 6

    # --- connectivity ---
    data_source: str = "synthetic"  # synthetic | pocket_option
    pocket_option_ssid: str = ""
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- ui ---
    theme: str = "dark"
    poll_seconds: int = 5

    @classmethod
    def load(cls) -> "Settings":
        path = paths.settings_path()
        if not path.exists():
            s = cls()
            s.save()
            return s
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        paths.settings_path().write_text(
            json.dumps(asdict(self), indent=2), encoding="utf-8"
        )

    def update(self, **kwargs: Any) -> None:
        known = {f.name for f in fields(self)}
        for k, v in kwargs.items():
            if k in known:
                setattr(self, k, v)
        self.save()
