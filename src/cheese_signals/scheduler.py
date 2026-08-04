"""Advance-warning signal scheduling.

The requirement is that a signal arrives *minutes before* you need to act,
telling you the pair, the direction, and the exact minute to enter.

The honest constraint
---------------------
Nothing can see the future. A 1-minute setup is detected when a 1-minute
candle *closes*; the naive version of this bot would then say "enter now",
giving you a few seconds of warning at best.

To give real advance warning, this scheduler separates **detection** from
**entry**:

    detected at 14:31:00  ->  entry at 14:33:00  ->  expiry at 14:34:00
                              (lead_minutes = 2)     (expiry_minutes = 1)

That buys you the warning time you asked for, and it has a real cost that is
worth stating plainly: the market keeps moving during the lead window, so a
setup detected two minutes early is a weaker predictor than one acted on
immediately. Two things mitigate it:

1. **Re-validation.** ``revalidate()`` is called on every new candle between
   detection and entry. If the setup breaks down (direction flips, score
   collapses, or the level that defined it is violated) the signal is
   cancelled before you ever trade it, with the reason recorded.
2. **Measurement.** Every signal stores its ``lead_seconds``, so the
   analytics tab reports realised win rate *by lead time*. If 2 minutes of
   warning costs you 4 points of win rate on your feed, you will see it and
   can tune ``lead_minutes`` down.

Set ``lead_minutes = 0`` for immediate-entry behaviour with no warning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .strategies import DOWN, FLAT, UP

PENDING = "pending"
ACTIVE = "active"
CANCELLED = "cancelled"
SETTLED = "settled"


def next_candle_open(now: datetime, timeframe_seconds: int = 60) -> datetime:
    """The start of the next candle boundary at or after ``now``."""
    epoch = now.replace(tzinfo=now.tzinfo or timezone.utc).timestamp()
    boundary = (int(epoch) // timeframe_seconds + 1) * timeframe_seconds
    return datetime.fromtimestamp(boundary, tz=timezone.utc)


@dataclass
class PendingSignal:
    """A signal announced ahead of time and awaiting its entry minute."""

    asset: str
    direction: int
    score: float
    strategy: str
    reason: str
    detected_at: datetime
    entry_at: datetime
    expiry_at: datetime
    session: str
    features: dict = field(default_factory=dict)
    status: str = PENDING
    cancel_reason: Optional[str] = None
    db_id: Optional[int] = None
    entry_price: Optional[float] = None
    order: object = None          # the placed trade, when autotrading is on

    @property
    def lead_seconds(self) -> int:
        return int((self.entry_at - self.detected_at).total_seconds())

    @property
    def side(self) -> str:
        return "BUY" if self.direction == UP else "SELL"

    def seconds_until_entry(self, now: datetime) -> float:
        return (self.entry_at - now).total_seconds()

    def seconds_until_expiry(self, now: datetime) -> float:
        return (self.expiry_at - now).total_seconds()

    def is_due_for_entry(self, now: datetime) -> bool:
        return self.status == PENDING and now >= self.entry_at

    def is_due_for_settlement(self, now: datetime) -> bool:
        return self.status == ACTIVE and now >= self.expiry_at


def schedule_signal(
    asset: str,
    direction: int,
    score: float,
    strategy: str,
    reason: str,
    detected_at: datetime,
    session: str,
    lead_minutes: int = 2,
    expiry_minutes: int = 1,
    timeframe_seconds: int = 60,
    features: Optional[dict] = None,
) -> PendingSignal:
    """Create a signal scheduled ``lead_minutes`` ahead, aligned to candle boundaries.

    Entry is always snapped to a candle open, because a binary option bought
    mid-candle doesn't align with the candle the strategy actually predicted.
    """
    first_open = next_candle_open(detected_at, timeframe_seconds)
    entry_at = first_open + timedelta(seconds=timeframe_seconds * max(lead_minutes, 0))
    expiry_at = entry_at + timedelta(minutes=expiry_minutes)
    return PendingSignal(
        asset=asset,
        direction=direction,
        score=score,
        strategy=strategy,
        reason=reason,
        detected_at=detected_at,
        entry_at=entry_at,
        expiry_at=expiry_at,
        session=session,
        features=features or {},
    )


# (reason a DOWN signal failed, reason an UP signal failed), per trigger.
_INVALIDATION_WORDING = {
    "bos": (
        "the broken structure was reclaimed, so the break failed",
        "the broken structure was reclaimed, so the break failed",
    ),
    "fractal": (
        "the swing point was taken out, so the pullback did not hold",
        "the swing point was taken out, so the pullback did not hold",
    ),
    "momentum": (
        "the momentum candle was fully retraced",
        "the momentum candle was fully retraced",
    ),
    "": (        # liquidity_sweep and anything that does not name a trigger
        "a genuine breakout, not a sweep",
        "a genuine breakdown, not a sweep",
    ),
}


def revalidate(
    pending: PendingSignal,
    current_direction: int,
    current_score: float,
    min_score: float,
    latest_close: Optional[float] = None,
    score_collapse_ratio: float = 0.6,
) -> Optional[str]:
    """Re-check a not-yet-entered signal against the latest candle.

    Returns a cancellation reason, or ``None`` if the signal still stands.

    Crucially, this asks *"is the premise still true?"* -- **not** *"is the
    setup still firing?"*. Most setups here are one-shot events: a liquidity
    sweep happens on a single candle and is over. Re-running the detector a
    minute later correctly reports "no sweep right now", so scoring a pending
    signal that way drives it to zero and cancels every single trade before it
    is ever taken. (That was a real bug: nothing ever entered, so no outcomes,
    history or analytics were ever produced.)

    A pending event setup is therefore invalidated only by evidence that it was
    *wrong*:

    * an opposing setup actually fires, or
    * price closes back through the level that defined it -- for a swept high,
      closing above that high means the level genuinely broke rather than being
      swept, which is the opposite trade.

    Score decay still applies to continuous setups (trend/mean-reversion),
    where "the condition no longer holds" is meaningful.
    """
    if pending.status != PENDING:
        return None

    if current_direction != FLAT and current_direction != pending.direction:
        return "setup invalidated: direction flipped before entry"

    level = (pending.features or {}).get("invalidation_level")
    if level and latest_close is not None:
        # The rule is the same for every setup -- price closing back through
        # the level that defined the signal means the premise failed -- but
        # the *wording* must match the setup that fired. A break-of-structure
        # signal cancelled with "not a sweep" reads like the wrong strategy
        # ran, which is exactly how a correct decision loses the user's trust.
        why = _INVALIDATION_WORDING.get(
            (pending.features or {}).get("trigger", ""), _INVALIDATION_WORDING[""]
        )
        if pending.direction == DOWN and latest_close > level:
            return (
                f"setup invalidated: price closed back above {level:.5f} "
                f"(close {latest_close:.5f}) -- {why[0]}"
            )
        if pending.direction == UP and latest_close < level:
            return (
                f"setup invalidated: price closed back below {level:.5f} "
                f"(close {latest_close:.5f}) -- {why[1]}"
            )

    is_event = bool((pending.features or {}).get("event_setup"))
    if not is_event and current_score < min_score * score_collapse_ratio:
        return (
            f"setup decayed: score fell to {current_score:.2f} "
            f"(below {min_score * score_collapse_ratio:.2f})"
        )

    return None


class SignalScheduler:
    """Tracks pending/active signals through their lifecycle."""

    def __init__(self, lead_minutes: int = 2, expiry_minutes: int = 1, timeframe_seconds: int = 60):
        self.lead_minutes = lead_minutes
        self.expiry_minutes = expiry_minutes
        self.timeframe_seconds = timeframe_seconds
        self.pending: list[PendingSignal] = []

    def add(self, signal: PendingSignal) -> None:
        self.pending.append(signal)

    def has_pending_for(self, asset: str) -> bool:
        return any(s.asset == asset and s.status in (PENDING, ACTIVE) for s in self.pending)

    def due_for_entry(self, now: datetime) -> list[PendingSignal]:
        return [s for s in self.pending if s.is_due_for_entry(now)]

    def due_for_settlement(self, now: datetime) -> list[PendingSignal]:
        return [s for s in self.pending if s.is_due_for_settlement(now)]

    def awaiting_entry(self, now: datetime) -> list[PendingSignal]:
        return [s for s in self.pending if s.status == PENDING and s.entry_at > now]

    def cancel(self, signal: PendingSignal, reason: str) -> None:
        signal.status = CANCELLED
        signal.cancel_reason = reason

    def mark_active(self, signal: PendingSignal, entry_price: float) -> None:
        signal.status = ACTIVE
        signal.entry_price = entry_price

    def mark_settled(self, signal: PendingSignal) -> None:
        signal.status = SETTLED

    def prune(self, keep: int = 200) -> None:
        finished = [s for s in self.pending if s.status in (CANCELLED, SETTLED)]
        if len(finished) > keep:
            drop = set(id(s) for s in finished[: len(finished) - keep])
            self.pending = [s for s in self.pending if id(s) not in drop]
