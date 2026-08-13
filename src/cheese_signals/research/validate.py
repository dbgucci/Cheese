"""What separates a finding from a coincidence.

The ADX filter is the cautionary tale this module exists for. It read 55.4%
on 815 trades with a Fisher p of 0.002 -- a real, correctly computed p-value.
It was still noise, and it cost roughly six points of win rate when it was
put into production, because two things were never done:

* it was never charged for the number of hypotheses tried alongside it;
* it was never checked on data that had not been looked at.

So nothing here reports a discovery. It reports a *candidate*, and a
candidate becomes a finding only by surviving, in order:

1. **Benjamini-Hochberg** across every probe in the run. Forty questions at
   p<0.05 produce two "significant" answers from pure noise.
2. **A holdout** the search never touched.
3. **Stability** across a split of the holdout, so an effect carried by one
   afternoon is visible as such.
4. **The payout**, which is the only threshold that pays: 52.08% at 0.92,
   54.05% at 0.85. Statistical significance below break-even is a precisely
   measured way to lose money.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Iterable, Optional

import numpy as np
import pandas as pd
from scipy import stats

from .probes import Finding


def breakeven(payout: float) -> float:
    return 1.0 / (1.0 + payout)


def benjamini_hochberg(findings: list[Finding], alpha: float = 0.05) -> list[Finding]:
    """Mark which findings survive once the whole battery is charged for.

    Controls the false discovery rate rather than the family-wise error rate:
    with hundreds of probes, Bonferroni would reject everything including
    anything real, and the goal is a shortlist to test out of sample, not a
    single certainty.
    """
    usable = [f for f in findings if np.isfinite(f.p_value)]
    if not usable:
        return []
    order = sorted(usable, key=lambda f: f.p_value)
    m = len(order)
    survivors, largest = [], -1
    for i, f in enumerate(order, start=1):
        if f.p_value <= alpha * i / m:
            largest = i
    for i, f in enumerate(order, start=1):
        if i <= largest:
            survivors.append(replace(f, note=(f.note + " | passed BH").strip(" |")))
    return survivors


@dataclass
class Split:
    """A time-ordered split. Never random: adjacent minutes are not independent."""

    train: dict[str, pd.DataFrame]
    test: dict[str, pd.DataFrame]
    boundary: pd.Timestamp

    def describe(self) -> str:
        tr = sum(len(v) for v in self.train.values())
        te = sum(len(v) for v in self.test.values())
        return (f"train {tr:,} bars up to {self.boundary:%Y-%m-%d %H:%M}, "
                f"test {te:,} bars after")


def split_by_time(panel: dict[str, pd.DataFrame], test_fraction: float = 0.35) -> Split:
    """Split every asset at the same wall-clock instant.

    Splitting each asset at its own quantile would let a pattern be trained
    on Tuesday for one pair and tested on Tuesday for another, which quietly
    reintroduces the lookahead the split exists to prevent.
    """
    stamps = pd.DatetimeIndex(sorted({ts for df in panel.values() for ts in df.index}))
    if len(stamps) < 10:
        raise ValueError("not enough history to split")
    boundary = stamps[int(len(stamps) * (1 - test_fraction))]
    train = {a: df[df.index < boundary] for a, df in panel.items()}
    test = {a: df[df.index >= boundary] for a, df in panel.items()}
    return Split(train, test, boundary)


@dataclass
class Verdict:
    """One candidate, carried all the way through."""

    candidate: Finding
    test_n: int = 0
    test_win_rate: float = float("nan")
    test_p: float = float("nan")
    half_a: float = float("nan")
    half_b: float = float("nan")
    survived: bool = False
    reason: str = ""

    def line(self, payout: float) -> str:
        be = breakeven(payout)
        return (f"{'PASS' if self.survived else 'fail'}  "
                f"{self.candidate.probe:12s} {self.candidate.subject:12s} "
                f"{self.candidate.detail:24s} "
                f"train {self.candidate.win_rate:.4f} -> "
                f"test {self.test_win_rate:.4f} (n={self.test_n:,}) "
                f"be={be:.4f}  {self.reason}")


def confirm(
    candidate: Finding,
    replay: Callable[[Finding, dict[str, pd.DataFrame]], Optional[tuple[int, int]]],
    test_panel: dict[str, pd.DataFrame],
    payout: float,
    min_n: int = 200,
) -> Verdict:
    """Re-run one candidate on the holdout and judge it.

    ``replay`` takes the finding and the untouched data and returns
    (wins, n) -- the same rule applied to bars the search never saw.
    """
    v = Verdict(candidate)
    got = replay(candidate, test_panel)
    if not got:
        v.reason = "could not be replayed on the holdout"
        return v
    wins, n = got
    v.test_n = n
    if n < min_n:
        v.reason = f"only {n} holdout samples, needs {min_n}"
        return v
    v.test_win_rate = wins / n
    v.test_p = float(stats.binomtest(wins, n, 0.5, alternative="greater").pvalue)

    be = breakeven(payout)
    if v.test_win_rate <= be:
        v.reason = f"below the {be:.2%} break-even out of sample"
        return v
    if v.test_p > 0.05:
        v.reason = f"not significant out of sample (p={v.test_p:.3f})"
        return v
    v.survived = True
    v.reason = "held out of sample"
    return v


def stability(series: pd.Series, halves: int = 2) -> list[float]:
    """Win rate in each equal slice of time, to expose a one-afternoon effect."""
    if series.empty:
        return []
    chunks = np.array_split(series.to_numpy(), halves)
    return [float(c.mean()) for c in chunks if len(c)]


def report(verdicts: list[Verdict], payout: float, tried: int) -> str:
    be = breakeven(payout)
    passed = [v for v in verdicts if v.survived]
    lines = [
        "VALIDATION",
        f"  {tried:,} hypotheses tested",
        f"  {len(verdicts)} survived multiplicity correction and reached the holdout",
        f"  {len(passed)} survived the holdout",
        f"  break-even at a {payout:.0%} payout is {be:.2%}",
        "",
    ]
    for v in sorted(verdicts, key=lambda x: (-x.survived, -(x.test_win_rate or 0))):
        lines.append("  " + v.line(payout))
    if not passed:
        lines += [
            "",
            "  Nothing survived. That is a result, not a failure to find one:",
            "  it means the series gave up no exploitable structure at this",
            "  horizon, on this much data, at this payout.",
        ]
    return "\n".join(lines)
