"""Walk-forward backtester for the confluence engine (and individual strategies).

Simulates a binary option: at candle i, if a signal fires, "buy" a
CALL/PUT with a fixed expiry of ``expiry_candles`` candles. Win if the
close ``expiry_candles`` later is on the predicted side of the entry
close, lose otherwise (binary options have no partial fills). No lookahead:
every decision at candle i only ever sees ``df.iloc[:i+1]``.

This is intentionally simple (no spread/slippage model, since Pocket Option
prices options off its own OTC feed rather than a public order book) but it
is honest about the one number that matters most for binary options: the
break-even win rate implied by the payout, so a backtest win rate can be
read in context instead of taken as "profitable" on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import confluence
from .strategies import DOWN, FLAT, UP, Signal


@dataclass
class Trade:
    timestamp: pd.Timestamp
    direction: int
    score: float
    entry_price: float
    exit_price: float
    won: bool
    pnl: float
    reason: str


@dataclass
class BacktestReport:
    trades: list[Trade] = field(default_factory=list)
    payout: float = 0.85

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.won for t in self.trades) / len(self.trades)

    @property
    def net_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def expectancy(self) -> float:
        return self.net_pnl / self.n_trades if self.n_trades else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl for t in self.trades if t.pnl > 0)
        losses = -sum(t.pnl for t in self.trades if t.pnl < 0)
        return gains / losses if losses > 0 else float("inf") if gains > 0 else 0.0

    @property
    def max_drawdown(self) -> float:
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in self.trades:
            equity += t.pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        return max_dd

    @property
    def breakeven_win_rate(self) -> float:
        # Solve p*payout - (1-p)*1 = 0  =>  p = 1 / (1 + payout)
        return 1.0 / (1.0 + self.payout)

    def summary(self) -> str:
        return (
            f"trades={self.n_trades} win_rate={self.win_rate:.1%} "
            f"breakeven_needed={self.breakeven_win_rate:.1%} "
            f"net_pnl={self.net_pnl:+.2f}u expectancy={self.expectancy:+.3f}u/trade "
            f"profit_factor={self.profit_factor:.2f} max_drawdown={self.max_drawdown:.2f}u"
        )


def _resample_bias(df_entry: pd.DataFrame, bias_multiple: int) -> pd.DataFrame | None:
    if bias_multiple <= 1:
        return None
    rule = f"{bias_multiple}min"
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return df_entry.resample(rule).agg(agg).dropna()


def run_backtest(
    df: pd.DataFrame,
    expiry_candles: int = 1,
    payout: float = 0.85,
    threshold: float = 0.55,
    window: int = 260,
    warmup: int = 210,
    cooldown_candles: int = 3,
    bias_multiple: int = 5,
    strategy_fn=None,
) -> BacktestReport:
    """Backtest the full confluence engine, or a single strategy if ``strategy_fn`` is given.

    ``strategy_fn`` should have the signature ``(df) -> Signal`` (see
    ``strategies.py``) -- pass e.g. ``strategies.trend_following`` to compare
    one strategy in isolation against the combined confluence result.
    """
    df_bias_full = _resample_bias(df, bias_multiple)
    n = len(df)
    report = BacktestReport(payout=payout)
    cooldown = 0

    for i in range(warmup, n - expiry_candles):
        if cooldown > 0:
            cooldown -= 1
            continue

        window_df = df.iloc[max(0, i - window + 1): i + 1]

        if strategy_fn is not None:
            sig: Signal = strategy_fn(window_df)
            direction, score = sig.direction, sig.score
            is_signal = sig.is_actionable and sig.score >= threshold
            reason = sig.reason
        else:
            bias_window = None
            if df_bias_full is not None:
                ts = df.index[i]
                bias_window = df_bias_full.loc[:ts].iloc[-window:]
            result = confluence.evaluate(window_df, bias_window, threshold=threshold)
            direction, score, is_signal = result.direction, result.score, result.is_signal
            reason = result.describe()

        if not is_signal:
            continue

        entry_price = df["close"].iloc[i]
        exit_price = df["close"].iloc[i + expiry_candles]
        actual_dir = UP if exit_price > entry_price else (DOWN if exit_price < entry_price else FLAT)
        won = actual_dir == direction
        pnl = payout if won else -1.0

        report.trades.append(
            Trade(df.index[i], direction, score, entry_price, exit_price, won, pnl, reason)
        )
        cooldown = cooldown_candles

    return report
