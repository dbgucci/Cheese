"""Is this feed predictable at all? Run this before mining anything.

A Pocket Option OTC symbol is not a market. When the underlying spot market
is closed, "EUR/USD OTC" is a price series Pocket Option generates; the
broker is the counterparty, the feed author, and the setter of the payout at
the same time. That does not automatically make it unbeatable -- a generator
with any memory in it leaves a footprint -- but it does mean the first
question is empirical, not technical: **does tomorrow's tick know anything
about today's?**

If the answer is no, then no indicator, no confluence stack and no neural net
can produce a win rate above 50%, and at a 92% payout every trade has an
expectancy of -4.2%. Mining patterns on such a feed produces beautiful
backtests and nothing else. So this battery runs first and its verdict
governs how the rest of the report should be read.

Four independent tests, chosen because they fail in different ways:

``autocorrelation``  linear memory in returns at lags 1..N.
``sign_persistence`` does an up candle predict another up candle?
``runs_test``        is the up/down sequence streakier (or choppier) than
                     chance? Catches structure that lag-1 correlation misses.
``variance_ratio``   Lo-MacKinlay. The standard random-walk test: under a
                     true random walk, variance grows linearly with horizon,
                     so VR(q) = 1 at every q. Mean reversion pushes it below
                     1, trending above.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from .stats import normal_sf, two_sided_normal_p


@dataclass
class TestResult:
    name: str
    statistic: float
    pvalue: float
    detail: str = ""

    @property
    def significant(self) -> bool:
        return self.pvalue < 0.05


@dataclass
class PredictabilityReport:
    asset: str
    n_bars: int
    tests: list[TestResult] = field(default_factory=list)

    @property
    def any_structure(self) -> bool:
        return any(t.significant for t in self.tests)

    @property
    def verdict(self) -> str:
        hits = [t for t in self.tests if t.significant]
        if not hits:
            return (
                "No detectable structure. This feed is statistically "
                "indistinguishable from a random walk, so no rule mined from "
                "it should be expected to hold live."
            )
        return (
            f"{len(hits)} of {len(self.tests)} tests reject randomness "
            f"({', '.join(t.name for t in hits)}). Worth mining -- but the "
            "effect still has to survive out-of-sample and clear the payout."
        )


def log_returns(close: pd.Series) -> np.ndarray:
    """Log returns with non-finite and zero-price rows dropped."""
    c = pd.to_numeric(close, errors="coerce").astype(float)
    c = c[c > 0]
    r = np.diff(np.log(c.to_numpy()))
    return r[np.isfinite(r)]


def autocorrelation(returns: np.ndarray, max_lag: int = 10) -> list[TestResult]:
    """Sample autocorrelation at lags 1..max_lag, with Bartlett standard errors."""
    n = len(returns)
    out: list[TestResult] = []
    if n < 50:
        return out

    x = returns - returns.mean()
    denom = float(np.sum(x * x))
    if denom <= 0:
        return out

    se = 1.0 / math.sqrt(n)  # asymptotic SE under the white-noise null
    for lag in range(1, min(max_lag, n // 4) + 1):
        rho = float(np.sum(x[lag:] * x[:-lag]) / denom)
        z = rho / se
        out.append(
            TestResult(
                name=f"autocorr(lag={lag})",
                statistic=rho,
                pvalue=two_sided_normal_p(z),
                detail=f"rho={rho:+.4f}, z={z:+.2f}",
            )
        )
    return out


def sign_persistence(close: pd.Series) -> Optional[TestResult]:
    """P(next candle same direction as this one) against a 50% null.

    The single most directly tradeable form of memory: if it is real and
    large enough, it *is* a strategy.
    """
    c = pd.to_numeric(close, errors="coerce").astype(float).dropna()
    diffs = np.diff(c.to_numpy())
    signs = np.sign(diffs)
    signs = signs[signs != 0]  # flat candles carry no directional information
    if len(signs) < 50:
        return None

    same = int(np.sum(signs[1:] == signs[:-1]))
    n = len(signs) - 1
    phat = same / n
    z = (phat - 0.5) / math.sqrt(0.25 / n)
    return TestResult(
        name="sign_persistence",
        statistic=phat,
        pvalue=two_sided_normal_p(z),
        detail=f"P(same direction)={phat:.4f} over {n:,} pairs, z={z:+.2f}",
    )


def runs_test(close: pd.Series) -> Optional[TestResult]:
    """Wald-Wolfowitz runs test on the up/down sequence.

    Counts how many times the direction flips. Too few runs means trends
    persist; too many means the feed alternates. Either is exploitable
    structure that a lag-1 correlation can miss when the effect is
    conditional rather than linear.
    """
    c = pd.to_numeric(close, errors="coerce").astype(float).dropna()
    signs = np.sign(np.diff(c.to_numpy()))
    signs = signs[signs != 0]
    n = len(signs)
    if n < 50:
        return None

    n_up = int(np.sum(signs > 0))
    n_down = n - n_up
    if n_up == 0 or n_down == 0:
        return None

    runs = 1 + int(np.sum(signs[1:] != signs[:-1]))
    expected = 1.0 + 2.0 * n_up * n_down / n
    variance = (
        2.0 * n_up * n_down * (2.0 * n_up * n_down - n) / (n * n * (n - 1.0))
    )
    if variance <= 0:
        return None

    z = (runs - expected) / math.sqrt(variance)
    return TestResult(
        name="runs_test",
        statistic=float(runs),
        pvalue=two_sided_normal_p(z),
        detail=f"runs={runs:,} vs {expected:,.1f} expected, z={z:+.2f}",
    )


def variance_ratio(returns: np.ndarray, q: int = 2) -> Optional[TestResult]:
    """Lo-MacKinlay variance ratio test, heteroskedasticity-robust.

    Under a random walk VR(q) = 1 for every horizon q. The robust z-statistic
    is used rather than the homoskedastic one because volatility on these
    feeds clusters, and the naive version rejects randomness far too often
    when it does -- which would hand back a false "there's an edge here".
    """
    n = len(returns)
    if n < 100 or q < 2:
        return None

    mu = float(np.mean(returns))
    x = returns - mu

    var_1 = float(np.sum(x * x)) / (n - 1)
    if var_1 <= 0:
        return None

    # q-period overlapping returns.
    cumsum = np.cumsum(returns)
    q_returns = cumsum[q - 1 :] - np.concatenate(([0.0], cumsum[:-q]))
    m = q * (n - q + 1) * (1.0 - q / n)
    if m <= 0:
        return None
    var_q = float(np.sum((q_returns - q * mu) ** 2)) / m

    vr = var_q / var_1

    # Heteroskedasticity-robust variance of VR (Lo & MacKinlay 1988, eq. 2.20).
    x2 = x * x
    denom = float(np.sum(x2)) ** 2
    if denom <= 0:
        return None

    # delta_j = sum(x_t^2 * x_{t-j}^2) / (sum x_t^2)^2  -- Lo & MacKinlay 1988.
    # The denominator is already the *squared* sum of squares, so delta_j
    # carries its own 1/n and must not be rescaled again.
    theta = 0.0
    for j in range(1, q):
        delta_j = float(np.sum(x2[j:] * x2[:-j])) / denom
        theta += ((2.0 * (q - j) / q) ** 2) * delta_j
    if theta <= 0:
        return None

    z = (vr - 1.0) / math.sqrt(theta)
    direction = "mean-reverting" if vr < 1 else "trending"
    return TestResult(
        name=f"variance_ratio(q={q})",
        statistic=vr,
        pvalue=two_sided_normal_p(z),
        detail=f"VR={vr:.4f} ({direction} if significant), z={z:+.2f}",
    )


def analyze(close: pd.Series, asset: str = "", max_lag: int = 10) -> PredictabilityReport:
    """Run the full battery on one asset's close series."""
    returns = log_returns(close)
    report = PredictabilityReport(asset=asset, n_bars=int(len(close)))

    report.tests.extend(autocorrelation(returns, max_lag=max_lag))

    for test in (sign_persistence(close), runs_test(close)):
        if test is not None:
            report.tests.append(test)

    for q in (2, 4, 8, 16):
        vr = variance_ratio(returns, q=q)
        if vr is not None:
            report.tests.append(vr)

    return report
