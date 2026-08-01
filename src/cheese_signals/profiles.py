"""Strategy profiles: how the engine weights its strategies per instrument type.

Why this exists
---------------
Pocket Option OTC pairs are not real markets. Their quotes are generated
algorithmically from historical volatility models and run 24/7, independent
of any exchange feed. That has direct, non-cosmetic consequences for which
strategies should be trusted:

* **Trend-following is weaker on OTC.** Real trends exist because real
  order flow persists in one direction. A volatility-model generator has no
  such mechanism, and synthetic feeds are widely reported to revert and to
  repeat patterns rather than to trend durably. Trend continuation signals
  are therefore demoted on OTC.
* **Mean-reversion is stronger on OTC**, for the same reason -- a generator
  drawing around a modelled distribution spends more time returning to its
  mean than a real market does.
* **The liquidity-sweep pattern still applies, but not for the usual
  reason.** On real markets it works because institutions hunt resting stop
  orders. On OTC there are no resting stops to hunt, so the "smart money"
  rationale is simply false. The *shape* (wick through a level, close back
  inside, with displacement) remains a good exhaustion signal on a
  mean-reverting series, so it is kept -- and on OTC it is treated as a
  mean-reversion/exhaustion setup, which is the honest description.

None of this is settled fact about your specific broker feed. It's the
starting prior. The whole point of the outcome journal and the analytics tab
is that you can check it: if your logged OTC results show trend-following
beating mean-reversion, change the profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StrategyProfile:
    name: str
    weights: dict[str, float]
    notes: str
    # Setups whose score gets an extra multiplier because the instrument
    # type suits them.
    adx_trend_min: float = 20.0
    adx_range_max: float = 20.0


OTC_PROFILE = StrategyProfile(
    name="otc",
    weights={
        "liquidity_sweep": 1.0,   # exhaustion pattern; the primary OTC setup
        "mean_reversion": 0.95,   # favoured: synthetic feeds revert
        "price_action": 0.70,     # confirming only
        "trend": 0.55,            # demoted: synthetic feeds trend poorly
    },
    notes=(
        "Broker-generated 24/7 synthetic feed. Favours reversion and repeating "
        "patterns; genuine trend persistence is weak. Trend signals demoted."
    ),
    # Require a *stronger* ADX before believing an OTC trend at all.
    adx_trend_min=25.0,
    adx_range_max=22.0,
)

LIVE_PROFILE = StrategyProfile(
    name="live",
    weights={
        "liquidity_sweep": 1.0,   # real stop-hunt mechanism applies here
        "trend": 1.0,
        "mean_reversion": 0.85,
        "price_action": 0.70,
    },
    notes="Real exchange-fed instrument. Standard weighting; trends are real.",
    adx_trend_min=20.0,
    adx_range_max=20.0,
)


def profile_for(asset: str) -> StrategyProfile:
    return OTC_PROFILE if asset.lower().endswith("_otc") else LIVE_PROFILE
