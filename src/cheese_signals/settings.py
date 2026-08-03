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
    #
    # Defaults to 1, not 2: on a 1-minute expiry a 2-minute warning means the
    # move that produced the setup is usually over before entry, and the
    # conflict checks below say so. Raise it if the Analytics "by
    # advance-warning time" table on your own data disagrees.
    lead_minutes: int = 1

    # --- setup + trigger ---
    # The setup picks a direction and why; the trigger decides when to act.
    # Any setup can be paired with any trigger.
    strategy: str = "trend_continuation"   # trend_continuation | support_resistance | reversal
    trigger: str = "bos"                   # fractal | bos | momentum

    # trigger tuning
    bos_lookback: int = 30
    bos_buffer_atr: float = 0.0
    momentum_close_pct: float = 0.70
    momentum_range_atr: float = 0.80

    # trend_continuation tuning
    ema_trend: int = 200
    keltner_ema: int = 20
    keltner_atr: int = 10
    keltner_mult: float = 1.0
    require_ha_alignment: bool = True

    # support_resistance tuning
    sr_lookback: int = 30
    sr_touch_atr: float = 0.25
    sr_reject_pct: float = 0.5

    # reversal tuning
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    bb_period: int = 20
    bb_std: float = 2.0
    adaptive_expiry: bool = True
    # How recently a fractal must have been confirmed to count as a trigger.
    # In a strong trend new swing points form rarely, so a 1-2 bar window
    # silences the strategy for long stretches; too wide and it fires on a
    # pullback that is long finished. Watch the Diagnostics tab to tune it.
    fractal_max_age: int = 5
    # Only trade when ADX sits in this band. Defaults are wide open (no
    # filtering); narrow them only on evidence from your own Analytics tab.
    adx_min: float = 0.0
    adx_max: float = 100.0
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

    # --- execution (autotrading) ---
    # Default is off. Execution never turns itself on: paper is an explicit
    # choice, and live additionally requires live_confirmed, which the UI only
    # sets after a typed confirmation.
    trade_mode: str = "off"          # off | paper | live
    live_confirmed: bool = False
    max_stake: float = 50.0
    max_concurrent_trades: int = 3
    max_daily_loss: float = 100.0
    min_balance: float = 50.0

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

    # ------------------------------------------------------------------
    def setup_config(self):
        """Build the SetupConfig these settings describe."""
        from .setups import SetupConfig

        return SetupConfig(
            kind=self.strategy,
            ema_trend=self.ema_trend,
            keltner_ema=self.keltner_ema,
            keltner_atr=self.keltner_atr,
            keltner_mult=self.keltner_mult,
            require_ha_alignment=self.require_ha_alignment,
            sr_lookback=self.sr_lookback,
            sr_touch_atr=self.sr_touch_atr,
            sr_reject_pct=self.sr_reject_pct,
            rsi_period=self.rsi_period,
            rsi_overbought=self.rsi_overbought,
            rsi_oversold=self.rsi_oversold,
            bb_period=self.bb_period,
            bb_std=self.bb_std,
            adx_min=self.adx_min,
            adx_max=self.adx_max,
        )

    def trigger_config(self):
        from .triggers import TriggerConfig

        return TriggerConfig(
            kind=self.trigger,
            fractal_period=7,
            fractal_max_age=self.fractal_max_age,
            bos_lookback=self.bos_lookback,
            bos_buffer_atr=self.bos_buffer_atr,
            momentum_close_pct=self.momentum_close_pct,
            momentum_range_atr=self.momentum_range_atr,
        )

    def conflicts(self) -> list[str]:
        """Settings combinations that fight each other or silence the bot.

        Returned as plain sentences for the UI to show. A setting that quietly
        cancels another is worse than one that refuses loudly.
        """
        out: list[str] = []

        if self.adx_min > self.adx_max:
            out.append(
                f"ADX band is inverted (min {self.adx_min:.0f} > max {self.adx_max:.0f}) — "
                "no trade can ever satisfy it."
            )
        if self.expiry_min_minutes > self.expiry_max_minutes:
            out.append("Shortest expiry is longer than the longest expiry.")
        if not self.adaptive_expiry and not (
            self.expiry_min_minutes <= self.expiry_minutes <= self.expiry_max_minutes
        ):
            out.append(
                f"Adaptive expiry is off, so the fixed {self.expiry_minutes}-minute expiry is "
                "used and the min/max range above is ignored."
            )
        if self.strategy == "reversal" and self.adx_min >= 25:
            out.append(
                "Reversal setups look for exhaustion, but a high ADX floor only admits strong "
                "trends — the two rarely coincide, so expect very few signals."
            )
        if self.strategy == "trend_continuation" and self.adx_max <= 15:
            out.append(
                "Trend continuation with an ADX ceiling of 15 or less only trades weak trends. "
                "That was your best-performing slice, but it is a deliberate contradiction — "
                "keep an eye on it."
            )
        if self.trigger == "fractal" and self.fractal_max_age <= 2:
            out.append(
                "A fractal is only confirmed 3 candles after it forms, so a trigger window of "
                f"{self.fractal_max_age} leaves almost no candles where it can fire."
            )
        if self.trigger != "fractal" and self.fractal_max_age != 5:
            out.append(
                f"'Fractal trigger window' only affects the fractal trigger; the current "
                f"trigger is '{self.trigger}', so that setting is ignored."
            )
        if self.trigger == "momentum" and self.momentum_close_pct < 0.55:
            out.append(
                "A momentum close threshold below 55% barely filters anything — nearly every "
                "bar will qualify."
            )
        if self.lead_minutes >= 2 and self.expiry_minutes <= 1:
            out.append(
                f"A {self.lead_minutes}-minute advance warning on a {self.expiry_minutes}-minute "
                "expiry means the move is usually over before entry. Consider lead 0–1."
            )
        if self.trade_mode == "live" and not self.live_confirmed:
            out.append("Live mode is selected but unconfirmed, so nothing will be traded.")
        if self.trade_mode != "off" and self.risk_per_trade * self.account_balance > self.max_stake:
            out.append(
                f"Risk per trade would stake "
                f"{self.risk_per_trade * self.account_balance:.2f}, above the "
                f"{self.max_stake:.2f} hard cap — the cap wins."
            )
        if self.trade_mode != "off" and self.account_balance <= self.min_balance:
            out.append(
                f"Balance {self.account_balance:.2f} is at or below the "
                f"{self.min_balance:.2f} floor — no trade will ever be placed."
            )
        if self.restrict_to_sessions and not self.allowed_sessions:
            out.append("Session restriction is on but no sessions are selected — nothing can trade.")
        if self.min_score > 0.95:
            out.append(f"Minimum confidence of {self.min_score:.2f} will reject nearly every signal.")
        if not self.assets:
            out.append("No pairs are being watched.")
        return out

    def update(self, **kwargs: Any) -> None:
        known = {f.name for f in fields(self)}
        for k, v in kwargs.items():
            if k in known:
                setattr(self, k, v)
        self.save()
