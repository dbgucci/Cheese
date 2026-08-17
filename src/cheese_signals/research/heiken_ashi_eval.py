"""Evaluate the Heiken Ashi and S/R Telegram bots.

Five files, two strategies. The four Heiken Ashi bots differ only in data
source (OANDA vs a Pocket Option WebSocket), timeframe (M5 vs M1) and how the
Telegram card is formatted -- their strategy blocks are byte-identical. The
fifth is a support/resistance + candlestick-pattern bot.

Both are scored here the way their own scheduler actually settles a trade,
which turns out not to be the way their own message says it does. That
discrepancy is the single most consequential thing in this module, so it is
measured rather than described:

* ``settle_offset=0`` -- enter at the open of candle k, settle at the close of
  candle k. This is a "1 minute trade", which is what the card promises and
  what the user buys on Pocket Option.
* ``settle_offset=1`` -- enter at the open of candle k, settle at the close of
  candle k+1. This is what the code does, because ``expiry_at`` is set to
  ``execute_at + 1 candle`` and the settlement price is then read as the close
  of the candle *starting* at ``expiry_at``.

Everything else here exists to put a number on a claim that is otherwise
argued from the code: whether the confidence score varies, whether it means
the same thing on EURUSD and USDJPY, how often the S/R filter is actually
binding, and what the martingale does to a reported win rate.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats

from .precision_reversal import driftless_walk
from .validate import breakeven

BUY = "BUY"
SELL = "SELL"


# ---------------------------------------------------------------------------
# the strategy, ported verbatim
# ---------------------------------------------------------------------------
def heiken_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """The bots' own HA, including its recursive open.

    Reproduced rather than reused from ``indicators.heikin_ashi`` so that any
    difference in the result is a difference in the strategy, not in the
    helper it was rewritten against.
    """
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    ha_close = (o + h + l + c) / 4.0
    ha_open = np.empty(len(df))
    if len(df):
        ha_open[0] = (o.iloc[0] + c.iloc[0]) / 2.0
    hc = ha_close.to_numpy()
    for i in range(1, len(df)):
        ha_open[i] = (ha_open[i - 1] + hc[i - 1]) / 2.0
    ha_open_s = pd.Series(ha_open, index=df.index)
    ha_high = pd.concat([ha_open_s, ha_close, h], axis=1).max(axis=1)
    ha_low = pd.concat([ha_open_s, ha_close, l], axis=1).min(axis=1)
    return pd.DataFrame({"HA_Open": ha_open_s, "HA_Close": ha_close,
                         "HA_High": ha_high, "HA_Low": ha_low})


def is_strong_ha(row, direction: str) -> bool:
    body = abs(row["HA_Close"] - row["HA_Open"])
    range_ = row["HA_High"] - row["HA_Low"]
    if range_ == 0:
        return False
    if direction == "bullish":
        wick_ok = row["HA_Low"] == min(row["HA_Open"], row["HA_Close"])
    else:
        wick_ok = row["HA_High"] == max(row["HA_Open"], row["HA_Close"])
    return (body / range_) > 0.6 and wick_ok


def confidence(row, ema_value: float) -> int:
    """The bots' confidence score, verbatim -- including the hardcoded 0.001.

    That constant is an *absolute* price distance. On EURUSD (~1.08) it is 10
    pips and the term is a real gradient; on USDJPY (~150) it is a tenth of a
    pip and the term is pinned at its maximum forever. The same number on the
    card therefore means two different things depending on the pair.
    """
    body = abs(row["HA_Close"] - row["HA_Open"])
    range_ = row["HA_High"] - row["HA_Low"]
    body_score = (body / range_) * 40 if range_ != 0 else 0
    ema_dist = abs(row["HA_Close"] - ema_value)
    ema_score = min(ema_dist / 0.001, 1.0) * 30
    direction = "bullish" if row["HA_Close"] > row["HA_Open"] else "bearish"
    wick_score = 30 if is_strong_ha(row, direction) else 0
    return int(round(min(100, body_score + ema_score + wick_score)))


def volatility_mask(df: pd.DataFrame) -> np.ndarray:
    """The bots' volatility gate, vectorised.

    They compute it per fetch as ``vol.iloc[-1] > 0.7 * vol.iloc[-50:].mean()``
    where ``vol`` is a 14-bar mean range. That is a 50-bar mean of a 14-bar
    mean evaluated at the last bar, so it is exactly a second rolling window
    -- and computing it that way is O(n) instead of O(n^2).
    """
    rng = (df["high"] - df["low"])
    vol = rng.rolling(window=14).mean()
    avg = vol.rolling(window=50).mean()
    return (vol > 0.7 * avg).to_numpy()


@dataclass
class Signal:
    index: int
    direction: str
    confidence: int


def ha_signals(df: pd.DataFrame, warmup: int = 210,
               cooldown_bars: int = 5) -> list[Signal]:
    """Every bar the HA bots would have alerted on.

    The live bots re-fetch and re-scan every 10 seconds, so they evaluate a
    candle that has not closed yet; here the decision is made on closed bars
    only. That makes this evaluation *more* favourable than the real bot, not
    less -- an unclosed candle's body/range ratio changes as it forms, so the
    live "strong candle" test fires on a shape that frequently no longer
    exists a few seconds later.
    """
    ha = heiken_ashi(df)
    ema200 = df["close"].ewm(span=200, adjust=False).mean()
    closes = df["close"].to_numpy()
    ema = ema200.to_numpy()

    bull = (ha["HA_Close"] > ha["HA_Open"]).to_numpy()
    bear = (ha["HA_Close"] < ha["HA_Open"]).to_numpy()
    vol_ok = volatility_mask(df)

    out: list[Signal] = []
    last_fired = -10**9
    for i in range(warmup, len(df)):
        if i - last_fired < cooldown_bars:
            continue
        if not vol_ok[i]:
            continue
        row = ha.iloc[i]
        if bull[i - 2] and bull[i - 1] and is_strong_ha(row, "bullish") and closes[i] > ema[i]:
            out.append(Signal(i, BUY, confidence(row, ema[i])))
            last_fired = i
        elif bear[i - 2] and bear[i - 1] and is_strong_ha(row, "bearish") and closes[i] < ema[i]:
            out.append(Signal(i, SELL, confidence(row, ema[i])))
            last_fired = i
    return out


# ---------------------------------------------------------------------------
# settlement
# ---------------------------------------------------------------------------
@dataclass
class Score:
    wins: int = 0
    losses: int = 0
    refunds: int = 0
    confidences: list[int] = field(default_factory=list)

    @property
    def n(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else float("nan")

    def ev(self, payout: float) -> float:
        return self.win_rate * payout - (1 - self.win_rate) if self.n else float("nan")


def settle(df: pd.DataFrame, signals: list[Signal], settle_offset: int = 0) -> Score:
    """Entry at the open of the bar after the signal; settlement per the offset.

    ``settle_offset=0`` is the 1-candle trade the card advertises;
    ``settle_offset=1`` is the 2-candle hold the scheduler actually measures.
    """
    opens = df["open"].to_numpy()
    closes = df["close"].to_numpy()
    s = Score()
    for sig in signals:
        entry_i = sig.index + 1
        exit_i = entry_i + settle_offset
        if exit_i >= len(df):
            continue
        move = closes[exit_i] - opens[entry_i]
        s.confidences.append(sig.confidence)
        if move == 0:
            s.refunds += 1
        elif (move > 0) == (sig.direction == BUY):
            s.wins += 1
        else:
            s.losses += 1
    return s


def martingale_math(p: float, payout: float, extra_attempts: int = 2) -> dict:
    """What a 1-2-4 martingale does to the reported win rate and to the money.

    The bots announce WIN as soon as any attempt lands and only announce LOSS
    after every attempt has failed, so the channel shows the probability of
    *a sequence* succeeding, not the probability of a trade winning. The two
    numbers are far apart and only one of them pays.
    """
    n = 1 + extra_attempts
    displayed = 1 - (1 - p) ** n
    ev = 0.0
    staked = 0.0
    for k in range(n):                     # win on attempt k (0-indexed)
        prob = ((1 - p) ** k) * p
        spent = 2 ** k - 1                 # 1+2+...+2^(k-1)
        ev += prob * (payout * (2 ** k) - spent)
        staked += prob * (2 ** k - 1 + 2 ** k)
    prob_all_lose = (1 - p) ** n
    total = 2 ** n - 1
    ev += prob_all_lose * (-total)
    staked += prob_all_lose * total
    return {"displayed_win_rate": displayed, "ev_per_sequence": ev,
            "expected_stake": staked, "ev_per_unit_staked": ev / staked if staked else 0.0}


# ---------------------------------------------------------------------------
# the S/R bot's filters
# ---------------------------------------------------------------------------
def sr_levels(df: pd.DataFrame, lookback: int = 20):
    support, resistance = [], []
    lows, highs = df["low"].to_numpy(), df["high"].to_numpy()
    for i in range(lookback, len(df) - lookback):
        if lows[i] == lows[i - lookback: i + lookback].min():
            support.append(lows[i])
        if highs[i] == highs[i - lookback: i + lookback].max():
            resistance.append(highs[i])
    return support, resistance


def sr_gate_rate(df: pd.DataFrame, window: int = 200, samples: int = 400,
                 tol: float = 0.002, seed: int = 0) -> dict:
    """How often "price is near support/resistance" is true at all.

    A filter that is almost always satisfied is not a filter. The bot keeps
    *every* pivot found in a 200-bar window and asks whether price is within
    0.2% of any of them, which on a 1-minute forex series is a wide net over a
    crowded field.
    """
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, max(1, len(df) - window), size=samples)
    near_s = near_r = 0
    counts = []
    for st in starts:
        w = df.iloc[st: st + window]
        sup, res = sr_levels(w)
        counts.append(len(sup) + len(res))
        price = float(w["close"].iloc[-1])
        near_s += any(abs(price - lvl) / max(lvl, 1e-9) < tol for lvl in sup)
        near_r += any(abs(price - lvl) / max(lvl, 1e-9) < tol for lvl in res)
    return {"near_support": near_s / samples, "near_resistance": near_r / samples,
            "mean_levels": float(np.mean(counts))}


def star_doji_rate(df: pd.DataFrame, threshold: float = 0.1) -> float:
    """Fraction of bars passing the Morning/Evening Star "doji" test.

    The bot writes it as ``abs(c2 - o2) < 0.1``, an absolute price threshold.
    On EURUSD 0.1 is a thousand pips, so the middle candle of a star is
    unconstrained and the pattern collapses into "down bar, any bar, up bar".
    """
    body = (df["close"] - df["open"]).abs().to_numpy()
    return float((body < threshold).mean())


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def report(bars: int = 40000, seeds: int = 8, payout: float = 0.85) -> str:
    lines: list[str] = []
    be = breakeven(payout)

    # --- HA strategy on a driftless null, both settlement conventions -------
    lines.append("HEIKEN ASHI, on driftless random walks (no edge by construction)")
    runs = []
    for s in range(seeds):
        df = driftless_walk(bars, seed=4000 + s)
        sigs = ha_signals(df)
        runs.append((df, sigs))
    for offset, label in ((0, "1 candle  (what the card promises)"),
                          (1, "2 candles (what the code settles)")):
        rates, ns = [], []
        for df, sigs in runs:
            sc = settle(df, sigs, settle_offset=offset)
            if sc.n:
                rates.append(sc.win_rate)
                ns.append(sc.n)
        if rates:
            a = np.array(rates)
            lines.append(f"  {label}: n={int(np.mean(ns)):,}/run  "
                         f"win rate mean {a.mean():.4f} sd {a.std(ddof=1):.4f}  "
                         f"EV@{payout:.2f} {a.mean() * payout - (1 - a.mean()):+.4f}")
    lines.append(f"  break-even at a {payout:.0%} payout is {be:.4f}")

    # --- the confidence score ----------------------------------------------
    lines.append("\nTHE CONFIDENCE SCORE")
    for price, name in ((1.08, "EURUSD-like (~1.08)"), (150.0, "USDJPY-like (~150)")):
        confs = []
        for s in range(seeds):
            df = driftless_walk(bars, start_price=price, vol=0.00012, seed=7000 + s)
            confs += [sig.confidence for sig in ha_signals(df)]
        if confs:
            c = np.array(confs)
            lines.append(f"  {name:22s} n={len(c):,}  min={c.min()} max={c.max()} "
                         f"mean={c.mean():.1f} sd={c.std(ddof=1):.2f}  "
                         f"unique values={len(np.unique(c))}")
    lines.append("  (the wick term is always 30: is_strong_ha is already required to fire)")

    # --- martingale ---------------------------------------------------------
    lines.append("\nMARTINGALE, 1-2-4 over 3 attempts")
    lines.append(f"  {'true win rate':>14s}  {'channel shows':>14s}  "
                 f"{'EV/sequence':>12s}  {'EV/unit staked':>15s}")
    for p in (0.50, 0.52, 0.55):
        m = martingale_math(p, payout)
        lines.append(f"  {p:>14.2%}  {m['displayed_win_rate']:>14.2%}  "
                     f"{m['ev_per_sequence']:>+12.4f}  {m['ev_per_unit_staked']:>+15.4f}")
    lines.append("  the channel's win rate is the chance a *sequence* lands, not a trade")

    # --- the S/R bot's filters ---------------------------------------------
    lines.append("\nS/R BOT FILTERS")
    df = driftless_walk(60000, start_price=1.08, seed=21)
    g = sr_gate_rate(df)
    lines.append(f"  levels kept per 200-bar window: {g['mean_levels']:.1f}")
    lines.append(f"  P(price within 0.2% of any support)    = {g['near_support']:.3f}")
    lines.append(f"  P(price within 0.2% of any resistance) = {g['near_resistance']:.3f}")
    lines.append(f"  bars passing the star 'doji' test (|body| < 0.1) on a ~1.08 series: "
                 f"{star_doji_rate(df):.4f}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bars", type=int, default=40000)
    ap.add_argument("--seeds", type=int, default=8)
    ap.add_argument("--payout", type=float, default=0.85)
    args = ap.parse_args(argv)
    print(report(args.bars, args.seeds, args.payout))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
