"""Walk-forward simulation of the opening-range rules, with the spread charged.

This module exists to try to talk you out of trading. That is not a joke: the
Pocket Option half of this repository is a record of what happens when a
strategy is judged on how sensible it sounds, and the cost wall in
``costs.py`` is there because the answer for short-horizon index scalping was
already known to be no. So the defaults here are chosen to make a strategy
look worse rather than better, and every place where an assumption had to be
made, the assumption favours losing.

The four assumptions that decide whether an opening-range backtest is honest
-----------------------------------------------------------------------------
**1. Which came first inside the bar.** A one-minute bar whose range contains
both the stop and the target could have hit either first, and the OHLC does
not say which. Optimism here is worth several points of win rate on a
strategy where stops and targets are only a few minutes apart, and it is
completely invisible in the output. So: the stop is assumed to have been hit,
and the number of trades resolved that way is reported. If a large share of
the result depends on those bars, the result is an artefact of this assumption
and not a finding about the market.

**2. Bars are bid, fills are not.** MetaTrader history is the bid series. A
long is filled at the ask -- one spread higher -- and then exits back on the
bid, so it pays the spread once, and its stop is effectively that much nearer
than the chart suggests. Modelling this as a flat deduction from the profit
gets the money roughly right and the stop distance wrong, which flatters the
win rate. Here the fill prices are constructed on the correct side of the
book and the exits are tested against the correct side, so the spread shows up
in both places.

**3. The spread charged is the one quoted that morning**, taken from the
opening range's own bars, not a long-run average. Averaging hides the widened
mornings, and the widened mornings are precisely the ones a live bot trades
through.

**4. The bar that triggers a close-through entry cannot also resolve it.** The
entry price is that bar's close, so the position does not exist until the bar
does. Allowing the same bar to hit the target is a one-bar lookahead and it
is worth a great deal of imaginary profit.

Results are reported in **R** -- multiples of the risk taken -- rather than in
money, because R is the only unit that survives a change of account size,
instrument or leverage. Money follows from R once a risk fraction is chosen,
and ``OrbReport.money`` does that conversion in one place instead of baking an
account size into every number.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from . import orb
from .clock import SessionSpec, session_for
from .execution import BUY, SELL
from .orb import OrbConfig, TradePlan

EXIT_STOP = "stop"
EXIT_TARGET = "target"
EXIT_BREAKEVEN = "breakeven"
EXIT_SESSION_CLOSE = "session close"


@dataclass(frozen=True)
class SimConfig:
    """How the simulation resolves what the data cannot tell it."""

    commission_points: float = 0.0
    fallback_spread_points: float = 0.0    # used only when bars carry no spread
    slippage_points: float = 0.0           # added to every fill, against the trade
    pessimistic_fills: bool = True         # ambiguous bar -> the stop was hit
    zero_cost: bool = False                # the counterfactual: what costs are eating
    invert: bool = False                   # the baseline: take every signal backwards


@dataclass(frozen=True)
class Trade:
    """One round trip, priced on the correct side of the book at both ends."""

    symbol: str
    session_date: date
    direction: int
    entry_at: datetime
    entry: float
    stop: float                 # as placed: the stop that defined the risk
    final_stop: float           # where it ended up, if breakeven moved it
    target: float
    risk_points: float
    exit_at: datetime
    exit: float
    exit_reason: str
    point_size: float
    spread_points: float        # already inside the entry and exit fills
    commission_points: float    # charged separately, as brokers charge it
    ambiguous: bool
    range_points: float
    reason: str

    @property
    def side(self) -> str:
        return "BUY" if self.direction == BUY else "SELL"

    @property
    def cost_points(self) -> float:
        return self.spread_points + self.commission_points

    @property
    def net_points(self) -> float:
        """Points made. The spread is inside the fills; commission is not."""
        raw = (self.exit - self.entry) * (1 if self.direction == BUY else -1)
        return raw / self.point_size - self.commission_points

    @property
    def r(self) -> float:
        return self.net_points / self.risk_points if self.risk_points else 0.0

    @property
    def won(self) -> bool:
        return self.net_points > 0

    def line(self) -> str:
        return (f"{self.entry_at:%Y-%m-%d %H:%M}  {self.symbol:10s} {self.side:4s} "
                f"{self.r:+5.2f}R  {self.exit_reason:13s} "
                f"risk {self.risk_points:5.0f}pt  cost {self.cost_points:4.1f}pt"
                f"{'  [ambiguous bar]' if self.ambiguous else ''}")


@dataclass(frozen=True)
class Skip:
    """A session that was not traded, and the reason it was not."""

    symbol: str
    session_date: date
    reason: str


@dataclass
class OrbReport:
    trades: list[Trade] = field(default_factory=list)
    skips: list[Skip] = field(default_factory=list)
    sessions: int = 0
    symbols: list[str] = field(default_factory=list)
    window: Optional[tuple[datetime, datetime]] = None
    sessions_by_symbol: dict[str, int] = field(default_factory=dict)

    # ------------------------------------------------------------- summary
    @property
    def count(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.won)

    @property
    def win_rate(self) -> float:
        return self.wins / self.count if self.count else 0.0

    @property
    def net_r(self) -> float:
        return sum(t.r for t in self.trades)

    @property
    def expectancy_r(self) -> float:
        """Average R per trade. The number that decides everything."""
        return self.net_r / self.count if self.count else 0.0

    @property
    def cost_points(self) -> float:
        return sum(t.cost_points for t in self.trades)

    @property
    def ambiguous(self) -> int:
        return sum(1 for t in self.trades if t.ambiguous)

    @property
    def profit_factor(self) -> float:
        gains = sum(t.r for t in self.trades if t.r > 0)
        losses = -sum(t.r for t in self.trades if t.r < 0)
        if losses <= 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    @property
    def max_drawdown_r(self) -> float:
        peak, equity, worst = 0.0, 0.0, 0.0
        for t in sorted(self.trades, key=lambda t: t.entry_at):
            equity += t.r
            peak = max(peak, equity)
            worst = min(worst, equity - peak)
        return worst

    @property
    def trade_rate(self) -> float:
        """Trades per session offered. Tells you if the filters are strangling it."""
        return self.count / self.sessions if self.sessions else 0.0

    def money(self, equity: float, risk_fraction: float) -> float:
        """Net R converted to currency at a fixed fractional risk.

        Deliberately linear: it assumes each trade risked the same fraction of
        the *starting* equity rather than compounding. Compounding a backtest
        is how a modest edge is made to look exponential, and the exponent is
        supplied entirely by the assumption.
        """
        return self.net_r * equity * risk_fraction

    @property
    def by_symbol(self) -> dict[str, "OrbReport"]:
        out: dict[str, OrbReport] = {}
        for symbol in sorted({t.symbol for t in self.trades} | set(self.symbols)):
            out[symbol] = OrbReport(
                trades=[t for t in self.trades if t.symbol == symbol],
                skips=[s for s in self.skips if s.symbol == symbol],
                sessions=self.sessions_by_symbol.get(symbol, 0),
                symbols=[symbol], window=self.window,
                sessions_by_symbol={symbol: self.sessions_by_symbol.get(symbol, 0)},
            )
        return out

    @property
    def skip_reasons(self) -> dict[str, int]:
        """Why untraded days were untraded, most common first."""
        counts: dict[str, int] = {}
        for s in self.skips:
            # The reasons carry live numbers; the leading clause is the class.
            key = s.reason.split(" -- ")[0].split(",")[0]
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def summary(self) -> str:
        if not self.count:
            return f"no trades over {self.sessions} sessions"
        return (f"{self.count} trades, {self.win_rate:.1%} win, "
                f"{self.expectancy_r:+.3f}R/trade, {self.net_r:+.1f}R total, "
                f"PF {self.profit_factor:.2f}, "
                f"max DD {self.max_drawdown_r:.1f}R, "
                f"{self.trade_rate:.2f} trades/session")


# --------------------------------------------------------------------------
# the simulation
# --------------------------------------------------------------------------
def _columns(bars: pd.DataFrame, sim: SimConfig) -> dict[str, np.ndarray]:
    """The bar loop's inputs as plain arrays.

    ``DataFrame.iterrows`` builds a Series per bar, which dominated the run
    time of a multi-instrument backtest -- minutes rather than seconds over a
    few years of M1. The loop below still walks bars strictly in order and
    still sees only the bar it is on; only the container changed.
    """
    n = len(bars)
    if sim.zero_cost:
        spread = np.zeros(n)
    elif "spread" in bars:
        spread = bars["spread"].astype(float).to_numpy()
        spread = np.where(np.isnan(spread), sim.fallback_spread_points, spread)
    else:
        spread = np.full(n, float(sim.fallback_spread_points))
    return {
        "high": bars["high"].astype(float).to_numpy(),
        "low": bars["low"].astype(float).to_numpy(),
        "close": bars["close"].astype(float).to_numpy(),
        "spread": spread,
    }


def _fill(price: float, direction: int, spread_price: float, slip_price: float) -> float:
    """An entry fill on the correct side of the book, plus slippage against us.

    Bars are the bid series, so a buy is filled a spread above the printed
    price and a sell at the printed price. Slippage is added in whichever
    direction hurts, never subtracted.
    """
    if direction == BUY:
        return price + spread_price + slip_price
    return price - slip_price


def simulate_session(
    bars: pd.DataFrame,
    symbol: str,
    spec: SessionSpec,
    day: date,
    cfg: OrbConfig,
    point: float,
    adr_points: float,
    sim: SimConfig,
) -> tuple[list[Trade], list[Skip]]:
    """One instrument, one session, bar by bar and in order.

    Nothing in here looks at a bar later than the one being processed, which
    is the property the whole exercise depends on and the reason the loop is
    written out longhand rather than vectorised.
    """
    rng = orb.build_range(
        bars, symbol, spec, day, cfg, point,
        commission_points=sim.commission_points,
        fallback_spread_points=sim.fallback_spread_points,
        # The counterfactual has to be consistent: a run whose fills are free
        # but whose filters still see the real spread is neither the strategy
        # nor the comparison it is supposed to be.
        cost_points=0.0 if sim.zero_cost else None,
    )
    if rng is None:
        return [], []            # a holiday, or a feed gap: not a tradeable day

    verdict = orb.check_range(rng, cfg, adr_points)
    if not verdict:
        return [], [Skip(symbol, day, verdict.reason)]

    flat_at = spec.close_utc(day) - timedelta(
        minutes=cfg.flat_before_close_minutes)
    last_entry = orb.entry_deadline(rng, cfg, spec)
    session = bars[(bars.index >= rng.end) & (bars.index <= flat_at)]
    if session.empty:
        return [], [Skip(symbol, day, "no bars between the range and the close")]

    trades: list[Trade] = []
    skips: list[Skip] = []
    barred: set[int] = set()      # directions this session has ruled out
    entries = 0
    open_trade: Optional[dict] = None

    col = _columns(session, sim)
    stamps = list(session.index)
    slip_price = (0.0 if sim.zero_cost else sim.slippage_points) * point

    for i, ts in enumerate(stamps):
        high, low, close = col["high"][i], col["low"][i], col["close"][i]
        spread_price = col["spread"][i] * point
        bar = {"high": high, "low": low, "close": close}
        just_closed = False

        # ---------------------------------------------------------- manage
        if open_trade is not None:
            plan: TradePlan = open_trade["plan"]
            stop, target = open_trade["stop"], plan.target
            direction = plan.direction

            # Exits are tested against the side of the book the position
            # closes on: a long sells at the bid (the printed low), a short
            # buys back at the ask (the printed high plus the spread).
            if direction == BUY:
                stop_hit, target_hit = low <= stop, high >= target
            else:
                stop_hit = high + spread_price >= stop
                target_hit = low + spread_price <= target

            ambiguous = bool(stop_hit and target_hit)
            if ambiguous and sim.pessimistic_fills:
                target_hit = False
            elif ambiguous:
                stop_hit = False

            if stop_hit or target_hit:
                level = stop if stop_hit else target
                reason = (EXIT_BREAKEVEN if stop_hit and open_trade["moved"]
                          else EXIT_STOP if stop_hit else EXIT_TARGET)
                trades.append(_close(open_trade, ts, level, reason, ambiguous,
                                     slip_price, point, sim))
                open_trade = None
                just_closed = True
            else:
                # Breakeven is applied only at the end of a bar that did not
                # resolve the trade. Moving it mid-bar and then testing the
                # new stop on the same bar would let the stop tighten onto a
                # low that had already happened.
                be = open_trade["breakeven_at"]
                if be is not None and not open_trade["moved"]:
                    reached = high >= be if direction == BUY else low <= be
                    if reached:
                        open_trade["stop"] = plan.entry
                        open_trade["moved"] = True

        # ------------------------------------------------------------ open
        #
        # A bar that closed a position may not open the next one. The stop was
        # hit somewhere inside the minute and this bar's close came afterwards,
        # so a same-bar re-entry is not strictly lookahead -- but it re-enters
        # on the price that just stopped the previous trade out, which is a
        # different strategy from the one being described, and a favourable one
        # to reconstruct from OHLC. One bar of daylight, deliberately.
        if open_trade is None and not just_closed \
                and entries < cfg.max_trades_per_session and ts <= last_entry:
            plan = orb.breakout(rng, ts, bar, cfg, taken=barred)
            if plan is not None:
                state = _open(plan, cfg, spread_price, slip_price, point, sim)
                if state is None:
                    skips.append(Skip(symbol, day, "the priced trade put its stop on "
                                                   "the wrong side of the fill"))
                    barred |= cfg.barred_after(plan.direction)
                    continue
                ok = orb.check_plan(state["plan"], cfg)
                if not ok:
                    skips.append(Skip(symbol, day, ok.reason))
                else:
                    open_trade = state
                    entries += 1
                    barred |= cfg.barred_after(plan.direction)

    if open_trade is not None:
        exit_price = float(col["close"][-1])
        if open_trade["plan"].direction == SELL:
            exit_price += float(col["spread"][-1]) * point
        # A timed exit is never ambiguous: the closing price is the one the
        # bar printed, not a barrier that may or may not have been touched.
        trades.append(_close(open_trade, stamps[-1], exit_price,
                             EXIT_SESSION_CLOSE, False, slip_price, point, sim))

    if not trades and not skips:
        skips.append(Skip(symbol, day, "the range held: no breakout in the entry window"))
    return trades, skips


def _mirror(entry: float, direction: int, risk: float,
            cfg: OrbConfig) -> tuple[float, float]:
    """A stop and target for the inverted baseline, built rather than derived.

    The mirror cannot reuse the range-based stop, because that stop is not
    symmetric about the entry: the opposite side of an opening range sits
    *below* the entry for a long and also below it for the inverted short,
    which is not a short at all but an instantly-profitable nonsense trade. An
    earlier version of this function did exactly that and reported a 100% win
    rate for the baseline, which is how the bug was noticed.

    So the mirror keeps the entry and the *magnitude* of the risk and flips
    which side of the entry the stop and target sit. That is the honest
    comparison: the same money at risk on the same trigger, the other way
    round. If it also makes money, the window carried a drift and neither
    direction is evidence of anything.
    """
    sign = 1 if direction == BUY else -1
    return entry - sign * risk, entry + sign * cfg.target_r * risk


def _open(plan: TradePlan, cfg: OrbConfig, spread_price: float,
          slip_price: float, point: float, sim: SimConfig) -> Optional[dict]:
    """Price the signal into a position, or refuse it.

    The direction is settled first, because which side of the book the fill
    comes from depends on it -- an inverted short fills at the bid where the
    long it replaced would have paid the ask.
    """
    direction = plan.direction
    if sim.invert:
        direction = SELL if direction == BUY else BUY

    entry = _fill(plan.entry, direction, spread_price, slip_price)

    if sim.invert:
        # The risk the real rule would have taken at this fill, mirrored.
        base_stop, _ = orb.stop_and_target(plan.range_, cfg, plan.direction, entry)
        stop, target = _mirror(entry, direction, abs(entry - base_stop), cfg)
        reason = plan.reason + " (inverted baseline)"
    else:
        # The stop and target were derived from the signal price; the fill is a
        # spread away from it for a long. Re-deriving from the *fill* keeps the
        # reward/risk ratio the config asked for, which is what a live bot
        # placing a bracket order around its own fill would get.
        stop, target = orb.stop_and_target(plan.range_, cfg, direction, entry)
        reason = plan.reason

    if (direction == BUY and stop >= entry) or (direction == SELL and stop <= entry):
        return None

    priced = replace(plan, direction=direction, entry=entry, stop=stop,
                     target=target, reason=reason)
    return {
        "plan": priced,
        "stop": stop,
        "moved": False,
        "breakeven_at": orb.breakeven_stop(priced, cfg),
        "spread_points": spread_price / point if point else 0.0,
        "commission_points": 0.0 if sim.zero_cost else sim.commission_points,
    }


def _close(state: dict, ts: datetime, price: float, reason: str,
           ambiguous: bool, slip_price: float, point: float,
           sim: SimConfig) -> Trade:
    plan: TradePlan = state["plan"]
    # Slippage on the exit hurts in the opposite direction to the entry.
    exit_price = price - slip_price if plan.direction == BUY else price + slip_price
    return Trade(
        symbol=plan.symbol, session_date=plan.range_.session_date,
        direction=plan.direction, entry_at=plan.at, entry=float(plan.entry),
        stop=float(plan.stop), final_stop=float(state["stop"]),
        target=float(plan.target),
        risk_points=float(plan.risk_points), exit_at=ts, exit=float(exit_price),
        exit_reason=reason, point_size=float(point),
        spread_points=float(state["spread_points"]),
        commission_points=float(state["commission_points"]),
        ambiguous=bool(ambiguous), range_points=float(plan.range_.width_points),
        reason=plan.reason,
    )


def run(
    frames: dict[str, pd.DataFrame],
    cfg: Optional[OrbConfig] = None,
    sim: Optional[SimConfig] = None,
    points: Optional[dict[str, float]] = None,
    sessions: Optional[dict[str, str]] = None,
    per_symbol_range_minutes: bool = True,
) -> OrbReport:
    """Every instrument, every session in the frames it was given.

    ``frames`` is 1-minute OHLC indexed by **real UTC** -- if the bars came
    from a broker terminal, ``clock.BarClock.frame_to_utc`` has to have been
    applied first, or every session boundary in here is off by the server's
    timezone offset and the strategy trades the middle of the afternoon.
    """
    cfg = cfg or OrbConfig()
    sim = sim or SimConfig()
    points = points or {}
    sessions = sessions or {}

    problems = cfg.validate()
    if problems:
        raise ValueError("contradictory OrbConfig: " + "; ".join(problems))

    report = OrbReport(symbols=sorted(frames))
    starts, ends = [], []

    for symbol, bars in sorted(frames.items()):
        if bars.empty:
            continue
        bars = orb.as_utc_index(bars).sort_index()
        starts.append(bars.index[0])
        ends.append(bars.index[-1])
        spec = session_for(symbol, sessions.get(symbol))
        point = points.get(symbol, 1.0)
        symbol_cfg = cfg
        if per_symbol_range_minutes:
            symbol_cfg = replace(cfg, range_minutes=orb.suggest_range_minutes(symbol))

        daily = orb.session_ranges(bars, spec)
        adr = orb.adr_before(daily, symbol_cfg.adr_days, point)
        counted = 0

        for day in daily.index:
            counted += 1
            adr_points = float(adr.get(day, float("nan")))
            if not np.isfinite(adr_points):
                # The first sessions in the window have no history behind them
                # to measure against. Trading them with the filter switched off
                # would be a different strategy from the one being tested.
                report.skips.append(Skip(symbol, day, "no prior sessions for the "
                                                      "average-daily-range filter"))
                continue
            trades, skips = simulate_session(
                bars, symbol, spec, day, symbol_cfg, point, adr_points, sim)
            report.trades += trades
            report.skips += skips

        report.sessions += counted
        report.sessions_by_symbol[symbol] = counted

    if starts:
        report.window = (min(starts).to_pydatetime(), max(ends).to_pydatetime())
    return report


def compare(
    frames: dict[str, pd.DataFrame],
    cfg: Optional[OrbConfig] = None,
    sim: Optional[SimConfig] = None,
    **kwargs,
) -> dict[str, OrbReport]:
    """The strategy against the three comparisons that make it interpretable.

    * ``strategy`` -- the rules as configured.
    * ``inverted`` -- every signal taken backwards. A strategy that cannot beat
      its own mirror image has found the window's drift, not an edge.
    * ``zero_cost`` -- the same trades with no spread or commission. The gap
      between this and ``strategy`` is what the broker takes, stated in R
      instead of assumed to be small.
    * ``no_filters`` -- the range and cost filters switched off. If this scores
      as well, the filters are decoration and the extra parameters are just
      more surface to curve-fit.
    """
    cfg = cfg or OrbConfig()
    sim = sim or SimConfig()
    loose = replace(cfg, min_range_cost_multiple=0.0, min_target_cost_multiple=0.0,
                    min_range_adr_fraction=0.0, max_range_adr_fraction=10.0)
    return {
        "strategy": run(frames, cfg, sim, **kwargs),
        "inverted": run(frames, cfg, replace(sim, invert=True), **kwargs),
        "zero_cost": run(frames, cfg, replace(sim, zero_cost=True), **kwargs),
        "no_filters": run(frames, loose, sim, **kwargs),
    }


def format_report(report: OrbReport, equity: float = 10_000.0,
                  risk_fraction: float = 0.005) -> str:
    """The whole result as text, with the caveats attached to the numbers."""
    out = ["OPENING RANGE BREAKOUT -- WALK-FORWARD RESULT"]
    if report.window:
        start, end = report.window
        out.append(f"{start:%Y-%m-%d} to {end:%Y-%m-%d}  "
                   f"{report.sessions} sessions across {len(report.symbols)} instruments")
    out += ["", f"  {report.summary()}"]
    if report.count:
        out += [
            f"  net {report.money(equity, risk_fraction):+,.2f} on {equity:,.0f} "
            f"at {risk_fraction:.2%} risk per trade (no compounding)",
            f"  {report.cost_points:,.0f} points of spread and commission paid, "
            f"already inside the fills above -- with risk-based sizing the cost "
            f"shows up as a lower win rate, not as a smaller R per win",
        ]
        if report.ambiguous:
            share = report.ambiguous / report.count
            out.append(
                f"  {report.ambiguous} trades ({share:.0%}) had the stop and target "
                f"inside one bar and were resolved as losses -- if this share is "
                f"large the result is an artefact of that rule")

    per = report.by_symbol
    if len(per) > 1:
        out += ["", "BY INSTRUMENT"]
        for symbol, r in per.items():
            out.append(f"  {symbol:12s} {r.summary()}")

    if report.skips:
        out += ["", "WHY DAYS WERE SKIPPED"]
        for reason, n in list(report.skip_reasons.items())[:8]:
            out.append(f"  {n:5d}  {reason}")
    return "\n".join(out)


def format_comparison(results: dict[str, OrbReport]) -> str:
    """The four runs side by side, which is the only way to read any of them."""
    order = ["strategy", "inverted", "zero_cost", "no_filters"]
    labels = {"strategy": "as configured", "inverted": "every signal reversed",
              "zero_cost": "with no spread or commission",
              "no_filters": "range/cost filters off"}
    out = ["", "COMPARISONS", "",
           f"  {'run':14s} {'trades':>7s} {'win':>7s} {'R/trade':>9s} "
           f"{'total R':>9s} {'max DD':>8s}  what it is"]
    for key in order:
        r = results.get(key)
        if r is None:
            continue
        out.append(
            f"  {key:14s} {r.count:7d} {r.win_rate:7.1%} {r.expectancy_r:+9.3f} "
            f"{r.net_r:+9.1f} {r.max_drawdown_r:8.1f}  {labels[key]}")

    strat, inv = results.get("strategy"), results.get("inverted")
    zero, loose = results.get("zero_cost"), results.get("no_filters")
    out.append("")
    if strat and strat.count == 0:
        out.append("  Nothing traded. Read the skip reasons above before touching "
                   "the parameters -- a filter may be inverted rather than strict.")
        return "\n".join(out)
    if strat and inv and inv.expectancy_r >= strat.expectancy_r:
        out.append("  The reversed version did at least as well. That is drift in "
                   "the window, not an opening-range edge. Do not trade this.")
    if strat and zero:
        bite = zero.expectancy_r - strat.expectancy_r
        out.append(f"  Costs take {bite:+.3f}R per trade. "
                   + ("The edge only exists before costs, which means it does not "
                      "exist." if zero.expectancy_r > 0 >= strat.expectancy_r
                      else "The edge survives them."))
    if strat and loose and loose.expectancy_r >= strat.expectancy_r:
        out.append("  Switching the filters off did not make it worse, so they are "
                   "not earning their parameters.")
    return "\n".join(out)
