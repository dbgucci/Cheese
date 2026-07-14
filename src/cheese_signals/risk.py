"""Position sizing and trade-pacing guardrails.

Deliberately does NOT implement martingale / anti-martingale staking
("double up after a loss"). That family of systems doesn't change the
underlying edge; it just reshapes the same expectancy into a distribution
with a small chance of a much bigger loss, which is the opposite of what
risk management is for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone


@dataclass
class RiskConfig:
    account_balance: float
    risk_per_trade: float = 0.02  # fraction of balance to stake per signal
    max_daily_loss_fraction: float = 0.06  # stop trading for the day after losing this much
    max_trades_per_hour: int = 6
    min_confluence_score: float = 0.55


@dataclass
class RiskState:
    trades_this_hour: int = 0
    hour_window_start: datetime | None = None
    pnl_today: float = 0.0
    day: object | None = None


def stake_size(cfg: RiskConfig) -> float:
    """Fixed-fractional stake: a constant % of current balance, not a Martingale ladder."""
    return round(cfg.account_balance * cfg.risk_per_trade, 2)


def can_trade(cfg: RiskConfig, state: RiskState, now: datetime, confluence_score: float) -> tuple[bool, str]:
    if confluence_score < cfg.min_confluence_score:
        return False, f"score {confluence_score:.2f} below minimum {cfg.min_confluence_score:.2f}"

    today = now.date()
    if state.day != today:
        state.day = today
        state.pnl_today = 0.0

    if state.pnl_today <= -cfg.max_daily_loss_fraction * cfg.account_balance:
        return False, "daily loss limit reached"

    if state.hour_window_start is None or (now - state.hour_window_start).total_seconds() > 3600:
        state.hour_window_start = now
        state.trades_this_hour = 0

    if state.trades_this_hour >= cfg.max_trades_per_hour:
        return False, f"hourly trade cap reached ({cfg.max_trades_per_hour})"

    return True, "ok"


def record_trade(state: RiskState, pnl_fraction_of_balance: float) -> None:
    state.trades_this_hour += 1
    state.pnl_today += pnl_fraction_of_balance


def is_low_liquidity_session(now_utc: datetime) -> bool:
    """Flag weekend + very early UTC hours, where OTC pricing tends to be thinnest/most synthetic."""
    if now_utc.weekday() >= 5:  # Sat/Sun
        return True
    t = now_utc.time()
    return time(21, 0) <= t or t <= time(1, 0)
