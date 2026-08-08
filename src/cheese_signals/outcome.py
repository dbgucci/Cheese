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


def pip_size(asset: str) -> float:
    """One pip for this instrument.

    Yen-quoted pairs are quoted to 2-3 decimals and their pip is 0.01, not
    0.0001. Using a single 1/10000 factor everywhere reported a 1.1-pip
    USDJPY move as "+110.0 pips" and a 6.5-pip EURJPY move as "-650.0 pips",
    which makes the trade log actively misleading.
    """
    base = asset.upper().replace("_OTC", "")
    return 0.01 if base.endswith("JPY") else 0.0001


def pips(asset: str, price_delta: float) -> float:
    return price_delta / pip_size(asset)


def is_refund(entry_price: Optional[float], exit_price: Optional[float]) -> bool:
    """A flat close: exit exactly equal to entry.

    Pocket Option returns the stake when the expiry price equals the entry
    price -- the trade is neither won nor lost. Recording it as a loss cost
    16 of the first 1,543 live trades a full stake each on paper (-$800 of
    fictional P/L) and pushed the reported win rate down by about half a
    point, because those trades stayed in the denominator as losses.
    """
    if entry_price is None or exit_price is None:
        return False
    return entry_price == exit_price


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
    def refunded(self) -> bool:
        return is_refund(self.entry_price, self.exit_price)

    @property
    def result_word(self) -> str:
        if self.refunded:
            return "REFUND"
        return "WIN" if self.won else "LOSS"

    @property
    def move_pips(self) -> float:
        return pips(self.signal.asset, self.exit_price - self.entry_price)


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
    move = pips(signal.asset, exit_price - entry_price)

    if actual == FLAT:
        return (
            "Refund: price closed exactly at the entry price, so the stake is "
            "returned. This is neither a win nor a loss and is excluded from "
            "the win rate."
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
    if actual == FLAT:
        pnl = 0.0            # stake returned
    else:
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
