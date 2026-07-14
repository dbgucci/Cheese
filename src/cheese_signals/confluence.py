"""Combine individual strategies into one confluence-scored trade decision.

This is the core idea that separates this from a single-indicator script:

1. Classify the current regime (trending vs. ranging) with ADX -- baked
   into ``trend_following``/``mean_reversion`` themselves, since each already
   gates on the ADX threshold that fits it. At most one of the two can fire
   on a given candle.
2. Treat price-action (support/resistance rejection) as a *confirming* vote
   only -- it never opens a trade by itself, only strengthens or vetoes a
   regime signal that already fired. A regime signal with no price-action
   read still trades (at a discount); one with agreeing price-action gets a
   bonus; one that price-action contradicts gets a heavy penalty instead of
   just averaging out to something misleadingly confident-looking.
3. Require a higher-timeframe EMA(50/200) bias check -- a mean-reversion
   "buy" on the 1m chart is worth much less if the 5m/15m trend is firmly
   down, so counter-bias signals are down-weighted rather than blocked
   outright (down-weighting keeps genuine reversals tradeable while still
   penalizing low-quality counter-trend entries).
4. Only emit a CALL/PUT when the combined score clears a configurable
   threshold -- this is what keeps signal frequency low and quality high,
   instead of firing on every candle.

Iteration history (see README.md's backtest table for the numbers): a first
version treated every strategy as an equal, independent vote and
backtested worse than ``trend_following`` traded alone, because a noisier
strategy (price-action) diluted a genuinely-edged one whenever they
disagreed. A second version fixed the averaging but still let price-action
fire *standalone* whenever the regime strategies were silent -- still
worse than trading trend_following alone, because those solo entries
weren't good enough to earn independent trigger rights. This version
requires a regime signal (trend_following or mean_reversion, whichever ADX
says applies) to fire first; price-action can only ever adjust its
confidence, never substitute for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from . import strategies as strat
from .strategies import DOWN, FLAT, UP, Signal

SOLO_PENALTY = 0.85  # a single strategy firing alone is discounted vs. two agreeing
AGREEMENT_BONUS = 1.15  # two strategies agreeing get a small confidence boost (capped at 1.0)
CONFLICT_PENALTY = 0.4  # strategies disagreeing on direction heavily discounts the louder side
COUNTER_BIAS_PENALTY = 0.5  # multiply score by this when a signal fights the higher-timeframe bias


@dataclass
class ConfluenceResult:
    direction: int
    score: float  # 0..1 confidence in the combined decision
    votes: list[Signal]
    bias: int
    threshold: float

    @property
    def is_signal(self) -> bool:
        return self.direction != FLAT and self.score >= self.threshold

    def describe(self) -> str:
        if not self.is_actionable_votes:
            return "no signal"
        parts = [f"{v.reason} (score {v.score:.2f})" for v in self.votes if v.direction == self.direction]
        return "; ".join(parts)

    @property
    def is_actionable_votes(self) -> bool:
        return any(v.is_actionable for v in self.votes)


def evaluate(
    df_entry: pd.DataFrame,
    df_bias: Optional[pd.DataFrame] = None,
    threshold: float = 0.55,
) -> ConfluenceResult:
    """Evaluate one closed candle on the entry timeframe.

    ``df_entry`` is the timeframe you actually trade (e.g. 1-minute candles).
    ``df_bias`` is an optional higher timeframe (e.g. 5-minute candles) used
    purely to establish trend direction and penalize counter-trend entries.
    """
    regime_vote = strat.trend_following(df_entry)
    if not regime_vote.is_actionable:
        regime_vote = strat.mean_reversion(df_entry)
    confirm_vote = strat.price_action(df_entry)

    bias = strat.higher_timeframe_bias(df_bias) if df_bias is not None else FLAT
    votes = [regime_vote, confirm_vote]

    # price_action is a *confirming* vote only -- it can strengthen or veto
    # a regime signal, but it never opens a trade on its own. Backtesting
    # showed that letting it fire standalone (whenever no regime signal was
    # present) dragged the combined engine below trend_following traded
    # alone, because price_action's solo hit rate isn't good enough to earn
    # independent trigger rights. See README.md's backtest table.
    if not regime_vote.is_actionable:
        return ConfluenceResult(FLAT, 0.0, votes, bias, threshold)

    if confirm_vote.is_actionable and confirm_vote.direction == regime_vote.direction:
        direction = regime_vote.direction
        score = min((regime_vote.score + confirm_vote.score) / 2 * AGREEMENT_BONUS, 1.0)
    elif confirm_vote.is_actionable:
        # Disagreement: trust the regime strategy (it's gated to the
        # market condition it was designed for) but penalize heavily --
        # a contradicting confirming vote is a real reason for caution.
        direction = regime_vote.direction
        score = regime_vote.score * CONFLICT_PENALTY
    else:
        direction, score = regime_vote.direction, regime_vote.score * SOLO_PENALTY

    if bias != FLAT and direction != bias:
        score *= COUNTER_BIAS_PENALTY

    return ConfluenceResult(direction, score, votes, bias, threshold)
