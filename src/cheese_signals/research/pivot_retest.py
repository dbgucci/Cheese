"""Pivot breakout-and-retest, reconstructed from the QT Sniper recordings.

What the recordings actually establish about the strategy:

* the chart overlay is ``Smart Pivot Points (Daily -- M1/M5/M15/M30)``, whose
  formula :mod:`cheese_signals.pivots` pins exactly from three legible levels;
* the panel says it "watches for a real trend with strong momentum and trades
  it automatically -- stop-loss, take-profit, and a trailing stop all handled";
* the seller says it "adds retest entries" -- so entries are on a pullback to
  a broken level, not on the break itself;
* MODE is a preset pair, Scalper ("fast, frequent, smaller moves") and Swing
  ("slower, fewer trades, bigger moves"), which sets timeframe and risk together;
* ACCOUNT SIZE sets "position sizing, daily limits, and trailing stops in
  dollars";
* it was running NAS100 on M5 at 1.20 lots, quoted 30098.60 / 30099.70 -- a
  1.10-point spread, which is where :data:`SPREAD_POINTS` comes from.

None of that is exotic. Pivot breakout-with-retest is a documented, decades-old
intraday method, and this is a competent implementation of it rather than a
discovery. The important structural difference from everything else in this
repo is that it is **not** a binary option: the stop defines 1R, the target is
the next level up, and a strategy can win 40% of the time and still make money.
So this module scores in R-multiples and expectancy, never in win rate alone --
win rate on its own is meaningless once payoff is asymmetric.

Two honesty constraints are wired in rather than noted:

* **No lookahead.** Today's levels come from yesterday's completed session, the
  entry decision is taken on a closed bar and filled at the *next* bar's open.
* **Ties go against you.** If a bar's range spans both stop and target there is
  no way to know which printed first, so the stop is assumed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from .. import indicators as ind
from .. import pivots

LONG, SHORT = 1, -1

# Observed on the NAS100 ticket in the recording: 30098.60 sell / 30099.70 buy.
SPREAD_POINTS = 1.10


@dataclass
class Trade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    direction: int
    entry: float
    exit: float
    stop: float
    target: float
    level: float
    r_multiple: float
    reason: str


@dataclass
class Backtest:
    trades: list[Trade] = field(default_factory=list)
    bars: int = 0

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(t.r_multiple > 0 for t in self.trades)

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else float("nan")

    @property
    def expectancy(self) -> float:
        """Average R per trade. This is the number that decides everything."""
        return float(np.mean([t.r_multiple for t in self.trades])) if self.n else float("nan")

    @property
    def total_r(self) -> float:
        return float(np.sum([t.r_multiple for t in self.trades])) if self.n else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.r_multiple for t in self.trades if t.r_multiple > 0)
        losses = -sum(t.r_multiple for t in self.trades if t.r_multiple < 0)
        return gains / losses if losses else float("inf")

    def max_drawdown_r(self) -> float:
        if not self.n:
            return 0.0
        eq = np.cumsum([t.r_multiple for t in self.trades])
        return float(np.max(np.maximum.accumulate(eq) - eq))

    def summary(self, label: str = "") -> str:
        if not self.n:
            return f"{label}no trades"
        return (f"{label}n={self.n:<5d} win={self.win_rate:.3f}  "
                f"expectancy={self.expectancy:+.4f}R  total={self.total_r:+.1f}R  "
                f"PF={self.profit_factor:.2f}  maxDD={self.max_drawdown_r():.1f}R")


def _next_level_above(lad: np.ndarray, price: float) -> Optional[float]:
    above = lad[lad > price]
    return float(above[0]) if len(above) else None


def _next_level_below(lad: np.ndarray, price: float) -> Optional[float]:
    below = lad[lad < price]
    return float(below[-1]) if len(below) else None


def backtest(
    intraday: pd.DataFrame,
    adx_min: float = 20.0,
    retest_bars: int = 12,
    stop_buffer_atr: float = 0.5,
    trail_after_r: float = 1.0,
    trail_atr: float = 2.0,
    spread_points: float = SPREAD_POINTS,
    trend_fast: int = 50,
    trend_slow: int = 200,
    tie_break: str = "stop",
) -> Backtest:
    """Break a daily pivot level, retest it, enter if the trend still agrees.

    ``retest_bars`` is how long a break stays "live" while waiting for the
    pullback. ``trail_after_r`` and ``trail_atr`` implement the trailing stop
    the panel advertises: once a trade is one R in front, the stop follows
    price at a fixed ATR distance and never moves backwards.
    """
    df = intraday.dropna(subset=["open", "high", "low", "close"]).copy()
    if len(df) < trend_slow + 10:
        return Backtest([], 0)

    lv = pivots.align(df, pivots.daily_levels(pivots.to_daily(df)))
    ok = lv.notna().all(axis=1)

    ema_f = ind.ema(df["close"], trend_fast).to_numpy()
    ema_s = ind.ema(df["close"], trend_slow).to_numpy()
    adx = ind.adx(df["high"], df["low"], df["close"]).to_numpy()
    atr = ind.atr(df["high"], df["low"], df["close"]).to_numpy()

    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    idx = df.index

    trades: list[Trade] = []
    # A pending break: level, direction, and the bar it must be retested by.
    pending: dict[float, tuple[int, int]] = {}
    open_trade: Optional[dict] = None

    for i in range(trend_slow, len(df) - 1):
        # ---- manage an open position first (it owns this bar) --------------
        if open_trade is not None:
            t = open_trade
            hit_stop = (l[i] <= t["stop"]) if t["dir"] == LONG else (h[i] >= t["stop"])
            hit_tgt = (h[i] >= t["target"]) if t["dir"] == LONG else (l[i] <= t["target"])
            exit_px = reason = None
            if hit_stop and hit_tgt:
                # The bar spans both. Without tick data there is no way to know
                # which printed first, and the choice is not cosmetic: when the
                # stop sits inside a typical bar's range, always resolving it
                # as a loss costs about half an R per trade on a series with no
                # edge in it. Run both ways and read the bracket.
                if tie_break == "stop":
                    exit_px, reason = t["stop"], "stop"
                else:
                    exit_px, reason = t["target"], "target"
            elif hit_stop:
                exit_px, reason = t["stop"], "stop"
            elif hit_tgt:
                exit_px, reason = t["target"], "target"

            if exit_px is None and np.isfinite(atr[i]):
                # trailing stop, once far enough in front
                move = (c[i] - t["entry"]) * t["dir"]
                if move >= trail_after_r * t["risk"]:
                    trail = (c[i] - trail_atr * atr[i]) if t["dir"] == LONG \
                        else (c[i] + trail_atr * atr[i])
                    t["stop"] = max(t["stop"], trail) if t["dir"] == LONG \
                        else min(t["stop"], trail)

            if exit_px is not None:
                r = ((exit_px - t["entry"]) * t["dir"] - spread_points) / t["risk"]
                trades.append(Trade(t["time"], idx[i], t["dir"], t["entry"], exit_px,
                                    t["stop"], t["target"], t["level"], r, reason))
                open_trade = None
            continue

        if not ok.iloc[i] or not np.isfinite(atr[i]) or not np.isfinite(adx[i]):
            continue

        lad = pivots.ladder(lv.iloc[i])
        if lad.size == 0:
            continue

        # ---- record fresh breaks -------------------------------------------
        for lvl in lad:
            if c[i - 1] <= lvl < c[i]:
                pending[float(lvl)] = (LONG, i + retest_bars)
            elif c[i - 1] >= lvl > c[i]:
                pending[float(lvl)] = (SHORT, i + retest_bars)
        pending = {k: v for k, v in pending.items() if v[1] >= i}

        # ---- look for a retest that holds ----------------------------------
        trending_up = ema_f[i] > ema_s[i]
        for lvl, (direction, _) in list(pending.items()):
            if adx[i] < adx_min:
                continue
            if direction == LONG and not trending_up:
                continue
            if direction == SHORT and trending_up:
                continue
            # touched the level this bar and closed back on the break side
            if direction == LONG:
                retested = l[i] <= lvl and c[i] > lvl
                target = _next_level_above(lad, c[i])
                stop = lvl - stop_buffer_atr * atr[i]
            else:
                retested = h[i] >= lvl and c[i] < lvl
                target = _next_level_below(lad, c[i])
                stop = lvl + stop_buffer_atr * atr[i]
            if not retested or target is None:
                continue

            entry = o[i + 1]                       # fill on the next bar's open
            risk = abs(entry - stop)
            if risk <= 0 or abs(target - entry) < risk * 0.5:
                continue                            # not worth at least 0.5R
            open_trade = {"time": idx[i + 1], "dir": direction, "entry": entry,
                          "stop": stop, "target": target, "risk": risk, "level": lvl}
            pending.pop(lvl, None)
            break

    return Backtest(trades, bars=len(df))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv", nargs="?", help="intraday OHLC csv with a ts column")
    ap.add_argument("--adx-min", type=float, default=20.0, dest="adx_min")
    ap.add_argument("--synthetic", type=int, default=0, metavar="N")
    args = ap.parse_args(argv)

    if args.csv:
        df = pd.read_csv(args.csv)
        tcol = next(c for c in df.columns if c.lower() in ("ts", "time", "date", "datetime"))
        df[tcol] = pd.to_datetime(df[tcol], utc=True)
        df = df.set_index(tcol).sort_index()
        df.columns = [c.lower() for c in df.columns]
    elif args.synthetic:
        from .precision_reversal import driftless_walk
        df = driftless_walk(args.synthetic, start_price=30000.0, vol=0.0004, seed=5)
        print("SYNTHETIC driftless walk -- there is no edge here by construction.\n"
              "Expectancy should land near -(spread/risk) per trade, not above zero.\n")
    else:
        ap.error("give a csv, or --synthetic N")

    pess = backtest(df, adx_min=args.adx_min, tie_break="stop")
    opt = backtest(df, adx_min=args.adx_min, tie_break="target")
    print(pess.summary("pessimistic    "))
    print(opt.summary("optimistic     "))
    print("  the truth is between these two; a strategy is only worth trading "
          "if the\n  pessimistic bound clears zero.")
    bt = pess
    if bt.n:
        longs = Backtest([t for t in bt.trades if t.direction == LONG], bt.bars)
        shorts = Backtest([t for t in bt.trades if t.direction == SHORT], bt.bars)
        print(longs.summary("  long         "))
        print(shorts.summary("  short        "))
        by = {}
        for t in bt.trades:
            by.setdefault(t.reason, []).append(t.r_multiple)
        for reason, rs in sorted(by.items()):
            print(f"  exit={reason:8s} n={len(rs):<5d} mean={np.mean(rs):+.3f}R")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
