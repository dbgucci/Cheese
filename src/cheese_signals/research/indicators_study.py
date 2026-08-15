"""The classic indicators, tested exhaustively and honestly.

RSI, Bollinger Bands and MACD are the three most widely used tools in
technical analysis, and they had not been tested here. The earlier battery
looked for structure in the *series*; this looks for structure in the
*indicators* -- which is a different question and deserves its own answer.

Everything anyone actually trades is included:

* each indicator alone, across a grid of thresholds rather than one
  conventional setting, so "RSI 30" failing is not confused with "RSI
  failing";
* the textbook single rules (oversold, band touch, MACD cross);
* **every pair and triple of conditions**, because the usual defence of a
  failed indicator is that it needs confluence -- so confluence is measured
  rather than argued about.

That produces thousands of hypotheses, which is exactly why the multiplicity
correction matters. A search this wide will always produce something that
looks good on the training slice. The only question that counts is whether
it still looks good on data the search never touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Callable, Iterable, Optional

import numpy as np
import pandas as pd
from scipy import stats

from .probes import Finding


# --------------------------------------------------------------------------
# the indicators, computed from raw OHLC
# --------------------------------------------------------------------------
def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def bollinger(close: pd.Series, period: int = 20, std: float = 2.0) -> pd.DataFrame:
    mid = close.rolling(period).mean()
    sd = close.rolling(period).std(ddof=0)
    upper, lower = mid + std * sd, mid - std * sd
    width = (upper - lower).replace(0, np.nan)
    return pd.DataFrame({
        "bb_mid": mid, "bb_upper": upper, "bb_lower": lower,
        "bb_pct_b": (close - lower) / width,          # 0 = lower band, 1 = upper
        "bb_width": (upper - lower) / mid,            # volatility, normalised
    })


def macd(close: pd.Series, fast: int = 12, slow: int = 26,
         signal: int = 9) -> pd.DataFrame:
    ef = close.ewm(span=fast, adjust=False).mean()
    es = close.ewm(span=slow, adjust=False).mean()
    line = ef - es
    sig = line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"macd": line, "macd_signal": sig,
                         "macd_hist": line - sig})


def build(df: pd.DataFrame) -> pd.DataFrame:
    """Every indicator, in the parameterisations people actually use."""
    close = df["close"].astype(float)
    out = pd.DataFrame(index=df.index)

    for p in (7, 9, 14, 21):
        out[f"rsi{p}"] = rsi(close, p)

    for p, s in ((20, 2.0), (20, 2.5), (14, 2.0), (50, 2.0)):
        b = bollinger(close, p, s)
        tag = f"{p}_{s:g}"
        out[f"bb_pct_b_{tag}"] = b["bb_pct_b"]
        out[f"bb_width_{tag}"] = b["bb_width"]

    for f, sl, sg in ((12, 26, 9), (5, 13, 5), (8, 21, 5)):
        m = macd(close, f, sl, sg)
        tag = f"{f}_{sl}_{sg}"
        out[f"macd_hist_{tag}"] = m["macd_hist"]
        out[f"macd_cross_{tag}"] = np.sign(m["macd"] - m["macd_signal"])

    atr = (df["high"] - df["low"]).rolling(14).mean().replace(0, np.nan)
    for p in (9, 21, 50, 200):
        ema = close.ewm(span=p, adjust=False).mean()
        out[f"ema{p}_dist"] = (close - ema) / atr
        out[f"ema{p}_slope"] = ema.diff(5) / atr

    out["ema9_21_cross"] = np.sign(
        close.ewm(span=9, adjust=False).mean()
        - close.ewm(span=21, adjust=False).mean())
    return out


# --------------------------------------------------------------------------
# conditions: a named boolean over the frame, plus the side it implies
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Condition:
    name: str
    mask: tuple            # stored as a tuple of positions for hashing safety
    side: int              # +1 expects up, -1 expects down

    def __len__(self) -> int:
        return len(self.mask)


def conditions(ind: pd.DataFrame) -> list[tuple[str, pd.Series, int]]:
    """Every rule worth testing, as (name, boolean mask, expected direction).

    Thresholds are a grid rather than the conventional value alone. If RSI
    only works at 30 and not at 25 or 35, that is a knife edge and worth
    seeing; if it works nowhere, "you used the wrong setting" is answered
    in advance.
    """
    out: list[tuple[str, pd.Series, int]] = []

    # --- RSI: oversold buys, overbought sells (the textbook reading) ---
    for p in (7, 9, 14, 21):
        col = ind[f"rsi{p}"]
        for lo in (20, 25, 30, 35, 40):
            out.append((f"rsi{p}<{lo}", col < lo, +1))
        for hi in (60, 65, 70, 75, 80):
            out.append((f"rsi{p}>{hi}", col > hi, -1))
        # ...and the momentum reading, which says the opposite.
        out.append((f"rsi{p}>50_mom", col > 50, +1))
        out.append((f"rsi{p}<50_mom", col < 50, -1))

    # --- Bollinger: band touches, read both ways ---
    for tag in ("20_2", "20_2.5", "14_2", "50_2"):
        b = ind[f"bb_pct_b_{tag}"]
        w = ind[f"bb_width_{tag}"]
        for th in (0.0, 0.05, 0.1):
            out.append((f"bb{tag}_below_{th}_revert", b <= th, +1))
            out.append((f"bb{tag}_below_{th}_break", b <= th, -1))
        for th in (1.0, 0.95, 0.9):
            out.append((f"bb{tag}_above_{th}_revert", b >= th, -1))
            out.append((f"bb{tag}_above_{th}_break", b >= th, +1))
        # squeeze: low volatility preceding expansion
        q = w.quantile(0.2)
        out.append((f"bb{tag}_squeeze_up", (w <= q) & (b > 0.5), +1))
        out.append((f"bb{tag}_squeeze_dn", (w <= q) & (b < 0.5), -1))

    # --- MACD ---
    for tag in ("12_26_9", "5_13_5", "8_21_5"):
        h = ind[f"macd_hist_{tag}"]
        c = ind[f"macd_cross_{tag}"]
        out.append((f"macd{tag}_hist_up", h > 0, +1))
        out.append((f"macd{tag}_hist_dn", h < 0, -1))
        out.append((f"macd{tag}_cross_up", (c > 0) & (c.shift() <= 0), +1))
        out.append((f"macd{tag}_cross_dn", (c < 0) & (c.shift() >= 0), -1))
        out.append((f"macd{tag}_rising", h.diff() > 0, +1))
        out.append((f"macd{tag}_falling", h.diff() < 0, -1))

    # --- moving averages ---
    for p in (9, 21, 50, 200):
        d = ind[f"ema{p}_dist"]
        s = ind[f"ema{p}_slope"]
        out.append((f"above_ema{p}", d > 0, +1))
        out.append((f"below_ema{p}", d < 0, -1))
        out.append((f"ema{p}_rising", s > 0, +1))
        out.append((f"ema{p}_falling", s < 0, -1))
    out.append(("ema9_over_21", ind["ema9_21_cross"] > 0, +1))
    out.append(("ema9_under_21", ind["ema9_21_cross"] < 0, -1))

    return [(n, m.fillna(False).astype(bool), s) for n, m, s in out]


# --------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------
def target(df: pd.DataFrame) -> pd.Series:
    """Did the next bar close higher? Ties dropped -- they are refunded."""
    nxt = df["close"].shift(-1) - df["close"]
    return (nxt[nxt != 0] > 0)


def evaluate(name: str, mask: pd.Series, side: int, y: pd.Series,
             min_n: int = 150) -> Optional[Finding]:
    idx = y.index.intersection(mask[mask].index)
    n = len(idx)
    if n < min_n:
        return None
    up = y.loc[idx]
    wins = int(up.sum()) if side > 0 else int((~up).sum())
    wr = wins / n
    base_up = float(y.mean())
    baseline = base_up if side > 0 else 1 - base_up
    p = float(stats.binomtest(wins, n, baseline, alternative="two-sided").pvalue)
    return Finding("indicator", "USDCAD_otc", name, n, wr, baseline, p, effect=side)


def search(df: pd.DataFrame, depth: int = 3, min_n: int = 150,
           top_singles: int = 60) -> tuple[list[Finding], dict[str, tuple]]:
    """Singles, then pairs, then triples of the best singles.

    Combining all pairs of 200 conditions is 20,000 tests and combining all
    triples is 1.3 million, which stops being a search and becomes a random
    number generator. So combinations are built from the strongest singles
    only -- still a wide search, and the correction is told how wide.
    """
    ind = build(df)
    y = target(df)
    conds = conditions(ind)
    lookup = {name: (mask, side) for name, mask, side in conds}

    findings = []
    for name, mask, side in conds:
        f = evaluate(name, mask, side, y, min_n)
        if f:
            findings.append(f)

    ranked = sorted(findings, key=lambda f: -f.win_rate)[:top_singles]
    names = [f.detail for f in ranked]

    for k in (2, 3)[:max(depth - 1, 0)]:
        for combo in combinations(names, k):
            sides = {lookup[c][1] for c in combo}
            if len(sides) > 1:
                continue      # conditions that disagree are not a strategy
            side = sides.pop()
            mask = lookup[combo[0]][0].copy()
            for c in combo[1:]:
                mask &= lookup[c][0]
            f = evaluate(" + ".join(combo), mask, side, y, min_n)
            if f:
                findings.append(f)
    return findings, lookup


def replay(detail: str, side: int, df: pd.DataFrame,
           min_n: int = 100) -> Optional[tuple[int, int]]:
    """Rebuild a rule from its name and score it on untouched data."""
    ind = build(df)
    lookup = {n: (m, s) for n, m, s in conditions(ind)}
    parts = detail.split(" + ")
    if not all(p in lookup for p in parts):
        return None
    mask = lookup[parts[0]][0].copy()
    for p in parts[1:]:
        mask &= lookup[p][0]
    y = target(df)
    idx = y.index.intersection(mask[mask].index)
    if len(idx) < min_n:
        return None
    up = y.loc[idx]
    wins = int(up.sum()) if side > 0 else int((~up).sum())
    return wins, len(idx)
