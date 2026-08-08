"""The cost wall: what a strategy must clear before it is worth writing.

Why this module exists at all
-----------------------------
The Pocket Option work failed for a reason that had nothing to do with the
signals. The payout was 85%, so break-even was 54.05%, and 1,543 trades came
in at 51.8%. The gap was 2.2 points and no indicator combination closed it,
because the house edge was larger than any edge available on the timeframe.

CFDs have the same structure wearing different clothes. The spread is the
house edge, it is charged twice per round trip, and it is charged whether or
not the trade works. So the first question is not "which strategy" but:

    given this broker's actual spread on this instrument, how far does price
    have to travel before a trade is even worth taking -- and how often does
    it travel that far?

Call that the **cost wall**. If the typical move over the intended holding
period is 3x the round-trip cost, a strategy has room to be mediocre and
still make money. If it is 1.2x, the strategy needs to be extraordinary, and
nothing in the published literature is extraordinary.

This is not a theoretical concern. Mesfin (2026) tested fourteen families of
intraday signals on Nasdaq micro futures across 947 days, and found gross
edges of 0.07 to 1.50 points per trade against a two-point round-trip cost.
Every family failed, not because the signals were worthless, but because the
wall was higher than the signals were tall. That study is the single closest
match to "scalp NAS100", and its answer is no.

The measurement here uses the broker's own quoted spreads rather than the
advertised ones, because those are the numbers that will actually be charged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

# A strategy needs the available move to be some multiple of the round-trip
# cost before it has any room to work. These thresholds are judgement, not
# measurement, and they are deliberately not subtle:
#
#   below 2x   the cost eats a mediocre edge entirely -- do not trade this
#   2x to 3x   possible, but only with an unusually strong signal
#   above 3x   ordinary published edges have room to survive
COST_RATIO_UNVIABLE = 2.0
COST_RATIO_MARGINAL = 3.0


@dataclass
class InstrumentCost:
    """What one instrument costs to trade, and what it offers in return."""

    symbol: str
    horizon_minutes: int
    spread_points_median: float
    spread_points_p90: float
    commission_points: float
    move_points_median: float          # |close-to-close| over the horizon
    move_points_p75: float
    bars: int

    @property
    def round_trip_points(self) -> float:
        """Spread crossed once on entry and once on exit, plus commission.

        The spread is quoted as the full bid/ask distance, and a market order
        pays it in full on the way in and again on the way out. Halving it --
        "I only pay half the spread" -- is a way of arriving at a number that
        makes a strategy look viable when it is not.
        """
        return self.spread_points_median + self.commission_points

    @property
    def ratio(self) -> float:
        """Typical available move divided by what it costs to capture it."""
        cost = self.round_trip_points
        return float(self.move_points_median / cost) if cost > 0 else float("inf")

    @property
    def ratio_p75(self) -> float:
        cost = self.round_trip_points
        return float(self.move_points_p75 / cost) if cost > 0 else float("inf")

    @property
    def verdict(self) -> str:
        if self.ratio < COST_RATIO_UNVIABLE:
            return "unviable"
        if self.ratio < COST_RATIO_MARGINAL:
            return "marginal"
        return "viable"

    @property
    def breakeven_win_rate(self) -> float:
        """Win rate needed at a 1:1 reward/risk, given the cost drag.

        Stated this way because it is directly comparable to the Pocket
        Option number that made that project unwinnable: a break-even of
        54.05% on a payout of 0.85. If an instrument and horizon imply a
        break-even above roughly that, it is the same trap in a new venue.
        """
        edge = self.move_points_median
        cost = self.round_trip_points
        if edge <= 0:
            return 1.0
        # Win  -> +edge - cost ; Lose -> -edge - cost.  Solve for p at zero EV.
        win, loss = edge - cost, edge + cost
        total = win + loss
        return float(min(max(loss / total, 0.0), 1.0)) if total > 0 else 1.0

    def summary(self) -> str:
        return (
            f"{self.symbol} @ {self.horizon_minutes}min: "
            f"cost {self.round_trip_points:.1f}pt, "
            f"typical move {self.move_points_median:.1f}pt, "
            f"ratio {self.ratio:.2f}x, "
            f"break-even {self.breakeven_win_rate:.1%} -> {self.verdict.upper()}"
        )


def spread_points(bars: pd.DataFrame, point: float = 1.0) -> pd.Series:
    """Spread per bar, in points.

    MetaTrader reports ``spread`` per bar already in points, so this mostly
    normalises the alternatives: an explicit bid/ask pair if the feed carries
    one, otherwise the recorded spread column.
    """
    if "spread" in bars:
        return bars["spread"].astype(float)
    if {"ask", "bid"} <= set(bars.columns):
        return (bars["ask"] - bars["bid"]).astype(float) / point
    raise KeyError("bars carry neither a 'spread' column nor 'ask'/'bid'")


def measure(
    bars: pd.DataFrame,
    symbol: str,
    horizon_minutes: int,
    point: float = 1.0,
    commission_points: float = 0.0,
    session_hours: Optional[tuple[int, int]] = None,
) -> InstrumentCost:
    """Measure the cost wall for one instrument at one holding period.

    ``bars`` is 1-minute OHLC with a spread column, indexed by UTC time.
    ``session_hours`` restricts the measurement to a UTC hour window, because
    a spread measured across the Asian session on an index CFD describes a
    market nobody is trading.
    """
    df = bars
    if session_hours is not None:
        lo, hi = session_hours
        hours = df.index.hour
        df = df[(hours >= lo) & (hours < hi)] if lo <= hi else df[(hours >= lo) | (hours < hi)]
    if df.empty:
        raise ValueError(f"no bars for {symbol} in the requested window")

    sp = spread_points(df, point)
    # The move actually available over the horizon: how far price gets from
    # here to there, unsigned. Not the high-to-low range -- nobody captures
    # the full range, and using it is how a backtest flatters itself.
    close = df["close"].astype(float)
    move = (close.shift(-horizon_minutes) - close).abs().dropna() / point

    return InstrumentCost(
        symbol=symbol,
        horizon_minutes=horizon_minutes,
        spread_points_median=float(sp.median()),
        spread_points_p90=float(sp.quantile(0.90)),
        commission_points=float(commission_points),
        move_points_median=float(move.median()),
        move_points_p75=float(move.quantile(0.75)),
        bars=int(len(df)),
    )


def sweep(
    bars_by_symbol: dict[str, pd.DataFrame],
    horizons: tuple[int, ...] = (1, 5, 15, 30, 60, 240),
    points: Optional[dict[str, float]] = None,
    commissions: Optional[dict[str, float]] = None,
    session_hours: Optional[dict[str, tuple[int, int]]] = None,
) -> pd.DataFrame:
    """Every instrument against every candidate holding period.

    The output is the decision table: it says which combinations are worth
    writing a strategy for, before any strategy exists to be defended.
    """
    points = points or {}
    commissions = commissions or {}
    session_hours = session_hours or {}
    rows = []
    for symbol, bars in bars_by_symbol.items():
        for h in horizons:
            try:
                c = measure(
                    bars, symbol, h,
                    point=points.get(symbol, 1.0),
                    commission_points=commissions.get(symbol, 0.0),
                    session_hours=session_hours.get(symbol),
                )
            except (ValueError, KeyError):
                continue
            rows.append({
                "symbol": symbol,
                "horizon_min": h,
                "cost_pts": round(c.round_trip_points, 2),
                "spread_p90": round(c.spread_points_p90, 2),
                "move_pts": round(c.move_points_median, 2),
                "ratio": round(c.ratio, 2),
                "ratio_p75": round(c.ratio_p75, 2),
                "breakeven_wr": round(c.breakeven_win_rate, 4),
                "verdict": c.verdict,
                "bars": c.bars,
            })
    columns = ["symbol", "horizon_min", "cost_pts", "spread_p90", "move_pts",
               "ratio", "ratio_p75", "breakeven_wr", "verdict", "bars"]
    if not rows:
        # A broker that offered nothing is a reportable outcome, not a crash;
        # the caller still needs a frame with the right shape to render.
        return pd.DataFrame(columns=columns)
    out = pd.DataFrame(rows, columns=columns)
    return out.sort_values(["symbol", "horizon_min"]).reset_index(drop=True)


def spread_by_hour(bars: pd.DataFrame, point: float = 1.0) -> pd.DataFrame:
    """Spread by UTC hour -- where the day's cheap and expensive hours are.

    Brokers widen spreads outside the liquid sessions and around scheduled
    releases. A strategy that trades at 22:00 UTC on a 3x-wider spread is a
    different strategy from the same rules run at 14:00, and this is how that
    shows up before it shows up in the P/L.
    """
    sp = spread_points(bars, point)
    g = sp.groupby(bars.index.hour)
    out = pd.DataFrame({
        "median": g.median(),
        "p90": g.quantile(0.90),
        "max": g.max(),
        "bars": g.size(),
    })
    out.index.name = "utc_hour"
    return out.round(2)


def verdict_lines(table: pd.DataFrame) -> list[str]:
    """Plain sentences for the report, naming what survives and what does not."""
    if table.empty:
        return ["No cost measurements available."]
    lines = []
    for symbol, g in table.groupby("symbol"):
        viable = g[g.verdict != "unviable"].sort_values("horizon_min")
        if viable.empty:
            worst = g.sort_values("ratio", ascending=False).iloc[0]
            lines.append(
                f"{symbol}: nothing clears the cost wall. Best case is "
                f"{worst.horizon_min}min at {worst.ratio:.2f}x cost "
                f"({worst.breakeven_wr:.1%} break-even win rate). Do not trade this."
            )
            continue
        shortest = viable.iloc[0]
        lines.append(
            f"{symbol}: viable from {shortest.horizon_min}min holds "
            f"({shortest.ratio:.2f}x cost, {shortest.breakeven_wr:.1%} break-even). "
            f"Shorter than that the spread takes more than the move."
        )
    return lines
