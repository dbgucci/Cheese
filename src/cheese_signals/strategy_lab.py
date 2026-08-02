"""Compare strategies and expiries on your own recorded candles.

The journal stores every candle the engine has seen, which means a new
strategy idea can be tested against your broker's real OTC feed instead of
against synthetic data or a chart read in hindsight.

Everything here is walk-forward: at bar *i* a strategy is shown only
``df.iloc[:i+1]``. Outcomes are scored on **real** closes, never Heikin Ashi
values, because that is what the option settles against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import pandas as pd

from . import storage
from . import strategies as strat
from . import trend as trend_mod
from .strategies import DOWN, FLAT, UP

StrategyFn = Callable[[pd.DataFrame], strat.Signal]

STRATEGIES: dict[str, StrategyFn] = {
    "trend_continuation": trend_mod.trend_continuation,
    "liquidity_sweep": strat.liquidity_sweep,
    "mean_reversion": strat.mean_reversion,
    "price_action": strat.price_action,
}


@dataclass
class LabResult:
    strategy: str
    expiry: int
    trades: int
    wins: int
    pnl: float
    ups: int = 0
    downs: int = 0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    def edge(self, payout: float) -> float:
        return self.win_rate - 1.0 / (1.0 + payout)

    def line(self, payout: float) -> str:
        return (
            f"{self.strategy:20s} {self.expiry}min  "
            f"trades={self.trades:5d}  win={self.win_rate:6.1%}  "
            f"edge={self.edge(payout):+6.1%}  net={self.pnl:+9.2f}  "
            f"(B{self.ups}/S{self.downs})"
        )


def run(
    df: pd.DataFrame,
    strategy: str,
    expiry_minutes: int,
    payout: float = 0.85,
    min_score: float = 0.0,
    warmup: Optional[int] = None,
    cooldown: int = 3,
    stake: float = 10.0,
    lead_minutes: int = 0,
) -> LabResult:
    """Walk forward over ``df`` applying one strategy at one expiry.

    ``lead_minutes`` reproduces the live advance-warning delay: the signal is
    detected at bar *i* but entered at bar *i + lead*, which is what actually
    happens when a trade is announced ahead of time.
    """
    fn = STRATEGIES[strategy]
    n = len(df)
    warm = warmup if warmup is not None else (trend_mod.MIN_BARS if strategy == "trend_continuation" else 260)
    result = LabResult(strategy, expiry_minutes, 0, 0, 0.0)
    closes = df["close"].to_numpy()
    cool = 0

    last = n - (expiry_minutes + lead_minutes) - 1
    for i in range(warm, last):
        if cool > 0:
            cool -= 1
            continue

        sig = fn(df.iloc[: i + 1])
        if not sig.is_actionable or sig.score < min_score:
            continue

        entry_idx = i + lead_minutes
        exit_idx = entry_idx + expiry_minutes
        entry, exit_ = closes[entry_idx], closes[exit_idx]

        actual = UP if exit_ > entry else (DOWN if exit_ < entry else FLAT)
        won = actual == sig.direction and actual != FLAT

        result.trades += 1
        result.wins += int(won)
        result.pnl += stake * payout if won else -stake
        if sig.direction == UP:
            result.ups += 1
        else:
            result.downs += 1
        cool = cooldown

    return result


def sweep(
    df: pd.DataFrame,
    strategies: Optional[list[str]] = None,
    expiries: tuple[int, ...] = (1, 2, 3, 4, 5),
    payout: float = 0.85,
    **kwargs,
) -> list[LabResult]:
    out: list[LabResult] = []
    for name in strategies or list(STRATEGIES):
        for e in expiries:
            out.append(run(df, name, e, payout=payout, **kwargs))
    return out


def baseline(df: pd.DataFrame, expiry_minutes: int, payout: float = 0.85, stake: float = 10.0) -> str:
    """What always-BUY and always-SELL would score on the same candles.

    Any strategy that cannot beat these is not showing skill -- it is showing
    the market's drift, which reverses without warning.
    """
    closes = df["close"].to_numpy()
    ups = downs = flat = 0
    for i in range(len(closes) - expiry_minutes - 1):
        a, b = closes[i], closes[i + expiry_minutes]
        if b > a:
            ups += 1
        elif b < a:
            downs += 1
        else:
            flat += 1
    total = ups + downs + flat
    if not total:
        return "no data"
    be = 1.0 / (1.0 + payout)
    return (
        f"baseline {expiry_minutes}min: price rose {ups / total:.1%}, fell {downs / total:.1%}, "
        f"flat {flat / total:.1%}  |  always-BUY {ups / total:.1%} vs break-even {be:.1%}"
    )


def load_journal_candles(asset: Optional[str] = None, limit: int = 100_000) -> dict[str, pd.DataFrame]:
    """Load recorded candles from the journal, keyed by asset."""
    j = storage.Journal()
    try:
        cur = j._conn.execute("SELECT DISTINCT asset FROM candles")
        assets = [r[0] for r in cur.fetchall()]
        if asset:
            assets = [a for a in assets if a == asset]
        return {a: j.load_candles(a, limit) for a in assets}
    finally:
        j.close()
