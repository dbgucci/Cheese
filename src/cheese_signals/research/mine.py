"""Systematic pattern search with the anti-fooling machinery wired in.

The search itself is the easy part: enumerate conditions over the features,
score a 1-bar binary option under each, rank by win rate. Anyone can do that
in an afternoon, and the result is always a list of spectacular-looking rules
that die on contact with a live account.

What makes this module worth running is the three gates every candidate has
to pass before it is reported as a finding:

1. **Break-even, not 50%.** A rule is scored against the payout's break-even
   win rate (52.08% at 92%), because that is the line between making money
   and donating it.

2. **Multiple-testing correction.** The miner tests thousands of conditions.
   At p<0.05, thousands of *pure noise* conditions produce dozens of
   "significant" hits. Benjamini-Hochberg puts a leash on that, and the
   report states plainly how many candidates were examined.

3. **Out-of-sample confirmation.** Candidates are found on the training split
   only, then re-scored on validation, and only survivors are measured once
   on a test split that nothing touched. The gap between the training win
   rate and the test win rate *is* the overfitting, measured rather than
   assumed.

A rule that clears all three on a decent sample is worth a demo account. A
rule that clears only the first is a coincidence with good marketing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from . import features as feat
from .stats import (
    Payout,
    benjamini_hochberg,
    bh_qvalues,
    binom_test_greater,
    required_trades,
    wilson_interval,
)

# A rule seen fewer times than this cannot be evaluated, no matter how good
# it looks. 200 decided trades still only pins a win rate to about +/-7%.
MIN_SAMPLES = 200


@dataclass
class Score:
    """How a rule performed over one split."""

    n: int = 0
    wins: int = 0
    losses: int = 0
    refunds: int = 0

    @property
    def decided(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        return self.wins / self.decided if self.decided else 0.0

    def expectancy(self, payout: Payout) -> float:
        return payout.expectancy(self.wins, self.losses, self.refunds)

    def interval(self) -> tuple[float, float]:
        return wilson_interval(self.wins, self.decided)


@dataclass
class Candidate:
    """One mined rule: a condition, a direction, and its scores per split."""

    feature: str
    bucket: str
    direction: int
    train: Score
    validate: Score = field(default_factory=Score)
    test: Score = field(default_factory=Score)
    pvalue: float = 1.0
    qvalue: float = 1.0
    survived_fdr: bool = False
    confirmed_oos: bool = False

    @property
    def name(self) -> str:
        side = "CALL" if self.direction > 0 else "PUT"
        return f"{self.feature}={self.bucket} -> {side}"

    @property
    def decay(self) -> float:
        """Training win rate minus test win rate: the overfitting, in points."""
        return self.train.win_rate - self.test.win_rate


def _mask_for(values: pd.Series, bucket: str) -> np.ndarray:
    """Boolean mask for one bucket.

    Comparing a nullable-string column yields NA wherever the feature was
    undefined (indicator warm-up, gaps). NA means "this bar is not in the
    bucket" for selection purposes, so it collapses to False rather than
    propagating into an object-dtype array that cannot index.
    """
    return (values == bucket).fillna(False).to_numpy(dtype=bool)


def _score(outcomes: np.ndarray, direction: int) -> Score:
    """Score a set of 1-bar outcomes traded in a fixed direction."""
    outcomes = outcomes[~np.isnan(outcomes)]
    wins = int(np.sum(outcomes == direction))
    refunds = int(np.sum(outcomes == 0))
    losses = int(len(outcomes) - wins - refunds)
    return Score(n=len(outcomes), wins=wins, losses=losses, refunds=refunds)


def _split_indices(n: int, train: float, validate: float) -> tuple[slice, slice, slice]:
    """Chronological three-way split. Never random: time order is the point."""
    a = int(n * train)
    b = int(n * (train + validate))
    return slice(0, a), slice(a, b), slice(b, n)


@dataclass
class MiningReport:
    asset: str
    n_bars: int
    payout: Payout
    n_candidates_tested: int = 0
    n_passed_significance: int = 0
    findings: list[Candidate] = field(default_factory=list)
    split_sizes: tuple[int, int, int] = (0, 0, 0)

    @property
    def confirmed(self) -> list[Candidate]:
        return [c for c in self.findings if c.confirmed_oos]

    @property
    def verdict(self) -> str:
        if not self.findings:
            return (
                f"{self.n_candidates_tested:,} rules tested, none survived "
                "multiple-testing correction on the training split. No edge found."
            )
        if not self.confirmed:
            return (
                f"{len(self.findings)} rule(s) survived correction in training "
                "but none held out of sample -- the classic overfitting "
                "signature. No edge found."
            )
        return (
            f"{len(self.confirmed)} rule(s) survived correction AND held out "
            "of sample. Candidates for demo-account validation, not for money."
        )


def mine_asset(
    candles: pd.DataFrame,
    asset: str = "",
    payout: Optional[Payout] = None,
    n_bins: int = 5,
    alpha: float = 0.05,
    min_samples: int = MIN_SAMPLES,
    train_frac: float = 0.60,
    validate_frac: float = 0.20,
    pair_features: bool = True,
    min_oos_samples: int = 30,
) -> MiningReport:
    """Mine one asset end to end: features, split, search, correct, confirm."""
    payout = payout or Payout()
    report = MiningReport(asset=asset, n_bars=len(candles), payout=payout)

    if len(candles) < min_samples * 3:
        return report

    raw = feat.clip_streak(feat.compute(candles))
    outcomes = feat.label(candles).to_numpy(dtype="float64")

    tr, va, te = _split_indices(len(candles), train_frac, validate_frac)
    report.split_sizes = (
        tr.stop - tr.start,
        va.stop - va.start,
        te.stop - te.start,
    )

    # Bin edges come from training data only -- see features.fit_bins.
    specs = feat.fit_bins(raw.iloc[tr], n_bins=n_bins)
    binned = feat.apply_bins(raw, specs)

    if pair_features:
        binned = _add_pairs(binned)

    candidates: list[Candidate] = []

    for column in binned.columns:
        values = binned[column]
        # Only consider buckets that are common enough in *training* to be
        # measurable; rare buckets are where spurious 100% win rates live.
        counts = values.iloc[tr].value_counts()
        for bucket in counts[counts >= min_samples].index:
            mask = _mask_for(values, bucket)

            for direction in (1, -1):
                train_score = _score(outcomes[tr][mask[tr]], direction)
                if train_score.decided < min_samples:
                    continue
                # Only test rules that are actually on the profitable side of
                # break-even; testing losers doubles the correction burden for
                # nothing.
                if train_score.win_rate <= payout.breakeven:
                    continue

                p = binom_test_greater(
                    train_score.wins, train_score.decided, payout.breakeven
                )
                candidates.append(
                    Candidate(
                        feature=column,
                        bucket=str(bucket),
                        direction=direction,
                        train=train_score,
                        pvalue=p,
                    )
                )

    # The denominator for the correction is every rule *examined*, including
    # the ones discarded above for being below break-even. Reporting only the
    # tested subset would understate the search and weaken the correction.
    report.n_candidates_tested = _count_examined(binned, tr, min_samples)

    if not candidates:
        return report

    pvals = [c.pvalue for c in candidates]
    keep = benjamini_hochberg(pvals, alpha=alpha)
    qvals = bh_qvalues(pvals)
    for candidate, kept, q in zip(candidates, keep, qvals):
        candidate.survived_fdr = kept
        candidate.qvalue = q

    survivors = [c for c in candidates if c.survived_fdr]
    report.n_passed_significance = len(survivors)

    # Out-of-sample: score survivors on validation, then once on test.
    for candidate in survivors:
        mask = _mask_for(binned[candidate.feature], candidate.bucket)
        candidate.validate = _score(outcomes[va][mask[va]], candidate.direction)
        candidate.test = _score(outcomes[te][mask[te]], candidate.direction)
        # "Confirmed" has to mean what the report says it means. A point
        # estimate above break-even on 40 trades is not a confirmation, so
        # the *lower* bound of the test-split interval must clear the line
        # too -- otherwise the label promises more than the data supports.
        test_lower, _ = candidate.test.interval()
        candidate.confirmed_oos = (
            candidate.validate.win_rate > payout.breakeven
            and candidate.test.win_rate > payout.breakeven
            and candidate.validate.decided >= min_oos_samples
            and candidate.test.decided >= min_oos_samples
            and test_lower > payout.breakeven
        )

    survivors.sort(key=lambda c: c.test.expectancy(payout), reverse=True)
    report.findings = survivors
    return report


def _count_examined(binned: pd.DataFrame, tr: slice, min_samples: int) -> int:
    """Total (bucket, direction) hypotheses the search looked at."""
    total = 0
    for column in binned.columns:
        counts = binned[column].iloc[tr].value_counts()
        total += int((counts >= min_samples).sum()) * 2
    return total


def _add_pairs(binned: pd.DataFrame, max_features: int = 8) -> pd.DataFrame:
    """Two-feature conjunctions, e.g. ``hour=14 AND streak=-3``.

    Interaction effects are where a real edge on a generated feed would most
    plausibly hide -- "this shape, at this hour" -- but each pair multiplies
    the hypothesis count, which is exactly what the FDR correction is there
    to absorb. Kept to a curated subset so the search stays interpretable.
    """
    interesting = [
        c
        for c in ("hour", "streak", "direction", "vol_pctile", "body_frac", "rsi14")
        if c in binned.columns
    ][:max_features]

    out = binned.copy()
    for i, a in enumerate(interesting):
        for b in interesting[i + 1 :]:
            out[f"{a}&{b}"] = binned[a].astype("string") + "|" + binned[b].astype("string")
    return out


def sample_size_note(payout: Payout, target_rate: float = 0.55) -> str:
    """Plain-language statement of how much data a claimed edge would need."""
    n = required_trades(target_rate, payout.breakeven)
    if n < 0:
        return (
            f"A {target_rate:.1%} win rate is below the {payout.breakeven:.2%} "
            "break-even -- it loses money however many trades you take."
        )
    return (
        f"Detecting a {target_rate:.1%} win rate against a "
        f"{payout.breakeven:.2%} break-even at 80% power needs ~{n:,} decided "
        "trades. Anything claimed on less is noise."
    )
