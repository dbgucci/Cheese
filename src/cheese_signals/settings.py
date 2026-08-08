"""User-adjustable settings, persisted as JSON in the Desktop data folder."""

from __future__ import annotations

import json
import re
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


# Any run of whitespace, commas or semicolons separates one symbol from the
# next. Splitting on newlines alone meant a list pasted on one line became a
# single 200-character "asset name", which the broker then rejected with an
# error that looked like an authentication problem.
_ASSET_SEPARATORS = re.compile(r"[\s,;]+")

# A Pocket Option symbol: a currency/instrument code, optionally suffixed
# _otc in any casing. Deliberately loose about the code itself -- the platform
# lists gold, indices and crypto too, and hardcoding a currency list here would
# reject valid instruments. A name that passes this but is not tradeable is
# caught at the feed, per asset, with a message that says so.
_ASSET_PATTERN = re.compile(r"^[A-Za-z0-9]{3,12}(_otc)?$", re.IGNORECASE)

# Everything from a '#' to end of line. People annotate watchlists.
_COMMENT = re.compile(r"#[^\n]*")


def parse_assets(text: str) -> tuple[list[str], list[str]]:
    """Split a pasted watchlist into symbols, returning (valid, rejected).

    Accepts one per line, comma-separated, space-separated, or any mixture,
    because all four are what people actually paste. ``#`` starts a comment.
    Symbols are normalised to the platform's own casing (``EURUSD_otc``) and
    de-duplicated with their order preserved.
    """
    valid: list[str] = []
    rejected: list[str] = []
    seen: set[str] = set()

    for raw in _ASSET_SEPARATORS.split(_COMMENT.sub(" ", text or "")):
        token = raw.strip()
        if not token:
            continue
        if not _ASSET_PATTERN.match(token):
            rejected.append(token)
            continue
        base, _, suffix = token.partition("_")
        name = base.upper() + ("_" + suffix.lower() if suffix else "")
        if name not in seen:
            seen.add(name)
            valid.append(name)

    return valid, rejected


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
    # Minimum gap between two signals on the same pair, whatever happened.
    # This is what stops two trades on one pair overlapping; measured on 190
    # live trades it already prevented same-pair overlap entirely (0 of 190).
    cooldown_minutes: int = 3

    # Extra rest for a pair after a settled trade, on top of the base cooldown.
    # These are concentration controls, not an edge. What 190 live trades
    # actually showed about the pair's *next* trade:
    #
    #   after a win   50.5% (n=99)   <- slightly worse
    #   after a loss  58.8% (n=85)   <- slightly better
    #
    # Fisher p=0.30, and the after-a-win effect reverses between halves
    # (39.5% then 58.9%), so neither is trustworthy as a prediction. The win
    # default is 5 because it is the direction the (weak) evidence points and
    # it costs ~14% of trades; the loss default is 0 because the evidence
    # points the *other* way -- resting a pair after a loss would skip its
    # better trades. Raise it only if you want the risk control regardless.
    win_cooldown_minutes: int = 5
    loss_cooldown_minutes: int = 0

    # --- martingale (single-step recovery) ---
    # After a losing trade, immediately re-enter the same pair and direction on
    # the next candle at double the stake. Off by default.
    #
    # This does NOT change the edge. Expected value per unit staked is
    # identical to flat staking at every win rate -- measured, not asserted:
    # -0.0787 either way at a 49.8% win rate, +0.0255 either way at 55.4%.
    # What it changes is the shape: more sequences finish positive (80% at a
    # 55.4% win rate), and the losing ones lose three stakes instead of one.
    #
    # That makes it a bankroll question, and the answer depends entirely on
    # whether the underlying win rate clears break-even. Simulated over 500
    # sequences from a 500 bankroll at a 10 stake:
    #
    #   win rate 49.8%   flat: median 106, busts 39%   |  x1: median 0, busts 88%
    #   win rate 55.4%   flat: median 624, busts  0.4% |  x1: median 757, busts 6%
    #
    # So it roughly doubles the money at a winning rate and roughly guarantees
    # ruin at a losing one. `conflicts()` says so when the two settings
    # disagree.
    martingale_enabled: bool = False
    # Re-entries after a loss. 1 means one recovery trade, risking 3 stakes in
    # the worst case. Capped at 3 deliberately: a 5-deep ladder risks 630 on a
    # 10 stake, which no bankroll here survives.
    martingale_reentries: int = 1

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
        s = cls(**{k: v for k, v in raw.items() if k in known})

        # Repair a watchlist saved by an older build, which split only on
        # newlines: a list pasted on one line was stored as a single symbol
        # made of every pair joined by spaces. Re-splitting it here means the
        # user does not have to notice and retype anything.
        repaired, _ = parse_assets(" ".join(s.assets))
        if repaired and repaired != s.assets:
            s.assets = repaired
            s.save()
        return s

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
        longest = max(self.cooldown_minutes,
                      self.win_cooldown_minutes, self.loss_cooldown_minutes)
        if longest and len(self.assets) * 1.0 and longest >= 30:
            out.append(
                f"The longest pair cooldown is {longest} minutes. With "
                f"{len(self.assets)} pairs that caps you at roughly "
                f"{len(self.assets) * 60 // longest} signals an hour."
            )
        if self.win_cooldown_minutes and self.win_cooldown_minutes <= self.cooldown_minutes:
            out.append(
                f"'Extra rest after a win' ({self.win_cooldown_minutes} min) is not longer "
                f"than the base cooldown ({self.cooldown_minutes} min), so it never applies."
            )
        if self.loss_cooldown_minutes and self.loss_cooldown_minutes <= self.cooldown_minutes:
            out.append(
                f"'Extra rest after a loss' ({self.loss_cooldown_minutes} min) is not longer "
                f"than the base cooldown ({self.cooldown_minutes} min), so it never applies."
            )
        if self.martingale_enabled:
            worst = sum(2 ** k for k in range(self.martingale_reentries + 1))
            base = round(self.account_balance * self.risk_per_trade, 2)
            risked = min(base, self.max_stake) * worst
            out.append(
                f"Martingale is on: a losing sequence risks {worst}x the stake "
                f"(about {risked:.0f}) to win one payout. It does not change the edge — "
                f"expected value per unit staked is identical to flat staking."
            )
            if self.martingale_reentries >= 3:
                out.append(
                    f"{self.martingale_reentries} re-entries means a bad sequence risks "
                    f"{worst}x the stake. Simulated on this app's own results, ladders "
                    f"beyond one re-entry busted the account most of the time."
                )
            out.append(
                "Martingale is on. On 1,543 logged trades no configuration of this "
                "app has cleared the 54.1% break-even — the pooled rate is 48.9%. "
                "Martingale roughly doubles the money at a winning rate and roughly "
                "guarantees ruin at a losing one, so it magnifies whichever one you "
                "actually have."
            )
            if self.adaptive_expiry:
                out.append(
                    "Martingale re-enters on the next candle for the fixed expiry, so "
                    "adaptive expiry does not apply to the recovery trade — the "
                    "recovery always runs for "
                    f"{self.expiry_minutes} minute(s)."
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
