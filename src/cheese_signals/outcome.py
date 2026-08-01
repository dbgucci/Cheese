"""Settle expired signals and attribute *why* each one won or lost.

"Why" is the part that makes the journal useful later. A bare win/loss log
tells you your win rate and nothing else. Recording the conditions that were
true at entry -- and which of them are statistically associated with losing
on your feed -- is what lets you tighten the rules instead of guessing.

Attribution here is deliberately conservative: it reports the conditions that
were present, and flags the ones the analytics module has found to be
associated with losses. It does not claim causation from a single trade.
A single loss on a high-quality setup is variance, not a broken rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .scheduler import PendingSignal
from .strategies import DOWN, FLAT, UP


@dataclass
class Outcome:
    signal: PendingSignal
    entry_price: float
    exit_price: float
    won: bool
    pnl: float
    stake: float
    payout: float
    reason: str
    settled_at: datetime

    @property
    def result_word(self) -> str:
        return "WIN" if self.won else "LOSS"

    @property
    def move_pips(self) -> float:
        return (self.exit_price - self.entry_price) * 10_000


def _direction_of(entry_price: float, exit_price: float) -> int:
    if exit_price > entry_price:
        return UP
    if exit_price < entry_price:
        return DOWN
    return FLAT


def attribute(
    signal: PendingSignal,
    entry_price: float,
    exit_price: float,
    won: bool,
) -> str:
    """Build a human-readable explanation of the result.

    Reads the feature snapshot captured at signal time (regime, ADX, bias
    alignment, displacement quality, lead time, session) and describes which
    of those conditions lined up with the result.
    """
    f = signal.features or {}
    bits: list[str] = []

    actual = _direction_of(entry_price, exit_price)
    move = (exit_price - entry_price) * 10_000

    if actual == FLAT:
        return (
            "Loss: price closed exactly at the entry price (a flat close counts "
            "as a loss on binary options -- there is no push/refund)."
        )

    bits.append(f"predicted {signal.side}, price moved {move:+.1f} pips over the expiry")

    strategy = signal.strategy or "unknown"
    bits.append(f"setup: {strategy}")

    adx = f.get("adx")
    if adx is not None:
        if strategy == "trend" and adx < 22:
            bits.append(f"weak trend backing it (ADX {adx:.1f}) -- marginal for a trend setup")
        elif strategy == "mean_reversion" and adx > 22:
            bits.append(f"ADX {adx:.1f} was high for a reversion setup -- fighting a live trend")
        else:
            bits.append(f"ADX {adx:.1f}")

    disp = f.get("displacement_atr")
    if disp is not None:
        quality = "strong" if disp >= 1.0 else "modest" if disp >= 0.7 else "weak"
        bits.append(f"{quality} displacement ({disp:.2f} ATR)")

    bias_aligned = f.get("bias_aligned")
    if bias_aligned is False:
        bits.append("traded against the higher-timeframe bias")
    elif bias_aligned is True:
        bits.append("aligned with the higher-timeframe bias")

    lead = signal.lead_seconds
    if lead >= 120:
        bits.append(f"{lead // 60}min advance warning (longer lead = more drift risk)")

    if signal.session:
        bits.append(f"session: {signal.session}")

    bits.append(f"confidence was {signal.score:.2f}")

    head = "Win" if won else "Loss"
    return f"{head}: " + "; ".join(bits) + "."


def settle(
    signal: PendingSignal,
    entry_price: float,
    exit_price: float,
    stake: float,
    payout: float,
    settled_at: datetime,
) -> Outcome:
    actual = _direction_of(entry_price, exit_price)
    won = actual == signal.direction and actual != FLAT
    pnl = stake * payout if won else -stake
    reason = attribute(signal, entry_price, exit_price, won)
    return Outcome(
        signal=signal,
        entry_price=entry_price,
        exit_price=exit_price,
        won=won,
        pnl=pnl,
        stake=stake,
        payout=payout,
        reason=reason,
        settled_at=settled_at,
    )
