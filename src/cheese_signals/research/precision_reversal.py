"""Evaluate the "precision reversal" strategy: wick rejection + volume + EMA200 + S/R.

The strategy is a four-confluence reversal filter written against TA-Lib:
a long wick sets the direction, a volume spike, EMA200 trend alignment and a
support/resistance touch are then required, and it reports a confidence of
70-100%.

This module does three separate jobs, because "is it any good" turns out to
have three different answers:

1. **Port it so it can run at all.** As written it raises ``KeyError`` on
   every frame this project produces (``ema200[i]`` is a *label* lookup on a
   DatetimeIndex, not a positional one), so it has never run against this
   repo's data. :func:`check_signal` is the same rules with the indexing
   repaired and TA-Lib's EMA reproduced exactly -- no logic changed.

2. **Take it apart.** Three of the four "confluences" are mandatory gates
   that ``return None`` when they fail, so by the time the
   ``len(confluences) >= 3`` test runs it is always true and the confidence
   is only ever 70 or 80. :func:`replay` records which gates fired at every
   bar so that structure is visible in the numbers rather than argued about.

3. **Judge it against the payout, not against 50%.** A binary option at a
   0.92 payout needs 52.08% to break even and at the median 0.78 payout it
   needs 56.18%. A strategy can be significantly better than a coin flip and
   still be a steady loser, which is the trap ``validate.py`` exists for.

``strict=True`` additionally repairs the three real logic defects (see
:func:`check_signal`) so the evaluation can distinguish "the code is buggy"
from "the premise does not hold on this series" -- they need different
answers, and only one of them is fixable.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats

from . import dataset
from .validate import breakeven

BUY = "BUY"
SELL = "SELL"

# Gates, in the order the strategy applies them.
WICK = "wick"
VOLUME = "volume"
TREND = "trend"
SR = "sr"


def talib_ema(values: np.ndarray | pd.Series, period: int) -> np.ndarray:
    """TA-Lib's EMA, reproduced exactly.

    Not interchangeable with ``indicators.ema``: TA-Lib emits ``period-1``
    NaNs and seeds the recursion with an SMA, where pandas' ``ewm`` starts
    from the first value and is defined everywhere. Using the pandas version
    here would silently change which bars the strategy is allowed to trade,
    so the original's warmup behaviour is preserved instead.
    """
    vals = np.asarray(values, dtype=float)
    out = np.full(len(vals), np.nan)
    if len(vals) < period:
        return out
    k = 2.0 / (period + 1.0)
    out[period - 1] = vals[:period].mean()
    for i in range(period, len(vals)):
        out[i] = (vals[i] - out[i - 1]) * k + out[i - 1]
    return out


@dataclass
class Setup:
    """One firing of the rules, with the gates that produced it."""

    index: int
    direction: str
    confidence: int
    gates: tuple[str, ...]
    reason: str


def check_signal(
    df: pd.DataFrame,
    i: int,
    ema200: np.ndarray,
    strict: bool = False,
) -> Optional[Setup]:
    """The original rules at bar ``i``. Only bars ``<= i`` are ever read.

    ``ema200`` is passed in precomputed. The original recomputed the full
    200-period EMA inside every call, which is O(n) per bar and O(n^2) over a
    backtest; it is causal either way, so hoisting it changes cost, not
    results.

    With ``strict=True`` three genuine logic defects are repaired:

    * **Wick choice is order-dependent.** The original tests the lower wick
      first, so a candle whose *upper* wick is the larger one is still called
      a BUY as long as the lower wick also clears ``1.5 * body``. On a doji
      (``body == 0``) any candle with both wicks qualifies and is always
      called a BUY. Strict picks the dominant wick.
    * **Support/resistance is measured on closes.** ``close[i-10:i+1]``
      includes bar ``i`` itself, so the bar is compared against its own
      close; and a level built from closes ignores the highs and lows that
      actually define it. Strict uses the prior 10 bars' lows/highs.
    * **The volume window is empty for i < 10.** ``volume[i-10:i]`` with a
      negative start returns nothing, ``mean()`` is NaN, and the comparison
      is quietly False rather than an error. Strict requires a full window.
    """
    row = df.iloc[i]
    close = df["close"]
    volume = df["volume"]

    confluences: list[str] = []
    gates: list[str] = []

    # --- 1. wick rejection, which also sets the direction -------------------
    body = abs(row["close"] - row["open"])
    upper_wick = row["high"] - max(row["close"], row["open"])
    lower_wick = min(row["close"], row["open"]) - row["low"]

    bullish_wick = lower_wick > body * 1.5
    bearish_wick = upper_wick > body * 1.5

    if strict:
        # Dominant wick wins; a tie is not a rejection either way.
        if bullish_wick and lower_wick > upper_wick:
            direction = BUY
        elif bearish_wick and upper_wick > lower_wick:
            direction = SELL
        else:
            return None
    else:
        if bullish_wick:
            direction = BUY
        elif bearish_wick:
            direction = SELL
        else:
            return None

    confluences.append(f"Wick rejection ({'bullish' if direction == BUY else 'bearish'})")
    gates.append(WICK)

    # --- 2. volume spike (the only optional confluence) ---------------------
    window = volume.iloc[max(0, i - 10): i]
    if len(window) == 10 or not strict:
        vol_sma = window.mean()
        if pd.notna(vol_sma) and volume.iloc[i] > vol_sma * 1.3:
            confluences.append("Volume spike")
            gates.append(VOLUME)

    # --- 3. EMA200 trend alignment ------------------------------------------
    ema_now = ema200[i]
    if direction == BUY and close.iloc[i] > ema_now:
        confluences.append("Trend is UP (above EMA 200)")
    elif direction == SELL and close.iloc[i] < ema_now:
        confluences.append("Trend is DOWN (below EMA 200)")
    else:
        return None
    gates.append(TREND)

    # --- 4. support / resistance --------------------------------------------
    if strict:
        if i < 10:
            return None
        lows = df["low"].iloc[i - 10: i]
        highs = df["high"].iloc[i - 10: i]
        touched = row["low"] <= lows.min() if direction == BUY else row["high"] >= highs.max()
    else:
        sr_window = close.iloc[max(0, i - 10): i + 1]
        touched = (
            row["low"] <= sr_window.min()
            if direction == BUY
            else row["high"] >= sr_window.max()
        )
    if not touched:
        return None
    confluences.append("Touched support" if direction == BUY else "Hit resistance")
    gates.append(SR)

    # The original's final test. It can never fail: the three gates above all
    # return None when unmet, so `confluences` holds at least 3 entries by the
    # time control reaches here, and at most 4.
    if len(confluences) >= 3:
        confidence = min(70 + (len(confluences) - 3) * 10, 100)
        return Setup(i, direction, confidence, tuple(gates), " + ".join(confluences))
    return None


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------
@dataclass
class Outcome:
    timestamp: pd.Timestamp
    index: int
    direction: str
    confidence: int
    gates: tuple[str, ...]
    won: bool


@dataclass
class Result:
    """Outcomes of one replay, scored the way the broker settles them."""

    outcomes: list[Outcome] = field(default_factory=list)
    bars: int = 0
    refunds: int = 0

    @property
    def n(self) -> int:
        return len(self.outcomes)

    @property
    def wins(self) -> int:
        return sum(o.won for o in self.outcomes)

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else float("nan")

    @property
    def rate_per_bar(self) -> float:
        return self.n / self.bars if self.bars else 0.0

    def ci(self, level: float = 0.95) -> tuple[float, float]:
        """Wilson interval -- correct near the tails and for small n, unlike normal-approx."""
        if not self.n:
            return (float("nan"), float("nan"))
        lo, hi = stats.binomtest(self.wins, self.n).proportion_ci(level, method="wilson")
        return float(lo), float(hi)

    def p_better_than_coin(self) -> float:
        if not self.n:
            return float("nan")
        return float(stats.binomtest(self.wins, self.n, 0.5, alternative="greater").pvalue)

    def ev(self, payout: float) -> float:
        """Expected units per unit staked. Refunds return the stake, so they are neutral."""
        if not self.n:
            return float("nan")
        return self.win_rate * payout - (1.0 - self.win_rate)

    def filter(self, pred) -> "Result":
        kept = [o for o in self.outcomes if pred(o)]
        return Result(kept, self.bars, self.refunds)


def replay(
    df: pd.DataFrame,
    expiry: int = 1,
    strict: bool = False,
    warmup: int = 200,
) -> Result:
    """Apply the rules bar by bar and settle each signal ``expiry`` bars later.

    A flat close is a refund on Pocket Option, so it is neither a win nor a
    loss and is kept out of the denominator -- counting refunds as losses
    understates the win rate, counting them as wins invents an edge.
    """
    if len(df) <= warmup + expiry:
        return Result([], 0, 0)

    ema200 = talib_ema(df["close"].to_numpy(dtype=float), 200)
    closes = df["close"].to_numpy(dtype=float)

    outcomes: list[Outcome] = []
    refunds = 0
    lo = max(warmup, 200)
    hi = len(df) - expiry
    for i in range(lo, hi):
        setup = check_signal(df, i, ema200, strict=strict)
        if setup is None:
            continue
        move = closes[i + expiry] - closes[i]
        if move == 0:
            refunds += 1
            continue
        won = (move > 0) if setup.direction == BUY else (move < 0)
        outcomes.append(
            Outcome(df.index[i], i, setup.direction, setup.confidence, setup.gates, won)
        )
    return Result(outcomes, bars=max(hi - lo, 0), refunds=refunds)


def replay_panel(panel: dict[str, pd.DataFrame], expiry: int = 1,
                 strict: bool = False, min_bars: int = 260) -> Result:
    """Replay every asset, over genuinely consecutive bars only.

    Segmenting matters: a signal on the last bar before a four-hour recording
    gap would otherwise be settled against a price four hours later, which is
    not the trade the strategy would have taken.
    """
    pooled = Result([], 0, 0)
    for _asset, df in sorted(panel.items()):
        for seg in dataset.segments(df, min_bars=min_bars):
            r = replay(seg, expiry=expiry, strict=strict)
            pooled.outcomes.extend(r.outcomes)
            pooled.bars += r.bars
            pooled.refunds += r.refunds
    return pooled


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
PAYOUTS = (0.78, 0.85, 0.90, 0.92)


def required_trades(true_rate: float, be: float, power: float = 0.80,
                    alpha: float = 0.05) -> Optional[int]:
    """Trades needed to show ``true_rate`` beats ``be``, one-sided.

    The number that decides whether an edge this size is *findable* at this
    signal rate, which is usually the binding constraint rather than whether
    it exists.
    """
    if true_rate <= be:
        return None
    za = stats.norm.ppf(1 - alpha)
    zb = stats.norm.ppf(power)
    p0, p1 = be, true_rate
    num = za * np.sqrt(p0 * (1 - p0)) + zb * np.sqrt(p1 * (1 - p1))
    return int(np.ceil((num / (p1 - p0)) ** 2))


def _line(label: str, r: Result, payout: float) -> str:
    if not r.n:
        return f"  {label:34s} {'no signals':>10s}"
    lo, hi = r.ci()
    return (f"  {label:34s} n={r.n:>6,} wr={r.win_rate:.4f} "
            f"[{lo:.4f}, {hi:.4f}]  EV@{payout:.2f}={r.ev(payout):+.4f}")


def report(panel: dict[str, pd.DataFrame], payout: float = 0.92,
           expiry: int = 1, test_fraction: float = 0.35) -> str:
    from .validate import split_by_time

    be = breakeven(payout)
    lines: list[str] = []

    for strict in (False, True):
        title = "AS WRITTEN" if not strict else "WITH THE LOGIC DEFECTS REPAIRED"
        r = replay_panel(panel, expiry=expiry, strict=strict)
        lines.append(f"\n{title}")
        if not r.n:
            lines.append("  no signals fired")
            continue
        lo, hi = r.ci()
        lines += [
            f"  signals            {r.n:,} on {r.bars:,} eligible bars "
            f"({r.rate_per_bar:.2%} of bars), {r.refunds:,} refunds",
            f"  win rate           {r.win_rate:.4f}  95% CI [{lo:.4f}, {hi:.4f}]"
            f"  p(>50%)={r.p_better_than_coin():.3f}",
            f"  break-even @{payout:.2f}   {be:.4f}"
            f"   ->  EV {r.ev(payout):+.4f} per unit staked",
            "",
            "  direction",
            _line("BUY", r.filter(lambda o: o.direction == BUY), payout),
            _line("SELL", r.filter(lambda o: o.direction == SELL), payout),
            "",
            "  its own confidence tiers",
            _line("70% (no volume spike)", r.filter(lambda o: o.confidence == 70), payout),
            _line("80% (volume spike)", r.filter(lambda o: o.confidence == 80), payout),
        ]

        lines.append("\n  payout sensitivity, at this win rate")
        for p in PAYOUTS:
            verdict = "profit" if r.win_rate > breakeven(p) else "loss"
            lines.append(f"    payout {p:.2f}  break-even {breakeven(p):.4f}  "
                         f"EV {r.ev(p):+.4f}  {verdict}")

    # Train/holdout on the as-written version: the question is whether any
    # apparent edge survives on bars that were not used to form the opinion.
    lines.append("\nHOLDOUT")
    try:
        split = split_by_time(panel, test_fraction)
        tr = replay_panel(split.train, expiry=expiry)
        te = replay_panel(split.test, expiry=expiry)
        lines.append(f"  {split.describe()}")
        lines.append(_line("train", tr, payout))
        lines.append(_line("holdout", te, payout))
        if tr.n and te.n:
            lines.append(f"  train -> holdout drift: {te.win_rate - tr.win_rate:+.4f}")
    except ValueError as exc:
        lines.append(f"  cannot split: {exc}")

    # What it would take to prove the observed rate is real.
    full = replay_panel(panel, expiry=expiry)
    if full.n:
        lines.append("\nEVIDENCE REQUIRED")
        need = required_trades(full.win_rate, be)
        if need is None:
            lines.append(f"  the observed {full.win_rate:.2%} is at or below the "
                         f"{be:.2%} break-even; there is no edge to size")
        else:
            bars = need / full.rate_per_bar if full.rate_per_bar else float("inf")
            lines.append(f"  to show {full.win_rate:.2%} beats {be:.2%} at 80% power: "
                         f"~{need:,} trades")
            lines.append(f"  at {full.rate_per_bar:.2%} of bars that is ~{bars:,.0f} "
                         f"1-minute bars ({bars / 1440:,.0f} days of continuous running)")
    return "\n".join(lines)


def driftless_walk(count: int, start_price: float = 1.10000, vol: float = 0.00012,
                   seed: int | None = None) -> pd.DataFrame:
    """A pure driftless random walk with wicks. The honest null.

    ``data.synthetic`` is deliberately *not* this: its docstring says it
    switches between trending and ranging regimes so the regime-gated
    strategies have something to react to. Those regimes are real drift, and
    a trend-aligned strategy can genuinely harvest them -- which makes that
    generator useless for asking "what does this score on nothing".

    Here every step is a zero-mean shock, so any win rate away from 50% is
    sampling noise and nothing else.
    """
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, vol, count)
    closes = start_price * np.exp(np.cumsum(steps))
    opens = np.empty(count)
    opens[0] = start_price
    opens[1:] = closes[:-1]

    scale = np.abs(steps) * closes
    scale = np.maximum(scale, 1e-9)
    highs = np.maximum(opens, closes) + np.abs(rng.normal(0, 1, count)) * scale
    lows = np.minimum(opens, closes) - np.abs(rng.normal(0, 1, count)) * scale
    volumes = rng.integers(50, 500, count).astype(float)

    # Anchored, not "now": the daily pivot levels downstream are cut on UTC day
    # boundaries, so an index that moves with wall-clock time silently changes
    # which bars land in which session and the same seed stops reproducing.
    index = pd.date_range(start=pd.Timestamp("2026-01-01", tz="UTC"),
                          periods=count, freq="60s")
    return pd.DataFrame({"open": opens, "high": highs, "low": lows,
                         "close": closes, "volume": volumes}, index=index)


def null_distribution(bars: int = 40000, seeds: int = 12, expiry: int = 1,
                      strict: bool = False) -> str:
    """Replay the rules over many independent driftless walks.

    One null run gives one number and no sense of its spread. Repeating it
    shows how far from 50% these rules land *by chance* at this signal count,
    which is the yardstick any real result has to clear.
    """
    rates, ns = [], []
    for s in range(seeds):
        r = replay(driftless_walk(bars, seed=1000 + s), expiry=expiry, strict=strict)
        if r.n:
            rates.append(r.win_rate)
            ns.append(r.n)
    if not rates:
        return "  no signals fired on any null series"
    arr = np.array(rates)
    return (f"  {seeds} driftless walks x {bars:,} bars, "
            f"{int(np.mean(ns)):,} signals each\n"
            f"  win rate  mean {arr.mean():.4f}  sd {arr.std(ddof=1):.4f}  "
            f"min {arr.min():.4f}  max {arr.max():.4f}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*", help="signals.db and/or candle CSV files")
    ap.add_argument("--payout", type=float, default=0.92)
    ap.add_argument("--expiry", type=int, default=1, help="expiry in candles")
    ap.add_argument("--test-fraction", type=float, default=0.35, dest="test_fraction")
    ap.add_argument("--synthetic", type=int, default=0, metavar="N",
                    help="no data? replay N bars from data.synthetic instead "
                         "(note: that generator has drift regimes built in, so "
                         "it is a demo, not a null)")
    ap.add_argument("--null", type=int, default=0, metavar="N",
                    help="replay N bars of a driftless random walk -- the honest "
                         "null, where any departure from 50%% is chance")
    ap.add_argument("--null-seeds", type=int, default=12, dest="null_seeds")
    args = ap.parse_args(argv)

    if args.null:
        print(f"NULL  driftless random walk, {args.null:,} bars per run.\n"
              f"Any win rate away from 50% here is sampling noise by construction.\n")
        print("as written")
        print(null_distribution(args.null, args.null_seeds, args.expiry, strict=False))
        print("\nwith the logic defects repaired")
        print(null_distribution(args.null, args.null_seeds, args.expiry, strict=True))
        return 0

    if args.paths:
        candles, sources = dataset.load(args.paths)
        print(dataset.summarise(candles, sources))
        if candles.empty:
            print("\nNothing to analyse.")
            return 1
        panel = dataset.to_panel(candles)
    elif args.synthetic:
        from ..data.synthetic import generate_synthetic_candles
        panel = {"SYNTHETIC": generate_synthetic_candles(args.synthetic, seed=11)}
        print(f"SYNTHETIC  {args.synthetic:,} bars, random walk with regime switches.\n"
              f"There is no edge in this series by construction, so the result below\n"
              f"is what the rules score on nothing -- a null, not a backtest.")
    else:
        ap.error("give candle sources, or --synthetic N")

    print(report(panel, payout=args.payout, expiry=args.expiry,
                 test_fraction=args.test_fraction))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
