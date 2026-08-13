"""The battery. Each probe asks one question that could carry a real edge.

A probe is not a strategy. It is a measurable claim about the series, stated
so that it can come back negative. Everything returns an effect size, a
sample count and a p-value, and ``multiplicity.py`` then charges the whole
battery for the number of questions asked -- because running forty tests and
reporting the best one is how the ADX filter happened.

Ordered roughly by how much an edge would be worth if it were there:

1. ``variance_ratio``   is it a random walk at all?
2. ``autocorrelation``  does the next minute depend on the last?
3. ``streaks``          the classic OTC claim: does a run of N reverse?
4. ``extremes``         does an unusually large candle revert?
5. ``repeats``          does the generator repeat sequences it has used?
6. ``time_of_day``      is any minute or hour of the day directionally biased?
7. ``quantisation``     are prices drawn from a coarse grid?
8. ``cross_pair``       do pairs share innovations, so one leads another?

All of them score against the same target: the sign of the next 1-minute
close-to-close move, which is exactly what a 1-minute binary option pays on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats


@dataclass
class Finding:
    """One measured claim, with everything needed to judge it."""

    probe: str
    subject: str            # asset, or "ALL"
    detail: str             # which bucket/lag/threshold
    n: int
    win_rate: float         # fraction of correct directional calls
    baseline: float         # the drift on the same windows
    p_value: float
    effect: float = 0.0     # probe-specific magnitude
    note: str = ""

    @property
    def edge(self) -> float:
        return self.win_rate - self.baseline

    def beats(self, payout: float) -> bool:
        return self.win_rate > 1.0 / (1.0 + payout)

    def __str__(self) -> str:
        return (f"{self.probe:14s} {self.subject:12s} {self.detail:26s} "
                f"n={self.n:>6,} wr={self.win_rate:.4f} "
                f"base={self.baseline:.4f} edge={self.edge:+.4f} "
                f"p={self.p_value:.2e}")


def _returns(df: pd.DataFrame) -> pd.Series:
    return df["close"].astype(float).diff()


def _next_up(df: pd.DataFrame) -> pd.Series:
    """The target: did the next bar close above this one? Ties are dropped.

    A tie is refunded on Pocket Option, so it is neither a win nor a loss and
    must not sit in the denominator of a win rate.
    """
    nxt = df["close"].shift(-1) - df["close"]
    return nxt.where(nxt != 0).dropna().gt(0)


def _binom(wins: int, n: int, p: float) -> float:
    if n == 0:
        return 1.0
    return float(stats.binomtest(int(wins), int(n), p, alternative="two-sided").pvalue)


def _score(mask: pd.Series, target: pd.Series, direction: int) -> Optional[tuple]:
    """Win rate of calling ``direction`` whenever ``mask`` is true."""
    idx = target.index.intersection(mask[mask].index)
    if len(idx) == 0:
        return None
    up = target.loc[idx]
    wins = int(up.sum()) if direction > 0 else int((~up).sum())
    base_up = float(target.mean())
    baseline = base_up if direction > 0 else 1 - base_up
    return len(idx), wins / len(idx), baseline, _binom(wins, len(idx), baseline)


# --------------------------------------------------------------------------
# 1. Is it a random walk?
# --------------------------------------------------------------------------
def variance_ratio(df: pd.DataFrame, asset: str, lags=(2, 5, 10, 30, 60)) -> list[Finding]:
    """Lo-MacKinlay variance ratio.

    Under a random walk, variance scales linearly with the horizon, so
    VR(q) = 1. Below 1 is mean reversion -- the single most tradeable
    deviation for 1-minute binaries, because it says a move is more likely
    than not to give some of itself back. Above 1 is trending.

    This is the first question because a negative answer here means every
    pattern found later is decoration on a coin.
    """
    r = _returns(df).dropna()
    if len(r) < 300:
        return []
    var1 = r.var(ddof=1)
    out = []
    for q in lags:
        if len(r) < q * 30:
            continue
        agg = r.rolling(q).sum().dropna()
        vr = float(agg.var(ddof=1) / (q * var1)) if var1 > 0 else np.nan
        if not np.isfinite(vr):
            continue
        n = len(r)
        # Heteroskedasticity-robust standard error (Lo & MacKinlay 1988).
        se = np.sqrt(2.0 * (2 * q - 1) * (q - 1) / (3.0 * q * n))
        z = (vr - 1.0) / se if se > 0 else 0.0
        p = float(2 * (1 - stats.norm.cdf(abs(z))))
        out.append(Finding("variance_ratio", asset, f"q={q}", n, np.nan, 1.0, p,
                           effect=vr,
                           note=("mean-reverting" if vr < 1 else "trending")))
    return out


# --------------------------------------------------------------------------
# 2. Does the next minute depend on the last?
# --------------------------------------------------------------------------
def autocorrelation(df: pd.DataFrame, asset: str, max_lag: int = 60) -> list[Finding]:
    """Direct test of the tradeable question, lag by lag.

    Reported as a win rate rather than a correlation: "follow the sign of the
    bar k ago" is a strategy, whereas a correlation of -0.03 is a number
    nobody can act on.
    """
    target = _next_up(df)
    r = _returns(df)
    out = []
    for lag in range(1, max_lag + 1):
        prev = r.shift(lag - 1)
        follow = _score(prev > 0, target, +1)
        fade = _score(prev > 0, target, -1)
        if follow is None or follow[0] < 200:
            continue
        n, wr, base, p = follow if follow[1] >= fade[1] else fade
        side = "follow" if follow[1] >= fade[1] else "fade"
        out.append(Finding("autocorr", asset, f"lag {lag} {side}", n, wr, base, p,
                           effect=float(r.autocorr(lag) or 0.0)))
    return out


# --------------------------------------------------------------------------
# 3. The classic claim: streaks reverse
# --------------------------------------------------------------------------
def streaks(df: pd.DataFrame, asset: str, max_run: int = 8) -> list[Finding]:
    """After N consecutive same-direction candles, what happens next?

    This is the single most repeated claim in OTC signal groups, and it is
    directly measurable. On a true random walk P(next up) after any run is
    0.5; a generated series that reverts would show it decaying below.
    """
    r = _returns(df)
    sign = np.sign(r).replace(0, np.nan).ffill()
    run = sign.groupby((sign != sign.shift()).cumsum()).cumcount() + 1
    target = _next_up(df)
    out = []
    for k in range(2, max_run + 1):
        for direction, label in ((1, "up"), (-1, "down")):
            mask = (run >= k) & (sign == direction)
            cont = _score(mask, target, direction)
            if cont is None or cont[0] < 100:
                continue
            n, wr_cont, base, _ = cont
            # Report whichever side of the trade is better, honestly labelled.
            rev = _score(mask, target, -direction)
            if rev[1] > wr_cont:
                n, wr, base, p = rev
                verb = "reverses"
            else:
                n, wr, base, p = cont
                verb = "continues"
            out.append(Finding("streak", asset, f"{k}x {label} -> {verb}",
                               n, wr, base, p, effect=k))
    return out


# --------------------------------------------------------------------------
# 4. Does an unusually large candle revert?
# --------------------------------------------------------------------------
def extremes(df: pd.DataFrame, asset: str,
             quantiles=(0.90, 0.95, 0.99)) -> list[Finding]:
    """After an outsized bar, is the next one biased?

    A generator drawing from a bounded distribution has to come back; a real
    market does not have to. This is where that difference would show.
    """
    r = _returns(df)
    size = r.abs()
    target = _next_up(df)
    out = []
    for q in quantiles:
        threshold = size.quantile(q)
        for direction, label in ((1, "up"), (-1, "down")):
            mask = (size >= threshold) & (np.sign(r) == direction)
            for call, verb in ((-direction, "revert"), (direction, "extend")):
                sc = _score(mask, target, call)
                if sc is None or sc[0] < 100:
                    continue
                n, wr, base, p = sc
                out.append(Finding("extreme", asset,
                                   f"big {label} p{int(q * 100)} -> {verb}",
                                   n, wr, base, p, effect=float(threshold)))
    return out


# --------------------------------------------------------------------------
# 5. Does the generator repeat itself?
# --------------------------------------------------------------------------
def repeats(df: pd.DataFrame, asset: str, window: int = 8,
            tolerance: float = 0.15) -> list[Finding]:
    """Do past sequences of returns recur, and does what followed recur too?

    A generator with limited internal state must eventually revisit a state.
    If the continuation after a repeated pattern is also repeated, that is
    not a statistical edge -- it is a broken random number generator, and it
    would be the most valuable finding possible.

    Sequences are compared as normalised shapes so that the same pattern at a
    different volatility still matches.
    """
    r = _returns(df).dropna()
    if len(r) < window * 200:
        return []
    vals = r.to_numpy(dtype=float)
    scale = np.std(vals)
    if scale <= 0:
        return []
    shapes = np.lib.stride_tricks.sliding_window_view(vals, window) / scale
    nxt = np.sign(vals[window:])
    shapes = shapes[:len(nxt)]

    # Quantise the shape and look for exact bucket collisions: an O(n) proxy
    # for nearest-neighbour search that is honest about what "similar" means.
    keys = np.round(shapes / tolerance).astype(np.int16)
    flat = [k.tobytes() for k in keys]
    table: dict[bytes, list[int]] = {}
    for i, k in enumerate(flat):
        table.setdefault(k, []).append(i)

    groups = [v for v in table.values() if len(v) > 1]
    if not groups:
        return [Finding("repeat", asset, f"window {window}", len(flat),
                        np.nan, np.nan, 1.0, note="no sequence ever repeated")]

    agree = total = 0
    for members in groups:
        outcomes = nxt[members]
        outcomes = outcomes[outcomes != 0]
        if len(outcomes) < 2:
            continue
        majority = np.sign(outcomes.sum()) or 1
        agree += int((outcomes == majority).sum())
        total += len(outcomes)
    if total < 50:
        return [Finding("repeat", asset, f"window {window}", total, np.nan,
                        np.nan, 1.0, note="too few repeated sequences to test")]
    wr = agree / total
    p = _binom(agree, total, 0.5)
    return [Finding("repeat", asset, f"window {window} continuation",
                    total, wr, 0.5, p, effect=len(groups),
                    note=f"{len(groups):,} repeated shapes")]


# --------------------------------------------------------------------------
# 6. Time of day
# --------------------------------------------------------------------------
def time_of_day(df: pd.DataFrame, asset: str, by: str = "hour") -> list[Finding]:
    """Is any hour, or any minute of the hour, directionally biased?

    Minute-of-hour is included because a scheduled generator often has
    structure there, and because it is the kind of thing nobody looks for.
    """
    target = _next_up(df)
    if target.empty:
        return []
    key = (target.index.hour if by == "hour" else target.index.minute)
    out = []
    base = float(target.mean())
    for value in sorted(set(key)):
        mask = pd.Series(key == value, index=target.index)
        sub = target[mask]
        if len(sub) < 200:
            continue
        wr_up = float(sub.mean())
        direction, wr = (1, wr_up) if wr_up >= 0.5 else (-1, 1 - wr_up)
        baseline = base if direction > 0 else 1 - base
        wins = int(round(wr * len(sub)))
        out.append(Finding("time_of_day", asset, f"{by} {value:02d} "
                           f"{'up' if direction > 0 else 'down'}",
                           len(sub), wr, baseline, _binom(wins, len(sub), baseline),
                           effect=value))
    return out


# --------------------------------------------------------------------------
# 7. Is the price grid coarse?
# --------------------------------------------------------------------------
def quantisation(df: pd.DataFrame, asset: str) -> list[Finding]:
    """Are quotes drawn from a coarser grid than they appear to be?

    If closes only ever land on multiples of some step, the generator is
    discrete, and the distance to the next grid point becomes information
    about which way the next bar can move.
    """
    close = df["close"].dropna()
    if len(close) < 500:
        return []
    diffs = np.abs(np.diff(np.unique(np.round(close.to_numpy(), 8))))
    diffs = diffs[diffs > 0]
    if len(diffs) == 0:
        return []
    step = float(np.min(diffs))
    on_grid = float(np.mean(np.abs((close / step) - np.round(close / step)) < 1e-6))
    last_digit = (np.round(close / step).astype(np.int64) % 10)
    counts = np.bincount(last_digit, minlength=10)
    chi = stats.chisquare(counts)
    return [Finding("quantisation", asset, f"step {step:g}", len(close),
                    np.nan, np.nan, float(chi.pvalue), effect=on_grid,
                    note=f"last-digit uniformity chi2={chi.statistic:.1f}")]


# --------------------------------------------------------------------------
# 8. Do pairs share a generator?
# --------------------------------------------------------------------------
def cross_pair(panel: dict[str, pd.DataFrame], max_lag: int = 3) -> list[Finding]:
    """Does one pair's move predict another's, at a lag?

    Six OTC pairs priced by one engine may share innovations. If pair A at
    minute t predicts pair B at minute t+1, that is a genuinely tradeable
    lead-lag, and nothing about a real market has to be true for it to exist.
    """
    closes = {a: df["close"].astype(float) for a, df in panel.items() if len(df) > 500}
    if len(closes) < 2:
        return []
    rets = pd.DataFrame({a: s.diff() for a, s in closes.items()}).dropna(how="all")
    out = []
    for a in rets.columns:
        for b in rets.columns:
            if a == b:
                continue
            for lag in range(1, max_lag + 1):
                joined = pd.concat([rets[a].shift(lag), rets[b]], axis=1).dropna()
                if len(joined) < 500:
                    continue
                lead, follow = joined.iloc[:, 0], joined.iloc[:, 1]
                target = follow[follow != 0] > 0
                mask = lead.reindex(target.index) > 0
                sc = _score(mask, target, +1)
                sc2 = _score(mask, target, -1)
                if sc is None or sc[0] < 300:
                    continue
                n, wr, base, p = sc if sc[1] >= sc2[1] else sc2
                side = "same" if sc[1] >= sc2[1] else "opposite"
                out.append(Finding("cross_pair", f"{a}->{b}", f"lag {lag} {side}",
                                   n, wr, base, p,
                                   effect=float(joined.corr().iloc[0, 1])))
    return out
