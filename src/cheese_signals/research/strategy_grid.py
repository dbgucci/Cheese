"""The indicator search, run to exhaustion and then charged for it.

``probes.py`` asks whether the *series* has structure. This module asks the
question traders actually ask: **does any indicator, on any pair, at any hour,
at any expiry, win often enough to beat the payout?** It is the "just backtest
everything" approach, done properly, so that its answer is worth something.

Doing it properly means three things that a naive grid search omits, each of
which is enough on its own to manufacture an edge that is not there:

1. **A holdout.** Every configuration is scored on data the search never saw.
   Ranking 20,000 cells and reporting the best in-sample number measures the
   size of the search, not the series.

2. **A null that ran the same gauntlet.** The benchmark for "my best cell hit
   67%" is not 50% -- it is *what the same search finds in data known to have
   no edge*. :func:`synthetic_null` re-runs the whole pipeline on Gaussian
   random walks matched to each pair's volatility and segment structure. If
   the real number sits inside that distribution, the number is the search.

3. **Walk-forward, not one split.** A bot does not get a holdout; it gets to
   re-pick its strategy each day and live with the choice. :func:`walk_forward`
   simulates exactly that, which is the only backtest whose number a live bot
   should be expected to reproduce.

Everything is causal: a signal at bar ``t`` uses bars ``<= t``, entry is the
open of ``t+1`` (the first price actually tradable once ``t`` has closed), and
expiry is the close of ``t+N``. A close equal to entry is a refund, excluded
from the win rate rather than scored as a loss.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import numpy as np
import pandas as pd

EXPIRIES = (1, 2, 3, 5)
MAX_INDICATOR_LOOKBACK = 50
WARMUP = 60  # > MAX_INDICATOR_LOOKBACK, so no signal is read off a partial window


# --------------------------------------------------------------------------
# causal indicators
# --------------------------------------------------------------------------
def ema(x: np.ndarray, n: int) -> np.ndarray:
    a = 2.0 / (n + 1)
    out = np.empty_like(x, dtype=float)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def sma(x: np.ndarray, n: int) -> np.ndarray:
    c = np.concatenate([[0.0], np.cumsum(x)])
    out = np.full(len(x), np.nan)
    out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def rolling_std(x: np.ndarray, n: int) -> np.ndarray:
    c1 = np.concatenate([[0.0], np.cumsum(x)])
    c2 = np.concatenate([[0.0], np.cumsum(x * x)])
    out = np.full(len(x), np.nan)
    s1, s2 = c1[n:] - c1[:-n], c2[n:] - c2[:-n]
    out[n - 1:] = np.sqrt(np.maximum(s2 / n - (s1 / n) ** 2, 0.0))
    return out


def rsi(x: np.ndarray, n: int) -> np.ndarray:
    d = np.diff(x, prepend=x[0])
    au = ema(np.where(d > 0, d, 0.0), n)
    ad = ema(np.where(d < 0, -d, 0.0), n)
    return 100 - 100 / (1 + au / np.where(ad == 0, 1e-12, ad))


def stochastic(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int) -> np.ndarray:
    hh = pd.Series(h).rolling(n).max().to_numpy()
    ll = pd.Series(l).rolling(n).min().to_numpy()
    return 100 * (c - ll) / np.where(hh - ll == 0, 1e-12, hh - ll)


def macd(x: np.ndarray, fast: int, slow: int, signal: int):
    line = ema(x, fast) - ema(x, slow)
    return line, ema(line, signal)


# --------------------------------------------------------------------------
# the strategy family
# --------------------------------------------------------------------------
def build_signals(bars: pd.DataFrame) -> dict[str, np.ndarray]:
    """Every variant, as arrays in {-1, 0, +1}. +1 = CALL, -1 = PUT, 0 = stand aside.

    Each rule appears in both polarities (``_revert`` / ``_follow``), because a
    rule and its exact opposite cannot both be edges, and including both keeps
    the search from quietly encoding a prior about which way the series moves.
    """
    o, h, l, c = (bars.open.values, bars.high.values, bars.low.values, bars.close.values)
    n = len(c)
    out: dict[str, np.ndarray] = {}
    body = c - o
    span = np.maximum(h - l, 1e-12)
    step = np.diff(c, prepend=c[0])
    sign = np.sign(step)

    for p in (2, 3, 5, 7, 14):
        r = rsi(c, p)
        for lo, hi in ((10, 90), (20, 80), (30, 70)):
            s = np.where(r < lo, 1, np.where(r > hi, -1, 0)).astype(int)
            out[f"rsi{p}_{lo}/{hi}_revert"] = s
            out[f"rsi{p}_{lo}/{hi}_follow"] = -s

    for p in (10, 20, 30):
        mu, sd = sma(c, p), rolling_std(c, p)
        for k in (1.5, 2.0, 2.5):
            s = np.where(c < mu - k * sd, 1, np.where(c > mu + k * sd, -1, 0)).astype(int)
            out[f"bb{p}_{k}_revert"] = s
            out[f"bb{p}_{k}_follow"] = -s

    for f, s_ in ((3, 10), (5, 20), (8, 26), (12, 50)):
        state = np.sign(ema(c, f) - ema(c, s_)).astype(int)
        prev = np.concatenate([[0], state[:-1]])
        out[f"emacross{f}/{s_}_state"] = state
        out[f"emacross{f}/{s_}_event"] = np.where(state != prev, state, 0)

    for f, s_, g in ((12, 26, 9), (5, 35, 5), (8, 17, 9)):
        line, sigline = macd(c, f, s_, g)
        state = np.sign(line - sigline).astype(int)
        prev = np.concatenate([[0], state[:-1]])
        out[f"macd{f}/{s_}/{g}_state"] = state
        out[f"macd{f}/{s_}/{g}_event"] = np.where(state != prev, state, 0)

    run = np.zeros(n, dtype=int)
    for i in range(1, n):
        run[i] = run[i - 1] + 1 if sign[i] != 0 and sign[i] == sign[i - 1] else int(sign[i] != 0)
    for k in (2, 3, 4, 5):
        s = np.where(run >= k, sign, 0).astype(int)
        out[f"streak{k}_follow"] = s
        out[f"streak{k}_revert"] = -s

    bsd = rolling_std(body, 20)
    for k in (1.5, 2.0, 3.0):
        s = np.where(np.abs(body) > k * bsd, np.sign(body), 0).astype(int)
        out[f"bigbody{k}_follow"] = s
        out[f"bigbody{k}_revert"] = -s

    for p in (5, 10, 20):
        hh = pd.Series(h).rolling(p).max().to_numpy()
        ll = pd.Series(l).rolling(p).min().to_numpy()
        ph = np.concatenate([[np.nan], hh[:-1]])
        pl = np.concatenate([[np.nan], ll[:-1]])
        s = np.where(c > ph, 1, np.where(c < pl, -1, 0)).astype(int)
        out[f"breakout{p}_follow"] = s
        out[f"breakout{p}_revert"] = -s

    for p in (5, 14):
        st = stochastic(h, l, c, p)
        for lo, hi in ((20, 80), (10, 90)):
            s = np.where(st < lo, 1, np.where(st > hi, -1, 0)).astype(int)
            out[f"stoch{p}_{lo}/{hi}_revert"] = s
            out[f"stoch{p}_{lo}/{hi}_follow"] = -s

    po = np.concatenate([[np.nan], o[:-1]])
    pc = np.concatenate([[np.nan], c[:-1]])
    bull = (c > o) & (pc < po) & (c >= po) & (o <= pc)
    bear = (c < o) & (pc > po) & (c <= po) & (o >= pc)
    eng = np.where(bull, 1, np.where(bear, -1, 0)).astype(int)
    out["engulf_follow"], out["engulf_revert"] = eng, -eng

    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l
    pin = np.where((lower > 2 * np.abs(body)) & (lower > 0.5 * span), 1,
                   np.where((upper > 2 * np.abs(body)) & (upper > 0.5 * span), -1, 0)).astype(int)
    out["pinbar_revert"], out["pinbar_follow"] = pin, -pin

    for p in (10, 20, 50):
        s = np.sign(np.nan_to_num(c - sma(c, p))).astype(int)
        out[f"vsSMA{p}_follow"] = s
        out[f"vsSMA{p}_revert"] = -s

    # The honest baselines. If nothing beats these, nothing is an edge.
    out["always_call"] = np.ones(n, dtype=int)
    out["always_put"] = -np.ones(n, dtype=int)
    return out


# --------------------------------------------------------------------------
# panel handling and the backtest itself
# --------------------------------------------------------------------------
def segments(frame: pd.DataFrame, min_bars: int = 200) -> dict[str, list[pd.DataFrame]]:
    """Split each asset into contiguous 1-minute runs.

    Returns are never computed across a collection gap: a 7-day hole would
    otherwise read as one enormous, entirely fictional candle.
    """
    panel: dict[str, list[pd.DataFrame]] = {}
    for asset, g in frame.groupby("asset"):
        g = g.sort_values("ts").reset_index(drop=True)
        breaks = (g.ts.diff().dt.total_seconds() > 60).cumsum()
        runs = [s.reset_index(drop=True) for _, s in g.groupby(breaks) if len(s) >= min_bars]
        if runs:
            panel[asset] = runs
    return panel


def backtest(panel: dict[str, list[pd.DataFrame]],
             expiries: Iterable[int] = EXPIRIES,
             warmup: int = WARMUP) -> pd.DataFrame:
    """One row per simulated trade: asset, strategy, expiry, hour, ts, result.

    ``res`` is +1 win, -1 loss, 0 refund.
    """
    frames = []
    for asset, runs in panel.items():
        for bars in runs:
            signals = build_signals(bars)
            o, c = bars.open.values, bars.close.values
            ts, hour = bars.ts.values, pd.DatetimeIndex(bars.ts).hour.values
            n = len(c)
            for expiry in expiries:
                t = np.arange(warmup, n - expiry - 1)
                if len(t) == 0:
                    continue
                move = c[t + expiry] - o[t + 1]
                for name, arr in signals.items():
                    d = arr[t]
                    keep = d != 0
                    if not keep.any():
                        continue
                    frames.append(pd.DataFrame({
                        "asset": asset, "strategy": name, "expiry": expiry,
                        "hour": hour[t[keep]], "ts": ts[t[keep]],
                        "res": (np.sign(move[keep]) * d[keep]).astype(np.int8)}))
    if not frames:
        return pd.DataFrame(columns=["asset", "strategy", "expiry", "hour", "ts", "res"])
    return pd.concat(frames, ignore_index=True)


def cell_key(trades: pd.DataFrame) -> pd.Series:
    return (trades.strategy.astype(str) + "|" + trades.asset.astype(str) + "|"
            + trades.hour.astype(str) + "|" + trades.expiry.astype(str))


def grid(trades: pd.DataFrame, cut_quantile: float = 0.65,
         min_train: int = 150, min_test: int = 50) -> pd.DataFrame:
    """Score every strategy x pair x hour x expiry cell in-sample and out."""
    t = trades.copy()
    t["ts"] = pd.to_datetime(t.ts, utc=True)
    cut = t.ts.quantile(cut_quantile)
    t["split"] = np.where(t.ts <= cut, "train", "test")
    g = (t.groupby(["strategy", "asset", "hour", "expiry", "split"], observed=True)
          .res.agg(w=lambda s: int((s > 0).sum()), l=lambda s: int((s < 0).sum()))
          .reset_index())
    g["dec"] = g.w + g.l
    p = g.pivot_table(index=["strategy", "asset", "hour", "expiry"],
                      columns="split", values=["dec", "w"], observed=True).fillna(0)
    out = pd.DataFrame({
        "train_n": p.get(("dec", "train"), 0), "train_w": p.get(("w", "train"), 0),
        "test_n": p.get(("dec", "test"), 0), "test_w": p.get(("w", "test"), 0),
    }).reset_index()
    out = out[(out.train_n >= min_train) & (out.test_n >= min_test)].copy()
    out["train_win"] = out.train_w / out.train_n
    out["test_win"] = out.test_w / out.test_n
    return out.sort_values("train_win", ascending=False).reset_index(drop=True)


@dataclass
class WalkForward:
    """What an adaptive bot would actually have earned."""
    min_trades: int
    top_k: int
    n: int
    wins: int
    selected_in_sample: float

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else float("nan")

    def ev(self, payout: float) -> float:
        return self.win_rate * payout - (1 - self.win_rate)

    def describe(self, payout: float = 0.92) -> str:
        return (f"min_trades={self.min_trades} top_k={self.top_k}: "
                f"selected at {self.selected_in_sample*100:.2f}% in-sample, "
                f"delivered {self.win_rate*100:.2f}% over {self.n} forward trades "
                f"(EV {self.ev(payout)*100:+.2f}% at {payout:.0%})")


def walk_forward(trades: pd.DataFrame, folds: int = 12,
                 min_trades: int = 150, top_k: int = 1) -> WalkForward:
    """Re-pick the best cell from all history so far, trade it in the next fold.

    This is the backtest that cannot be fooled by hindsight: at every point the
    selection uses only data that already existed.
    """
    t = trades.copy()
    t["ts"] = pd.to_datetime(t.ts, utc=True)
    t = t.sort_values("ts").reset_index(drop=True)
    t["cell"] = cell_key(t)
    t["fold"] = pd.qcut(t.ts.rank(method="first"), folds, labels=False)

    total_n = total_w = 0
    picked: list[float] = []
    for f in range(1, folds):
        history, future = t[t.fold < f], t[t.fold == f]
        h = (history.groupby("cell", observed=True).res
             .agg(w=lambda s: int((s > 0).sum()), l=lambda s: int((s < 0).sum())))
        h["dec"] = h.w + h.l
        h = h[h.dec >= min_trades]
        if h.empty:
            continue
        h["wr"] = h.w / h.dec
        picks = h.sort_values("wr", ascending=False).head(top_k)
        taken = future[future.cell.isin(picks.index) & (future.res != 0)]
        if taken.empty:
            continue
        picked.append(float(picks.wr.mean()))
        total_n += len(taken)
        total_w += int((taken.res > 0).sum())
    return WalkForward(min_trades, top_k, total_n, total_w,
                       float(np.mean(picked)) if picked else float("nan"))


# --------------------------------------------------------------------------
# the null: the same search, on series known to hold nothing
# --------------------------------------------------------------------------
def match_spec(panel: dict[str, list[pd.DataFrame]]) -> dict[str, dict]:
    """Per-pair volatility and segment shape, so the null is a fair comparison."""
    spec = {}
    for asset, runs in panel.items():
        r = np.concatenate([np.diff(np.log(s.close.values)) for s in runs])
        z = (r - r.mean()) / r.std()
        r = r[np.abs(z) <= 20]  # drop feed glitches; they are not price moves
        spec[asset] = {"sigma": float(r.std()),
                       "price": float(runs[0].close.iloc[0]),
                       "shape": [(len(s), s.ts) for s in runs]}
    return spec


def synthetic_panel(spec: dict[str, dict], rng: np.random.Generator
                    ) -> dict[str, list[pd.DataFrame]]:
    """Gaussian random walks with each pair's volatility and exact timestamps."""
    panel = {}
    for asset, sp in spec.items():
        runs = []
        for length, ts in sp["shape"]:
            c = sp["price"] * np.exp(np.cumsum(rng.normal(0, sp["sigma"], length)))
            o = np.concatenate([[c[0]], c[:-1]])
            wick = np.abs(rng.normal(0, sp["sigma"], (length, 2))) * c[:, None]
            runs.append(pd.DataFrame({
                "asset": asset, "ts": pd.Series(ts).values, "open": o,
                "high": np.maximum(o, c) + wick[:, 0],
                "low": np.minimum(o, c) - wick[:, 1],
                "close": c, "volume": 0.0}).reset_index(drop=True))
        panel[asset] = runs
    return panel


def synthetic_null(panel: dict[str, list[pd.DataFrame]], reps: int = 24,
                   seed: int = 1000,
                   score: Optional[Callable[[pd.DataFrame], float]] = None,
                   progress: Optional[Callable[[int, float], None]] = None
                   ) -> np.ndarray:
    """Re-run the whole pipeline on ``reps`` universes that contain no edge.

    The returned distribution is the honest benchmark for any number the same
    pipeline produced on the real data.
    """
    if score is None:
        def score(trades: pd.DataFrame) -> float:
            g = grid(trades)
            return float(g.train_win.max()) if len(g) else float("nan")
    spec = match_spec(panel)
    out = []
    for i in range(reps):
        rng = np.random.default_rng(seed + i)
        value = score(backtest(synthetic_panel(spec, rng)))
        out.append(value)
        if progress:
            progress(i, value)
    return np.asarray(out, dtype=float)


def breakeven(payout: float) -> float:
    """The win rate a payout demands. 0.92 -> 52.08%, 0.85 -> 54.05%."""
    return 1.0 / (1.0 + payout)
