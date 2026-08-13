"""The statistics that decide whether a pattern is real or is just noise.

This module exists because of one uncomfortable number. A binary option that
pays 92% on a win and takes 100% on a loss breaks even at a win rate of
1/1.92 = 52.08%, not 50%. Every edge in this project has to clear that line
*after* costs, and it has to clear it by enough that the result could not
plausibly have come from a coin.

Three things here, and they are the difference between research and
wishful thinking:

``binom_sf``        exact binomial tail probability, so "58% over 40 trades"
                    gets the p-value it deserves (0.16 -- pure noise) rather
                    than the excitement it invites.
``wilson_interval`` a confidence interval that stays honest at small n, where
                    the normal approximation famously does not.
``benjamini_hochberg``
                    the correction for having *looked at lots of patterns*.
                    Testing 500 candidate rules at p<0.05 yields ~25 winners
                    on pure random data. Without this step a mining run is a
                    machine for generating false discoveries, and every
                    over-fitted bot that ever lost money skipped it.

No scipy: the incomplete beta function is hand-rolled below, for the same
reason ``indicators.py`` hand-rolls RSI -- plain-pip installability on any
OS/arch.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

# ---------------------------------------------------------------------------
# Option economics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Payout:
    """Broker payout terms for a binary option.

    ``rate`` is the profit fraction on a win: 0.92 means a $100 stake returns
    $192 ($92 profit). A loss costs the full stake. A flat close (exit exactly
    equal to entry) is refunded by Pocket Option rather than lost, which is
    why the arithmetic below tracks refunds as their own bucket instead of
    folding them into losses.
    """

    rate: float = 0.92

    @property
    def breakeven(self) -> float:
        """Win rate (over *decided* trades) at which expectancy is exactly 0."""
        return 1.0 / (1.0 + self.rate)

    def expectancy(self, wins: int, losses: int, refunds: int = 0) -> float:
        """Expected profit per unit staked, averaged over every trade taken.

        Refunds dilute expectancy toward zero -- they cost nothing but they
        consume a slot -- so they belong in the denominator.
        """
        n = wins + losses + refunds
        if n == 0:
            return 0.0
        return (wins * self.rate - losses) / n

    def edge_over_breakeven(self, wins: int, losses: int) -> float:
        """Win-rate percentage points above the break-even line.

        Computed over decided trades only, because refunds are neither
        evidence for nor against the rule being right.
        """
        decided = wins + losses
        if decided == 0:
            return 0.0
        return wins / decided - self.breakeven


# ---------------------------------------------------------------------------
# Incomplete beta -> exact binomial tails
# ---------------------------------------------------------------------------


def _betacf(a: float, b: float, x: float, itmax: int = 400, eps: float = 3e-16) -> float:
    """Continued fraction for the incomplete beta, via the modified Lentz method."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0

    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d

    for m in range(1, itmax + 1):
        m2 = 2 * m

        # Even step.
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c

        # Odd step.
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta

        if abs(delta - 1.0) < eps:
            break

    return h


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """``I_x(a, b)``, the regularized incomplete beta function."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0

    log_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(log_beta + a * math.log(x) + b * math.log1p(-x))

    # The continued fraction converges fast only on one side of this pivot;
    # reflect to the other side when needed.
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def binom_sf(k: int, n: int, p: float) -> float:
    """``P(X >= k)`` for ``X ~ Binomial(n, p)``. Exact, via the beta identity.

    This is the one-sided p-value for "I won ``k`` of ``n``; could a rule with
    true win rate ``p`` have done that by luck?"
    """
    if n <= 0:
        return 1.0
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    return regularized_incomplete_beta(k, n - k + 1, p)


def binom_test_greater(wins: int, decided: int, p_null: float) -> float:
    """One-sided p-value against H0: true win rate <= ``p_null``.

    ``p_null`` should be the payout break-even, not 0.5. Beating a coin is
    not the bar; beating the payout is.
    """
    return binom_sf(wins, decided, p_null)


# ---------------------------------------------------------------------------
# Confidence intervals
# ---------------------------------------------------------------------------


def wilson_interval(
    successes: int, trials: int, z: float = 1.959963984540054
) -> tuple[float, float]:
    """Wilson score interval for a proportion (default 95%).

    Preferred over the textbook normal interval because it does not run off
    the end of [0, 1] and stays calibrated for the small samples that a
    per-hour, per-pair breakdown inevitably produces.
    """
    if trials <= 0:
        return (0.0, 1.0)

    phat = successes / trials
    z2 = z * z
    denom = 1.0 + z2 / trials
    center = (phat + z2 / (2 * trials)) / denom
    margin = (
        z
        / denom
        * math.sqrt(phat * (1.0 - phat) / trials + z2 / (4 * trials * trials))
    )
    return (max(0.0, center - margin), min(1.0, center + margin))


def normal_sf(z: float) -> float:
    """Upper-tail probability of the standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def two_sided_normal_p(z: float) -> float:
    return 2.0 * normal_sf(abs(z))


# ---------------------------------------------------------------------------
# Multiple-testing correction
# ---------------------------------------------------------------------------


def benjamini_hochberg(pvalues: Sequence[float], alpha: float = 0.05) -> list[bool]:
    """Benjamini-Hochberg FDR control. Returns a keep/discard mask.

    Controls the *expected proportion of false positives among the rules you
    keep* at ``alpha``. This is the right correction for pattern mining:
    Bonferroni would be so strict that a genuine, modest edge could never
    clear it, while no correction at all guarantees a pile of ghosts.
    """
    m = len(pvalues)
    if m == 0:
        return []

    order = sorted(range(m), key=lambda i: pvalues[i])
    keep = [False] * m

    # Largest rank k whose p-value falls under its BH threshold; everything
    # ranked at or below k is kept.
    cutoff_rank = 0
    for rank, idx in enumerate(order, start=1):
        if pvalues[idx] <= alpha * rank / m:
            cutoff_rank = rank

    for rank, idx in enumerate(order, start=1):
        if rank <= cutoff_rank:
            keep[idx] = True
    return keep


def bh_qvalues(pvalues: Sequence[float]) -> list[float]:
    """Per-hypothesis BH q-values (monotone-adjusted p-values)."""
    m = len(pvalues)
    if m == 0:
        return []

    order = sorted(range(m), key=lambda i: pvalues[i])
    q = [0.0] * m
    running_min = 1.0
    for rank in range(m, 0, -1):
        idx = order[rank - 1]
        running_min = min(running_min, pvalues[idx] * m / rank)
        q[idx] = min(1.0, running_min)
    return q


# ---------------------------------------------------------------------------
# How much data would settle the question?
# ---------------------------------------------------------------------------


def required_trades(
    true_rate: float,
    null_rate: float,
    alpha: float = 0.05,
    power: float = 0.80,
) -> int:
    """Trades needed to detect ``true_rate`` against ``null_rate``.

    The reality check nobody runs before deploying. Detecting a 55% edge
    against a 52.08% break-even at 80% power needs roughly 1,500 trades --
    which at a handful of signals a day is months. Any claim of an edge
    backed by 60 trades is, statistically, a claim backed by nothing.
    """
    if true_rate <= null_rate:
        return -1  # not detectable: the "edge" is on the wrong side of the line

    # Normal approximation to the binomial (one-sided).
    z_alpha = _inv_norm_sf(alpha)
    z_beta = _inv_norm_sf(1.0 - power)
    p0, p1 = null_rate, true_rate
    numerator = (
        z_alpha * math.sqrt(p0 * (1 - p0)) + z_beta * math.sqrt(p1 * (1 - p1))
    ) ** 2
    return int(math.ceil(numerator / ((p1 - p0) ** 2)))


def _inv_norm_sf(p: float) -> float:
    """Inverse survival function of the standard normal (Acklam's algorithm)."""
    if p <= 0.0:
        return float("inf")
    if p >= 1.0:
        return float("-inf")

    # Solve for the quantile of (1 - p), then use symmetry.
    return -_inv_norm_cdf(p)


def _inv_norm_cdf(p: float) -> float:
    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    ]
    d = [
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    ]

    p_low, p_high = 0.02425, 1.0 - 0.02425

    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p > p_high:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(
            ((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]
        ) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)

    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
    )


def mean(values: Iterable[float]) -> float:
    vals = list(values)
    return sum(vals) / len(vals) if vals else 0.0
